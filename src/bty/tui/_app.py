"""bty.tui - textual terminal UI for image inspection and flashing.

Targeted at interactive use from a live environment (serial console,
SSH session, minimal recovery image). Exposes the same operations as
the ``bty`` CLI in a navigable, three-pane form (images | disks |
details), styled with the Tokyo Night theme to match the bty mascot's
navy + warm-yellow palette.

Catalog sources (combine freely):

- **Local image-root** (always scanned). Files + ``.bri`` descriptors
  under the configured root (USB live env's ``BTY_IMAGES`` partition,
  ``BTY_IMAGE_ROOT`` env, or ``--image-root /path``).
- **Catalog overlay** (``--catalog SOURCE``). One additional source --
  a local TOML file or an http(s):// / oras:// URL pointing at a TOML
  catalog. Fetched once at startup, cached in memory; the catalog's
  entries surface in the catalog table alongside the local files.
  Selecting any row -- local file, .bri descriptor, catalog entry --
  flashes through the same URL-or-path pipeline.

The remote / PXE-interactive use case: ``--catalog
http://bty-server:8080/catalog.toml`` for the bty-web instance, plus
``--mac <MAC>`` so the TUI POSTs back to the server's
``/pxe/<mac>/done`` endpoint on successful flash (derived from the
catalog URL's host).

Keymap (forward navigation is automatic on Enter-to-commit;
the keys below cover everything else):

- ``Enter``       forward (commit row, trigger active button)
- ``Esc`` / ``Backspace``  back (clear most recent commit, or
                  clear filter if one is active)
- ``q``           quit
- ``r``           refresh catalogs
- ``c``           switch catalog source (path / URL / blank for local-only)
- ``d``           load bty's default release-asset catalog
- ``i``           install bty-server (latest from GitHub releases)
- ``/``           filter the image catalog by substring
- ``f``           flash shortcut (equivalent to Enter on Flash button)

Wizard flow (3 stages, derived from selection state):

1. Stage 1: select an image (Enter on a row) -> auto-advance to
   Stage 2 with focus on Disks.
2. Stage 2: select a disk (Enter on a row) -> auto-advance to
   Stage 3 with focus on the Flash button.
3. Stage 3: Enter on the ``Flash!`` button (or ``f``) ->
   FlashConfirmScreen -> FlashStatusScreen. On success the
   action-pane button transforms into ``Reboot`` (label + handler
   swap) so the natural next step is one keypress away.

Empty catalogs render an onboarding panel with actionable next
steps (drop ``*.img.zst`` onto BTY_IMAGES, or PUT to the server's
``/images`` endpoint) instead of a blank table. The flash modal is
a stop-the-world floating overlay that disables Close until the
flash completes or fails -- the operator can't accidentally bail
mid-write.

Requires the ``[tui]`` install extra (pulls in textual).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

import bty
from bty import catalog as _catalog
from bty import disks, flash, images


class _WizardStage(IntEnum):
    """The three stages of the flash wizard.

    Derived from ``BtyTui`` selection state -- never stored directly.
    See ``BtyTui._stage``.
    """

    SELECT_IMAGE = 1
    SELECT_DISK = 2
    CONFIRM_FLASH = 3


@dataclass
class _TuiImage:
    """Unified representation of a TUI catalog row.

    Either ``path`` (local) or ``url`` (remote) is set. Used as the
    common shape between the local image-root scan and the remote
    ``GET /images`` catalog so the rest of the TUI doesn't have to
    branch.
    """

    name: str
    fmt: str | None
    size_bytes: int
    path: Path | None = None
    url: str | None = None


def _format_mib(size_bytes: int | None) -> str:
    """Format a size in bytes as a comma-grouped MiB string.

    ``None`` and negative inputs render as ``?`` so an image whose
    format probe couldn't determine a virtual size (e.g. a streamed
    raw URL whose Content-Length the server didn't advertise) shows
    a clean placeholder instead of crashing the modal.

    Used for both the images-table size column and the disk-details
    body so all size displays in the TUI share one format.
    """
    if size_bytes is None or size_bytes < 0:
        return "?"
    return f"{size_bytes / (1 << 20):,.1f} MiB"


_SIZE_SUFFIX_MULTIPLIERS = {
    "K": 1 << 10,
    "M": 1 << 20,
    "G": 1 << 30,
    "T": 1 << 40,
    "P": 1 << 50,
}


def _parse_size_to_bytes(s: str) -> int:
    """Parse an lsblk-style human-readable size ("500G", "1.5T") to
    bytes.

    lsblk emits human-readable units by default; bty's CLI + web-UI
    consumers expect that string form, so we parse to bytes only at
    the TUI display layer rather than changing ``disks.list_disks``.
    Empty / unrecognised input returns 0 (caller can format as "?").
    """
    s = s.strip().upper()
    if not s:
        return 0
    # Suffix-trailing form: "500G" / "1.5T" / "8G" / "9.1G".
    if s[-1] in _SIZE_SUFFIX_MULTIPLIERS:
        try:
            n = float(s[:-1])
        except ValueError:
            return 0
        return int(n * _SIZE_SUFFIX_MULTIPLIERS[s[-1]])
    # Plain integer-bytes form (lsblk -b).
    try:
        return int(s)
    except ValueError:
        return 0


# Catalog response size cap. Lives in ``bty.catalog`` so the CLI's
# ``bty images --catalog`` and this TUI share the same hostile-input
# protection. Re-exported here as the historical name only for
# readability of the comment block above it.


# The bty-server bootstrap shortcut (``i`` in the TUI) flashes this
# URL. ``releases/latest/download/<name>`` is GitHub's stable
# redirect-to-newest-tag pattern, so the shortcut tracks new
# releases without rebaking the live env. Network constraint: the
# live env needs HTTPS reachability to github.com /
# objects.githubusercontent.com at flash time. Air-gapped operators
# ship their own .img.gz through the BTY_IMAGES / Ventoy
# ``bty-images/`` folder path instead.
_BTY_SERVER_LATEST_URL = (
    "https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz"
)
_BTY_SERVER_LATEST_NAME = "bty-server (latest from GitHub)"

# bty's default portable catalog, published as a release asset on
# every tag. The ``d`` keybinding swaps the TUI's catalog source for
# this URL with one keypress, so an operator on a fresh stick (or a
# stick with no local images) doesn't need to type the catalog URL
# via the ``c`` switch dialog. Contents match the four-entry
# BTY_IMAGES starter ``.bri`` set: three nosi sysdev images via
# ``oras://`` rolling tags plus the bty-server appliance via its
# GitHub release URL.
_BTY_DEFAULT_CATALOG_URL = "https://github.com/safl/bty/releases/latest/download/catalog.toml"


def load_catalog_from_source(source: str, *, timeout: float = 30.0) -> list[_TuiImage]:
    """Load catalog rows from a local path or remote URL into the TUI shape.

    Source can be:

    - a local file path (``./catalog.toml``, ``/etc/bty/catalog.toml``,
      ``file:///path/to/catalog.toml``)
    - an HTTP(S) URL serving a TOML catalog
      (``https://example.com/catalog.toml``, or a bty-web instance's
      ``http://server:8080/catalog.toml``)
    - an ``oras://`` reference whose layer is a TOML catalog
      (``oras://ghcr.io/owner/bty-catalog:latest``)

    Thin projection over :func:`bty.catalog.load_source` into the
    TUI's ``_TuiImage`` row shape. Free function so unit tests can
    mock ``urllib.request.urlopen`` without instantiating a textual
    ``App``.
    """
    parsed_catalog = _catalog.load_source(source, timeout=timeout)
    return [
        _TuiImage(
            name=entry.name,
            fmt=entry.format,
            size_bytes=entry.size_bytes or 0,
            url=entry.src,
        )
        for entry in parsed_catalog.entries
    ]


def _pxe_done_base_from_source(source: str | None) -> str | None:
    """Derive a bty-web base URL for the pxe-done POST from a
    ``--catalog`` source. Returns ``None`` when the source isn't an
    http(s) URL (static file / ``oras://`` -> no pxe-done signal).

    Best-effort: if the catalog source's scheme+host pair turns out
    not to be a bty-web after all, the POST simply fails harmlessly
    in :func:`post_pxe_done`.
    """
    if source is None:
        return None
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def post_pxe_done(pxe_done_base: str, mac: str, *, timeout: float = 10.0) -> None:
    """Best-effort ``POST <pxe_done_base>/pxe/{mac}/done`` after a
    successful remote flash. Silent on success; raises
    ``urllib.error.URLError`` on transport failure (caller decides
    whether to surface). ``pxe_done_base`` is the pre-derived
    scheme+host pair from :func:`_pxe_done_base_from_source`; no
    further validation here."""
    base = pxe_done_base.rstrip("/")
    req = urllib.request.Request(f"{base}/pxe/{mac}/done", method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


class FlashConfirmScreen(ModalScreen[bool]):
    """Modal showing the flash plan and asking for confirmation.

    Returns ``True`` when the operator confirms, ``False`` otherwise.
    Errors disable the confirm button so an invalid plan cannot proceed.
    """

    DEFAULT_CSS = """
    FlashConfirmScreen {
        align: center middle;
    }

    /* Floating panel with rounded border + ``Flash plan`` label in
       the border-title (harlequin / posting style). Matches the
       main app's border treatment so the modal feels like a
       layered piece of the same UI. */
    FlashConfirmScreen > Vertical {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    .errors {
        color: $error;
        margin: 1 0;
        padding: 1;
        border: round $error 50%;
    }

    .actions {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    /* Buttons get a minimum width so short labels don't render as
       tiny squares, and an explicit focus style (``bold reverse``,
       which renders identically on tty1 / xterm / SSH consoles)
       so the operator can never mistake which button Enter is
       about to trigger. The default textual focus shading is too
       subtle on a framebuffer. */
    .actions Button {
        margin-left: 2;
        min-width: 14;
    }

    .actions Button:focus {
        text-style: bold reverse;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss(False)", "Cancel"),
    ]

    def __init__(self, plan: flash.FlashPlan, errors: list[str]) -> None:
        super().__init__()
        self._plan = plan
        self._errors = errors

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Flash plan  "
            yield Static(self._plan_text())
            if self._errors:
                yield Static(self._errors_text(), classes="errors")
            with Horizontal(classes="actions"):
                # Conventional dialog layout: Cancel on the left,
                # primary action on the right. Default focus is set
                # explicitly in ``on_mount`` (Confirm, unless errors
                # disabled it) -- DOM order would otherwise default
                # to Cancel, and the operator just clicked Flash to
                # get here, so Enter should advance not cancel.
                yield Button("Cancel", id="cancel", variant="default")
                yield Button(
                    "Flash now",
                    id="confirm",
                    variant="primary",
                    disabled=bool(self._errors),
                )

    def on_mount(self) -> None:
        # Belt-and-braces: explicitly focus the confirm button if it's
        # enabled, fall back to cancel if validation errors disabled
        # it. Don't trust DOM-order focus; users hitting Enter twice
        # in quick succession (once on Flash!, once on the modal)
        # were accidentally cancelling.
        try:
            confirm = self.query_one("#confirm", Button)
        except Exception:  # pragma: no cover - defensive
            return
        if confirm.disabled:
            with contextlib.suppress(Exception):
                self.query_one("#cancel", Button).focus()
        else:
            confirm.focus()

    def _plan_text(self) -> str:
        # MiB rather than raw-byte counts so the operator can compare
        # image-virtual vs target-size at a glance in the modal -- the
        # decision moment doesn't benefit from billions-of-bytes
        # precision.
        plan = self._plan
        return "\n".join(
            [
                f"Image:      {plan.image.display}",
                f"Format:     {plan.image.format}",
                f"Size:       {_format_mib(plan.image.virtual_size_bytes)} (virtual)",
                f"Target:     {plan.target.path}",
                f"Target sz:  {_format_mib(plan.target.size_bytes)}",
            ]
        )

    def _errors_text(self) -> str:
        return "Validation FAILED:\n" + "\n".join(f"  - {e}" for e in self._errors)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class ProbingScreen(ModalScreen[None]):
    """Lightweight floating modal shown while ``flash.probe_image`` and
    ``flash.probe_target`` run.

    The probe phase runs ``qemu-img info`` and ``blockdev --getsize64``
    in worker threads -- normally fast, but on a slow USB target or a
    large qcow2 the operator was left staring at a static
    ``Flash: probing image foo.qcow2...`` status line with no visible
    motion. That read as a freeze.

    This modal pops immediately on Flash trigger, animates an ASCII
    spinner (``|/-\\`` -- tty1-safe, no Unicode), and ticks the row's
    state to ``[X] done`` as each probe completes. The caller dismisses
    it once the plan is built; the ``FlashConfirmScreen`` then takes
    over.
    """

    DEFAULT_CSS = """
    ProbingScreen {
        align: center middle;
    }

    ProbingScreen > Vertical {
        width: 60;
        height: 7;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    .probe-row {
        height: 1;
    }
    """

    _SPINNER: ClassVar[str] = "|/-\\"

    def __init__(self, image_name: str, target_path: str) -> None:
        super().__init__()
        self._image_name = image_name
        self._target_path = target_path
        self._image_done = False
        self._target_done = False
        self._frame = 0

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Probing  "
            yield Static("", id="probe-image", classes="probe-row")
            yield Static("", id="probe-target", classes="probe-row")

    def on_mount(self) -> None:
        self._redraw()
        # 150ms tick rate: fast enough that motion reads as "working",
        # slow enough that the framebuffer console isn't redrawing
        # constantly.
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._SPINNER)
        self._redraw()

    def _redraw(self) -> None:
        spin = self._SPINNER[self._frame]
        img_state = "[X] done" if self._image_done else f"[ ] {spin}   "
        tgt_state = "[X] done" if self._target_done else f"[ ] {spin}   "
        try:
            self.query_one("#probe-image", Static).update(
                f"{img_state}  Image:  {self._image_name}"
            )
            self.query_one("#probe-target", Static).update(
                f"{tgt_state}  Target: {self._target_path}"
            )
        except Exception:  # pragma: no cover - defensive during teardown
            pass

    def image_done(self) -> None:
        self._image_done = True
        self._redraw()

    def target_done(self) -> None:
        self._target_done = True
        self._redraw()


class FlashStatusScreen(ModalScreen[str]):
    """Floating modal that runs the flash in a worker and reports progress.

    Designed to feel like helix/zellij modals: bold stop-the-world
    "DO NOT REMOVE STICK" framing, a visual stage track that ticks
    as ``flash.execute_plan`` emits each lifecycle event, and a
    streaming event log below. The Close button is disabled until
    the flash settles to one of:

    - ``"ok"`` - success (returned via ``dismiss``)
    - ``"failed"`` - pipeline error
    - ``"cancelled"`` - operator pressed Cancel / Esc; the watchdog
      terminated curl + decompressor + dd before the flash completed.

    The Cancel button (enabled while the flash is running) sets a
    ``threading.Event`` the worker passes to ``flash.execute_plan``
    as ``cancel``. The flash code's cancel watchdog then SIGTERM's
    its subprocess pipeline and raises :class:`flash.FlashCancelled`.
    """

    # Stable order of FlashProgress.event values that this modal
    # treats as "stages" with a visible row in the tracker. Any
    # other event lands in the log but doesn't tick a stage.
    _STAGES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("started", "Validating plan"),
        ("writing", "Writing image to disk"),
        ("synced", "Flushing kernel buffers"),
        ("partprobed", "Re-reading partition table"),
        ("done", "Done"),
    )

    DEFAULT_CSS = """
    FlashStatusScreen {
        align: center middle;
    }

    FlashStatusScreen > Vertical {
        width: 90;
        height: 30;
        padding: 1 2;
        background: $panel;
        border: round $warning;
        border-title-style: bold;
        border-title-color: $warning;
        border-title-align: left;
    }

    .flash-warning {
        height: 3;
        content-align: center middle;
        background: $warning 30%;
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }

    .flash-target {
        height: 1;
        content-align: center middle;
        color: $text-muted;
        margin-bottom: 1;
    }

    .flash-stages {
        height: auto;
        margin-bottom: 1;
    }

    .flash-stage {
        height: 1;
        padding-left: 1;
    }

    .flash-stage.done {
        color: $success;
    }

    .flash-stage.active {
        color: $accent;
        text-style: bold;
    }

    .flash-stage.pending {
        color: $text-muted;
    }

    .flash-stage.failed {
        color: $error;
        text-style: bold;
    }

    .flash-progress {
        height: auto;
        margin-bottom: 1;
    }

    .flash-progress-summary {
        height: 1;
        color: $text-muted;
        padding-left: 1;
    }

    #flash-progress-bar {
        height: 1;
        margin-top: 0;
    }

    RichLog {
        height: 1fr;
        border: tall $primary 50%;
    }

    #flash-actions {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    #flash-actions Button {
        min-width: 14;
    }

    #flash-actions Button:focus {
        text-style: bold reverse;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel_flash", "Cancel"),
    ]

    def __init__(self, plan: flash.FlashPlan) -> None:
        super().__init__()
        self._plan = plan
        # ``"ok"`` / ``"failed"`` / ``"cancelled"`` once the worker
        # settles; ``None`` while in-flight. ``dismiss`` uses it.
        self._result: str | None = None
        self._completed_stages: set[str] = set()
        # Used to compute MB/s as ``writing_progress`` events arrive.
        # Set on first event; reset on each successful run.
        self._progress_start_t: float | None = None
        self._progress_start_bytes: int | None = None
        # ``threading.Event`` (not asyncio) because the flash worker
        # runs in a thread via ``@work(thread=True)``; the Cancel
        # button's handler runs in the textual event-loop and just
        # ``set()``s it. The flash code's watchdog polls
        # ``cancel()`` ~4Hz and terminates curl/dd on True.
        import threading

        self._cancel_event = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Flashing  "
            yield Static(
                "FLASHING - DO NOT REMOVE STICK OR DISCONNECT",
                classes="flash-warning",
            )
            yield Static(
                f"{self._plan.image.display} -> {self._plan.target.path}",
                classes="flash-target",
            )
            with Vertical(classes="flash-stages"):
                for event_name, label in self._STAGES:
                    yield Static(
                        f"[ ] {label}",
                        id=f"stage-{event_name}",
                        classes="flash-stage pending",
                    )
            with Vertical(classes="flash-progress"):
                # ProgressBar renders an indeterminate bar until total
                # is set; once we know the image's virtual_size_bytes
                # (from the ``started`` event) we set total and the
                # ``writing_progress`` events drive the percent.
                yield ProgressBar(
                    id="flash-progress-bar",
                    show_eta=True,
                    show_percentage=True,
                )
                yield Static("", id="flash-progress-summary", classes="flash-progress-summary")
            yield RichLog(highlight=False, markup=True, id="flash_log")
            with Horizontal(id="flash-actions"):
                # Cancel is enabled while the flash runs; disabled once
                # _finish lands (the operator picks Close at that point).
                yield Button("Cancel", id="cancel-flash", variant="warning")
                yield Button("Close", id="close", variant="default", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("[dim]Starting flash...[/]")
        self._run_flash()

    @work(thread=True, exclusive=True)
    def _run_flash(self) -> None:
        def on_progress(event: flash.FlashProgress) -> None:
            # ``writing_progress`` fires ~1/sec from a daemon thread
            # parsing dd's stderr. Don't append to the log on every
            # tick (would flood); only update the progress bar +
            # summary line. The other events fire once each, so we
            # log them.
            if event.event == "writing_progress":
                self.app.call_from_thread(self._update_progress_bar, event)
                return

            line = f"[{event.event}]"
            if event.note:
                line += f" {event.note}"
            if event.total_bytes is not None:
                line += f" total_bytes={event.total_bytes}"
            self.app.call_from_thread(self._append_log, line)
            self.app.call_from_thread(self._mark_stage_active, event.event)
            # ``started`` carries total_bytes; latch it onto the bar so
            # subsequent writing_progress ticks show real percent + ETA.
            if event.event == "started" and event.total_bytes is not None:
                self.app.call_from_thread(self._set_progress_total, event.total_bytes)

        try:
            flash.execute_plan(
                self._plan,
                progress=on_progress,
                cancel=self._cancel_event.is_set,
            )
            self.app.call_from_thread(self._mark_stage_active, "done")
            self.app.call_from_thread(self._finish, "ok", "[green][OK] Flash completed.[/]")
        except flash.FlashCancelled as exc:
            # ``FlashCancelled`` subclasses ``FlashError``; catch it
            # FIRST so the "failed" path doesn't swallow a deliberate
            # operator-requested abort. The cancel watchdog has
            # already SIGTERM'd the subprocess pipeline by the time
            # we get here.
            self.app.call_from_thread(self._mark_stage_failed)
            self.app.call_from_thread(
                self._finish,
                "cancelled",
                f"[yellow][CANCELLED] {exc}[/]",
            )
        except flash.FlashError as exc:
            self.app.call_from_thread(self._mark_stage_failed)
            self.app.call_from_thread(self._finish, "failed", f"[red][FAIL] Flash failed: {exc}[/]")

    def _append_log(self, line: str) -> None:
        self.query_one(RichLog).write(line)

    def _set_progress_total(self, total_bytes: int) -> None:
        try:
            bar = self.query_one("#flash-progress-bar", ProgressBar)
        except Exception:  # pragma: no cover - defensive
            return
        bar.update(total=total_bytes, progress=0)

    def _update_progress_bar(self, event: flash.FlashProgress) -> None:
        """Advance the bar + summary line as dd reports byte progress.

        ``event.bytes_written`` is the cumulative count from the start
        of the write. Speed is computed as a moving average from the
        first observed (bytes, t) snapshot of this run -- enough to
        smooth out jitter without keeping a long ring buffer.
        """
        if event.bytes_written is None:
            return
        try:
            bar = self.query_one("#flash-progress-bar", ProgressBar)
            summary = self.query_one("#flash-progress-summary", Static)
        except Exception:  # pragma: no cover - defensive
            return

        import time

        now = time.monotonic()
        if self._progress_start_t is None:
            self._progress_start_t = now
            self._progress_start_bytes = event.bytes_written

        elapsed = max(now - self._progress_start_t, 0.001)
        delta_bytes = event.bytes_written - (self._progress_start_bytes or 0)
        bytes_per_sec = delta_bytes / elapsed
        mb_per_sec = bytes_per_sec / (1024 * 1024)

        # Update the textual ProgressBar. ``progress`` is the absolute
        # value (not a delta) - textual handles the rendering. If we
        # don't know total (qcow2 / unknown source size), the bar
        # stays indeterminate and only the summary line shows
        # bytes + speed.
        if event.total_bytes:
            bar.update(progress=event.bytes_written)

        gib_written = event.bytes_written / (1024**3)
        if event.total_bytes:
            gib_total = event.total_bytes / (1024**3)
            summary_text = f"  {gib_written:.2f} / {gib_total:.2f} GiB · {mb_per_sec:.1f} MB/s"
        else:
            summary_text = f"  {gib_written:.2f} GiB · {mb_per_sec:.1f} MB/s"
        summary.update(summary_text)

    def _mark_stage_active(self, event_name: str) -> None:
        """Tick the stage tracker: previous active becomes done, this one becomes active.

        Special case: when ``event_name`` is the LAST stage (``done``),
        we mark it as ``done`` itself rather than ``active`` -- the
        final stage represents "the flash succeeded", not "still
        running this stage". So at the end of a successful run all
        stages are marked ``done``.

        ``event_name`` may be an event we don't render as a stage;
        in that case the tracker doesn't change.
        """
        stage_ids = {name for name, _ in self._STAGES}
        if event_name not in stage_ids:
            return
        is_final = event_name == self._STAGES[-1][0]
        seen_current = False
        for name, label in self._STAGES:
            if name == event_name:
                seen_current = True
                if is_final:
                    self._set_stage_class(name, "done", marker="X", label=label)
                else:
                    self._set_stage_class(name, "active", marker="*", label=label)
                self._completed_stages.add(name)
            elif not seen_current:
                # Earlier stage; mark done if not already.
                self._set_stage_class(name, "done", marker="X", label=label)
                self._completed_stages.add(name)
            else:
                self._set_stage_class(name, "pending", marker=" ", label=label)

    def _mark_stage_failed(self) -> None:
        """Mark the currently-active stage as failed; leave earlier as done."""
        for name, label in self._STAGES:
            try:
                widget = self.query_one(f"#stage-{name}", Static)
            except Exception:  # pragma: no cover - defensive
                continue
            if "active" in widget.classes:
                self._set_stage_class(name, "failed", marker="!", label=label)
                return

    def _set_stage_class(self, name: str, state: str, *, marker: str, label: str) -> None:
        try:
            widget = self.query_one(f"#stage-{name}", Static)
        except Exception:  # pragma: no cover - defensive
            return
        widget.update(f"[{marker}] {label}")
        widget.set_classes(f"flash-stage {state}")

    def _finish(self, result: str, message: str) -> None:
        log = self.query_one(RichLog)
        log.write(message)
        self._result = result
        success = result == "ok"
        # Stop the progress bar's animation. When ``total`` was
        # never set (image lacked a known virtual_size_bytes, or
        # the flash failed before the ``started`` event), the bar
        # stays in indeterminate mode and continues to bounce
        # back-and-forth even though the flash is done. Force the
        # bar to a finished determinate state so it freezes at
        # 100% (on success) or 0% (on failure / cancel).
        try:
            bar = self.query_one("#flash-progress-bar", ProgressBar)
            if bar.total is None:
                bar.update(total=1, progress=(1 if success else 0))
            elif success:
                bar.update(progress=bar.total)
        except Exception:  # pragma: no cover - defensive
            pass
        # Cancel is now meaningless; Close becomes the operator's
        # next action.
        with contextlib.suppress(Exception):
            self.query_one("#cancel-flash", Button).disabled = True
        close_btn = self.query_one("#close", Button)
        close_btn.disabled = False
        close_btn.focus()

    def action_cancel_flash(self) -> None:
        """Esc binding: same effect as pressing the Cancel button."""
        self._request_cancel()

    def _request_cancel(self) -> None:
        if self._result is not None:
            return  # already settled; Cancel is a no-op
        if self._cancel_event.is_set():
            return  # already requested; wait for the watchdog
        self._cancel_event.set()
        with contextlib.suppress(Exception):
            self.query_one(RichLog).write(
                "[yellow]Cancelling: terminating curl / decompressor / dd ...[/]"
            )
        # Disable Cancel so a second press doesn't look ambiguous.
        with contextlib.suppress(Exception):
            self.query_one("#cancel-flash", Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            # ``_result`` is one of "ok" / "failed" / "cancelled" by the
            # time Close is enabled; ``or "failed"`` is just defensive
            # for a closed-too-early-without-result race.
            self.dismiss(self._result or "failed")
            return
        if event.button.id == "cancel-flash":
            self._request_cancel()
            return


class CatalogSelectScreen(ModalScreen["str | Path | None"]):
    """Modal for switching the catalog source mid-session.

    Returns:
    - ``str`` (catalog SOURCE -- local path or http/https/oras URL) when
      the operator enters a value and confirms.
    - ``Path`` (local image root) when they clear the input + confirm
      (local-only mode).
    - ``None`` on Esc.

    The current source is pre-filled in the input. Apply re-populates
    pane-1 and updates its border-title via the caller.
    """

    DEFAULT_CSS = """
    CatalogSelectScreen {
        align: center middle;
    }

    CatalogSelectScreen > Vertical {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    CatalogSelectScreen Input {
        margin-top: 1;
    }

    .source-help {
        color: $text-muted;
        margin-top: 1;
    }

    .source-actions {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    .source-actions Button {
        margin-left: 2;
        min-width: 14;
    }

    .source-actions Button:focus {
        text-style: bold reverse;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, current_source: str | None, current_image_root: Path) -> None:
        super().__init__()
        self._current_source = current_source
        self._current_image_root = current_image_root

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Switch catalog source  "
            yield Static("Catalog source (path or URL; blank = local-only):")
            initial = self._current_source if self._current_source else ""
            yield Input(
                value=initial,
                placeholder=(
                    "/path/to/catalog.toml | https://host/catalog.toml | "
                    "oras://ghcr.io/owner/repo:tag"
                ),
                id="source-url",
            )
            yield Static(
                f"Local image-root: {self._current_image_root}",
                classes="source-help",
            )
            with Horizontal(classes="source-actions"):
                yield Button("Cancel", id="source-cancel", variant="default")
                yield Button("Apply", id="source-apply", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#source-url", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "source-cancel":
            self.dismiss(None)
            return
        if event.button.id == "source-apply":
            self._apply()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter on the URL field is the natural "apply" gesture.
        if event.input.id == "source-url":
            self._apply()

    def _apply(self) -> None:
        url = self.query_one("#source-url", Input).value.strip()
        if not url:
            self.dismiss(self._current_image_root)
        else:
            self.dismiss(url)


class HelpScreen(ModalScreen[None]):
    """Cheat sheet of every bty-tui keybinding.

    Triggered by ``?`` from the main screen. The operator on the bty
    USB live env has no docs at hand -- this modal is the only
    discovery surface for the wizard / source / filter
    bindings. ``Esc``, ``q``, or ``?`` again all close it.
    """

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }

    HelpScreen > Vertical {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    .help-section {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    .help-row {
        height: 1;
    }

    .help-footer {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("q", "dismiss(None)", "Close"),
        Binding("question_mark", "dismiss(None)", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  bty-tui keybindings  "
            yield Static("Wizard", classes="help-section")
            yield Static("  Enter         commit selection, advance one stage", classes="help-row")
            yield Static("  Esc / Bksp    undo last commit, return one stage", classes="help-row")
            yield Static("  f             trigger Flash (Stage 3+)", classes="help-row")
            yield Static(
                "  Reboot        action-pane button (after a successful flash)",
                classes="help-row",
            )
            yield Static("Navigation", classes="help-section")
            yield Static("  1 / 2         jump focus to Images / Disks pane", classes="help-row")
            yield Static("  h / Left      cycle focus to previous pane", classes="help-row")
            yield Static("  l / Right     cycle focus to next pane", classes="help-row")
            yield Static("  Up / Down     navigate rows in the focused table", classes="help-row")
            yield Static("Actions", classes="help-section")
            yield Static(
                "  r             refresh image catalog + disks",
                classes="help-row",
            )
            yield Static(
                "  c             switch catalog source (path / URL / blank for local-only)",
                classes="help-row",
            )
            yield Static(
                "  d             load bty's default catalog (the release-asset catalog.toml)",
                classes="help-row",
            )
            yield Static(
                "  i             install bty-server (latest from GitHub)",
                classes="help-row",
            )
            yield Static(
                "  /             filter the image catalog by substring",
                classes="help-row",
            )
            yield Static("  ?             this help", classes="help-row")
            yield Static("  q             quit", classes="help-row")
            yield Static("Esc, q, or ? to close.", classes="help-footer")


class BtyTui(App[None]):
    """The bty terminal UI.

    Layout: three columns -- images (left), disks (middle), details
    (right; updates with whatever's currently focused). Filter the
    images list with ``/`` (helix-style; press ``escape`` to clear).
    Empty catalogs render an onboarding panel instead of a blank
    table.

    No modifier keys, no modal navigation -- bty has so few actions
    that direct single-key bindings cover the surface.
    """

    TITLE = "bty"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("f", "flash", "Flash", show=False),
        # No standalone reboot key-binding. Reboot is exclusively reached
        # via the action-pane button that flips to "Reboot" after a
        # successful flash (see ``on_button_pressed``). Pairing a one-
        # keypress refresh (lowercase ``r``) with a one-keypress reboot
        # (``R`` aka shift+r) was a real fat-finger trap.
        Binding("i", "install_bty_server", "Install bty-server", show=False),
        Binding("c", "catalog", "Catalog", show=False),
        # ``d`` swaps the catalog source for bty's default release-
        # asset catalog (the one published alongside every bty tag
        # at https://github.com/safl/bty/releases/latest/download/
        # catalog.toml). Lets an operator on a fresh / empty-local
        # stick get a flashable catalog with one keypress -- no need
        # to type the URL via ``c``. ``show=True`` so the Footer
        # advertises it; this is the empty-state onboarding CTA for
        # operators who skip the welcome panel.
        Binding("d", "default_catalog", "Default catalog"),
        Binding("slash", "focus_filter", "Filter", show=False),
        # ``?`` pops a help modal listing every keybinding. Common
        # TUI convention (helix, k9s, lazygit); the operator on the
        # bty live env has no docs at hand -- this is the cheat
        # sheet.
        Binding("question_mark", "help", "Help", show=False),
        # Wizard-back binding. Esc / Backspace clear the most-recent
        # commit and return one stage. Forward advance happens
        # automatically when a row is committed via Enter -- no
        # separate pane-jump bindings needed.
        Binding("escape", "wizard_back", "Back", show=False),
        Binding("backspace", "wizard_back", "Back", show=False),
    ]

    DEFAULT_CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    /* Four numbered panes stacked vertically, harlequin/posting
       style: rounded border with the stage label in the top-
       border (set via ``border_title`` in ``on_mount``). Images
       and Disks get the bulk of the height (1fr each, flexible);
       Flash and Reboot are small action panels with fixed
       heights so they don't waste room on a TTY. Focused pane
       lights up in $accent so the operator always knows where
       keystrokes land. */
    .pane {
        layout: vertical;
        border: round $primary 40%;
        background: $panel;
        margin: 0 1 0 1;
        padding: 0 2;
        border-title-style: bold;
        border-title-color: $primary;
        border-title-align: left;
    }

    /* Focused pane: only the border + title color shift to the
       accent color; interior background stays the same as the
       unfocused state. The pane-line switch alone is enough
       signal without the background tint, which competed with
       the DataTable's row-cursor highlight. */
    .pane:focus-within {
        border: round $accent;
        border-title-color: $accent;
    }

    #pane-1 {
        height: 1fr;
        margin-top: 1;
    }

    #pane-2 {
        height: 1fr;
    }

    /* Action pane (Flash): the big primary button is centered
       both axes via ``align: center middle`` on the parent.
       Height: 5 rows = 2 for the rounded border + 3 for the
       button. */
    #pane-3 {
        height: 5;
        align: center middle;
        margin-bottom: 0;
    }

    /* DataTable styling left to the active theme on purpose --
       overriding component classes (.datatable--header, etc.) is
       fragile across Textual minor versions and risks visibility
       regressions on hardware tty1. The theme's defaults already
       hit the visual target inside our rounded panels. */
    DataTable {
        height: 1fr;
        background: transparent;
    }

    #filter-input {
        height: 3;
        margin: 0 1 0 1;
        border: round $primary 40%;
        background: transparent;
        display: none;
    }

    #filter-input.active {
        display: block;
    }

    #filter-input:focus {
        border: round $accent;
    }

    #welcome {
        height: auto;
        padding: 1 2;
        color: $text-muted;
    }

    /* Action-pane content: a fixed-width primary button centered
       in the pane body via the parent's ``align: center middle``.
       The pane's border-title carries the stage label; the button
       is the operator's commit handle. ``bold reverse`` on focus
       so the operator can never mistake which Enter target is
       active -- the textual default focus shading is too subtle
       on a framebuffer console. */
    .action-button {
        width: 24;
        height: 3;
    }

    .action-button:focus {
        text-style: bold reverse;
    }

    /* Bottom nav: a single line with three key-hint groups
       (quit, reboot, nav). No per-stage variation -- these
       three are always available. */
    #status-bar {
        height: 1;
        padding: 0 2;
        color: $text-muted;
    }

    #status {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        background: $boost;
    }

    #key-hints {
        width: 1fr;
        height: 1;
        content-align: right middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        image_root: Path | None = None,
        *,
        catalog_source: str | None = None,
        mac: str | None = None,
    ) -> None:
        super().__init__()
        # ``catalog_source`` is the operator's ``--catalog`` value:
        # a local path or an http(s):// / oras:// URL pointing at a
        # TOML catalog. Stored verbatim (no rstrip "/"): URLs need
        # the full filename suffix and paths might legitimately end
        # in ``/`` (treated as a dir error downstream).
        self._catalog_source: str | None = catalog_source
        # ``_pxe_done_base`` is auto-derived from the catalog source
        # when it's http(s)://. Used by the PXE interactive-mode TUI
        # to POST a completion signal back to bty-web. Static-file
        # and oras:// sources -> None -> no POST.
        self._pxe_done_base: str | None = _pxe_done_base_from_source(catalog_source)
        # Catalog entries fetched ONCE at startup (per the operator-
        # confirmed model: refresh re-scans local image-root only;
        # the remote catalog is point-in-time). Lazy-loaded on first
        # populate so __init__ stays IO-free.
        self._cached_remote_catalog: list[_TuiImage] | None = None
        self._mac: str | None = mac
        self._image_root: Path = image_root or images.default_image_root()
        # Unified shape so the row-selected-> flash path doesn't branch.
        self._images_by_key: dict[str, _TuiImage] = {}
        self._disks_by_key: dict[str, dict[str, object]] = {}
        # Filter state: when set, _populate_images includes only rows
        # whose name contains this substring (case-insensitive).
        self._filter: str = ""
        # Wizard state. The current stage is *derived* from these
        # bools via the ``_stage`` property -- numeric jumps and Esc
        # both stay coherent because we never store stage directly.
        self._selected_image: _TuiImage | None = None
        self._selected_disk: dict[str, object] | None = None
        # ``_post_flash`` flips True when a flash returns success;
        # the action pane's button transforms from ``Flash!`` into
        # ``Reboot`` so the operator's natural next step (boot the
        # freshly flashed disk) is one keypress away. Esc /
        # Backspace clears it.
        self._post_flash: bool = False
        # Last message passed to ``_set_status_transient``. The
        # auto-clear timer compares against this rather than reading
        # the widget back, so the typing stays clean and the clear
        # is robust to any future rich/markup the status line picks up.
        self._transient_status: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        # Four numbered panes stacked vertically. Each pane carries
        # the stage name + number in its top-border (set in
        # on_mount), and the focused pane lights up in $accent so
        # the operator always knows where keystrokes land.
        with Vertical(classes="pane", id="pane-1"):
            yield Input(
                placeholder="filter (substring match on name)",
                id="filter-input",
            )
            yield DataTable(id="images_table", cursor_type="row")
            yield Static("", id="welcome")
        with Vertical(classes="pane", id="pane-2"):
            yield DataTable(id="disks_table", cursor_type="row")
        # Stage 3 pane: a single big ``Flash!`` button centered in
        # the pane body. Disabled until both image + disk are
        # committed (the safety net against an accidental click on
        # an incomplete plan).
        with Vertical(classes="pane", id="pane-3"):
            yield Button("Flash!", id="flash-btn", variant="primary", classes="action-button")
        # Bottom nav: a single line with three universal hint
        # groups -- quit, reboot, nav. Stage-aware hints would be
        # more information-dense but the simpler layout is
        # explicitly user-preferred.
        yield Static(self._nav_text(), id="status-bar")
        yield Static(self._initial_status(), id="status")

    def on_mount(self) -> None:
        # Tokyo Night picks up the navy + warm-yellow palette of the
        # bty mascot (saturated cool background, yellow accents).
        # Single fixed theme: no runtime picker.
        self.theme = "tokyo-night"
        self.sub_title = bty.__version__
        # Border-title labels on each pane (harlequin / posting style:
        # the title sits in the top-border, not as a separate Static
        # row, so the panel feels like one piece). The images label
        # carries the source so the operator can see where the catalog
        # is coming from at a glance.
        # Source label: local image-root by default, with the catalog
        # source overlay appended when --catalog is set so the operator
        # sees both feeds at a glance.
        if self._catalog_source is not None:
            source_label = f"{self._image_root} + {self._catalog_source}"
        else:
            source_label = str(self._image_root)
        self.query_one(
            "#pane-1", Vertical
        ).border_title = f"  1: Pick an image from {source_label}  "
        self.query_one(
            "#pane-2", Vertical
        ).border_title = "  2: Select disk to write the image to  "
        # Pane-3's title flips post-flash via ``_render_status``;
        # set the pre-flash variant here so the initial render is
        # consistent.
        self.query_one("#pane-3", Vertical).border_title = "  3: Flash! Actually write the image!  "
        # Populate disks first so the images table's RowHighlighted
        # fires last and the details pane shows the image (the primary
        # pane) by default rather than a disk.
        self._populate_disks()
        self._populate_images()
        # Initial status-bar render: Stage 1 active, no selections,
        # key hints shown.
        self._render_status()
        # Focus the images table so the wizard starts on Stage 1
        # with the operator able to immediately Enter on a row.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self.query_one("#images_table", DataTable).focus()

    # ---------- data refresh ------------------------------------------------

    def _populate_images(self) -> None:
        table = self.query_one("#images_table", DataTable)
        welcome = self.query_one("#welcome", Static)
        table.clear(columns=True)
        # Header is just "Size"; the cell carries the unit ("MiB") so
        # both the images table and the disk-details body align on
        # the same format. See user-confirmed UX choice in plan.
        table.add_columns("Name", "Format", "Size")
        self._images_by_key.clear()

        try:
            entries = self._load_images()
        except OSError as exc:
            self._set_status(f"Error reading images: {exc}")
            welcome.update("")
            return
        except (urllib.error.URLError, ValueError) as exc:
            self._set_status(f"Error fetching catalog: {exc}")
            welcome.update("")
            return

        if not entries:
            # Empty catalog: keep the (empty) data table visible but
            # populate the welcome panel below it with actionable
            # next steps. Status line keeps the legacy short form so
            # tests / scripts have a stable hook.
            welcome.update(self._welcome_text())
            source = self._catalog_source or str(self._image_root)
            self._set_status(f"No images at {source}. See screen for how to add some.")
            return

        # Apply filter if set.
        filtered = entries
        if self._filter:
            needle = self._filter.lower()
            filtered = [e for e in entries if needle in e.name.lower()]

        if not filtered:
            welcome.update("")
            self._set_status(f"No images match {self._filter!r}; press Escape to clear the filter.")
            return

        # Non-empty: clear the onboarding panel and let the table fill the space.
        welcome.update("")
        for tui_img in filtered:
            # Remote rows use the URL as their key (also passed verbatim to
            # ``flash.probe_image_url`` later); local rows use the path string.
            key = tui_img.url if tui_img.url is not None else str(tui_img.path)
            self._images_by_key[key] = tui_img
            table.add_row(
                tui_img.name,
                tui_img.fmt or "?",
                _format_mib(tui_img.size_bytes),
                key=key,
            )

    def _welcome_text(self) -> str:
        """Compose onboarding text shown when the catalog is empty.

        Two variants: local (no catalog source set) leads with the
        ``d`` default-catalog CTA; remote (a source URL is set but
        returned no entries) leads with switch / add-entries. Both
        stay tight enough to fit a tty1 framebuffer screen without
        scrolling.
        """
        if self._catalog_source is not None:
            return (
                "[b]No images in this catalog.[/]\n\n"
                f"Source: [accent]{self._catalog_source}[/]\n"
                f"Local root: [accent]{self._image_root}[/]\n\n"
                "  [b]c[/]  Switch catalog ([b]d[/] for bty's default).\n"
                "  [b]i[/]  Install bty-server on this box.\n\n"
                "[dim]Or add entries at the source, then [b]c[/] to re-fetch.[/]"
            )
        return (
            "[b]No images yet.[/]\n\n"
            f"Local catalog: [accent]{self._image_root}[/]\n\n"
            "  [b]d[/]  Load bty's default catalog\n"
            "      (3 nosi sysdev images + bty-server).\n"
            "  [b]c[/]  Point at a different catalog\n"
            "      (path, [dim]https://[/], or [dim]oras://[/]).\n"
            "  [b]i[/]  Install bty-server on this box.\n\n"
            "Or stage [dim]*.img.gz[/] / [dim]*.qcow2[/] / [dim]*.bri[/] in\n"
            "[b]BTY_IMAGES[/] (bty-usb), [dim]bty-images/[/] on a Ventoy stick,\n"
            "or at the path above ([b]--image-root[/]); then press [b]r[/].\n\n"
            "[dim]Alt+F2 for a shell.[/]"
        )

    def _load_images(self) -> list[_TuiImage]:
        """Load the catalog from either a remote bty-web or the local
        image root, returning a unified ``_TuiImage`` list.

        In local mode, the directory scan is followed by a scan for
        ``.bri`` (bty Remote Image) descriptors; each descriptor
        becomes a ``_TuiImage`` with ``url`` set so the operator can
        flash from a URL pointer dropped into BTY_IMAGES.

        The bty-server bootstrap is NOT in this list -- it's reached
        via the ``i`` keyboard shortcut (see ``action_install_bty_
        server``) so it doesn't clutter the regular catalog and is
        always available regardless of what's been dropped on the
        BTY_IMAGES / Ventoy stick.
        """
        # Always scan the local image-root (files + .bri descriptors).
        local = [
            _TuiImage(
                name=img.name,
                fmt=img.format,
                size_bytes=img.size_bytes,
                path=img.path,
            )
            for img in images.list_images(self._image_root)
        ]
        # ``size_bytes`` on a .bri is optional; ``-1`` is the
        # unknown-size sentinel that ``_format_mib`` renders as
        # ``?`` (rather than a misleading ``0.0 MiB``). The real
        # number wins when the .bri supplies it.
        remote = [
            _TuiImage(
                name=r.name,
                fmt=r.format,
                size_bytes=r.size_bytes if r.size_bytes is not None else -1,
                url=r.url,
            )
            for r in images.list_remote_images(self._image_root)
        ]
        # Overlay the --catalog source's entries. Fetched once and
        # cached in memory; ``r``-refresh re-scans local only,
        # ``c``-switch invalidates ``_cached_remote_catalog`` to force
        # a re-fetch on the next populate.
        catalog_rows: list[_TuiImage] = []
        if self._catalog_source is not None:
            if self._cached_remote_catalog is None:
                try:
                    self._cached_remote_catalog = load_catalog_from_source(self._catalog_source)
                except (OSError, ValueError, _catalog.CatalogError) as exc:
                    # Surface in the status line but don't let a flaky
                    # catalog source block local-only operation.
                    self._set_status_transient(f"--catalog {self._catalog_source} failed: {exc}")
                    self._cached_remote_catalog = []
            catalog_rows = list(self._cached_remote_catalog)
        return local + remote + catalog_rows

    def _populate_disks(self) -> None:
        table = self.query_one("#disks_table", DataTable)
        table.clear(columns=True)
        # Trim to the columns operators actually need to make a flash
        # decision: where the disk is, how big, what kind of drive.
        # Removable / Read-only got dropped per the plan -- they're
        # binary attributes that rarely change the decision; Transport
        # already conveys the "is this a USB stick or an internal
        # disk" signal in the form most operators recognise (usb /
        # sata / nvme / sd).
        table.add_columns("Path", "Size", "Model", "Transport", "Serial")
        self._disks_by_key.clear()
        try:
            entries = disks.list_disks()
        except OSError as exc:
            self._set_status(f"Error reading disks: {exc}")
            return
        for d in entries:
            key = str(d["path"])
            self._disks_by_key[key] = d

            def _str(field: str, _disk: dict[str, object] = d) -> str:
                v = _disk.get(field)
                return v.strip() if isinstance(v, str) else ""

            model = _str("model")
            transport = _str("tran")
            serial = _str("serial")
            # ``disks.list_disks`` returns lsblk's human-readable size
            # ("500G", "8G", "1T"). Convert to MiB at display time so
            # all size cells across the TUI share one format.
            size_str = d.get("size")
            size_mib = _format_mib(_parse_size_to_bytes(str(size_str))) if size_str else "-"
            table.add_row(
                str(d["path"]),
                size_mib,
                model or "-",
                transport or "-",
                serial or "-",
                key=key,
            )

    # ---------- actions ------------------------------------------------------

    def action_refresh(self) -> None:
        self._populate_images()
        self._populate_disks()
        self._set_status_transient("Refreshed.")

    def action_install_bty_server(self) -> None:
        """``i`` binding: pre-select the bty-server bootstrap image
        and advance the wizard to disk selection.

        The bty-server image (``releases/latest/download/bty-server-
        x86_64.img.gz`` on GitHub) is not in the regular image
        catalog -- it's a built-in shortcut so a fresh-from-Ventoy
        operator with no catalog of their own can still go straight
        to a working bty-server install. From here the flow is the
        same as any other URL-backed image: pick a disk, hit Flash.

        Network constraint: flash time needs HTTPS to github.com /
        objects.githubusercontent.com. ``execute_plan`` will surface
        a clean error if reachability is missing.
        """
        self._selected_image = _TuiImage(
            name=_BTY_SERVER_LATEST_NAME,
            fmt="img.gz",
            size_bytes=-1,
            url=_BTY_SERVER_LATEST_URL,
        )
        # _post_flash carries over a stale "Reboot" button state if
        # the operator chose ``i`` right after a previous flash;
        # reset so the wizard derives Stage 2 / 3 cleanly.
        self._post_flash = False
        self._render_status()
        # Focus the disks pane so the next Enter commits the disk.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self.query_one("#disks_table", DataTable).focus()
        self._set_status_transient(
            f"Selected {_BTY_SERVER_LATEST_NAME}; pick a disk to install on."
        )

    def action_help(self) -> None:
        """``?`` binding: pop the keybinding cheat sheet.

        Plain ``push_screen`` (no ``push_screen_wait``) since we don't
        need the dismiss result -- the modal manages its own close
        via ``Esc`` / ``q`` / ``?`` bindings.
        """
        self.push_screen(HelpScreen())

    # ---------- wizard navigation -------------------------------------------

    def action_wizard_back(self) -> None:
        """``Esc`` binding: route based on app state.

        Priority order:
        1. If a filter is active, Esc clears it (legacy behavior --
           operators expect the Input's escape semantics).
        2. Otherwise undo the most recent wizard commit and return
           one stage.
        """
        if self._filter:
            self.action_clear_filter()
            return
        # Post-flash: Esc clears the success state + the disk
        # selection, returning to Stage 2 so the operator can flash
        # the same image to a different disk on the same machine.
        if self._post_flash:
            self._post_flash = False
            self._selected_disk = None
            self._render_status()
            with contextlib.suppress(Exception):
                self.query_one("#disks_table", DataTable).focus()
            return
        stage = self._stage
        if stage == _WizardStage.CONFIRM_FLASH:
            self._selected_disk = None
            self._render_status()
            with contextlib.suppress(Exception):
                self.query_one("#disks_table", DataTable).focus()
            return
        if stage == _WizardStage.SELECT_DISK:
            self._selected_image = None
            self._render_status()
            with contextlib.suppress(Exception):
                self.query_one("#images_table", DataTable).focus()
            return
        # Stage 1: nothing to undo.

    def action_reboot(self) -> None:
        """Dispatch a graceful reboot of the machine running bty-tui.

        Reached only via the action-pane button, which flips to
        "Reboot" after a successful flash (see ``on_button_pressed``).
        We deliberately do NOT bind this to a single keystroke because
        ``r``/``R`` would shoulder-rub the refresh shortcut (a real
        fat-finger trap on the live env).

        Non-root invocations get a status message; ``systemctl reboot``
        itself enforces the privilege check.
        """
        self._set_status("Rebooting...")
        try:
            subprocess.run(["systemctl", "reboot"], check=False, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._set_status(f"Reboot failed to dispatch: {exc}")

    def action_focus_filter(self) -> None:
        """Show + focus the filter input. ``/`` triggers this, helix-style."""
        try:
            filter_input = self.query_one("#filter-input", Input)
        except Exception:  # pragma: no cover - defensive during teardown
            return
        filter_input.add_class("active")
        filter_input.focus()

    def action_clear_filter(self) -> None:
        """Clear the active filter and re-populate the catalog."""
        if not self._filter:
            return
        try:
            filter_input = self.query_one("#filter-input", Input)
        except Exception:  # pragma: no cover - defensive
            return
        filter_input.value = ""
        filter_input.remove_class("active")
        self._filter = ""
        self._populate_images()
        self._set_status_transient("Filter cleared.")
        # Return focus to the catalog so navigation keys work again.
        with contextlib.suppress(Exception):
            self.query_one("#images_table", DataTable).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter-input":
            return
        self._filter = event.value.strip()
        self._populate_images()
        # Move focus back to the table so navigation keys work.
        with contextlib.suppress(Exception):
            self.query_one("#images_table", DataTable).focus()

    @property
    def _stage(self) -> _WizardStage:
        """Derived wizard stage. We never store the stage directly so
        Esc back-nav stays coherent (clearing one bit of state
        always lands the operator on the right stage).
        """
        if self._selected_image is None:
            return _WizardStage.SELECT_IMAGE
        if self._selected_disk is None:
            return _WizardStage.SELECT_DISK
        return _WizardStage.CONFIRM_FLASH

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Wizard-flow forward: Enter on a row commits and auto-advances.

        - Image row Enter -> store image, focus disks pane (if previously
          on Stage 1).
        - Disk row Enter -> store disk, focus Stage 3 segment (if
          previously on Stage 2).

        Re-committing on a re-focused table (operator pressed `1`/`2`
        to go back) updates the value but does NOT auto-advance --
        the operator stays on the table they're working with.
        """
        prev_stage = self._stage
        table_id = event.data_table.id
        if event.row_key is None or event.row_key.value is None:
            return
        key = event.row_key.value
        if table_id == "images_table":
            tui_img = self._images_by_key.get(key)
            if tui_img is None:
                return
            self._selected_image = tui_img
            self._render_status()
            if prev_stage == _WizardStage.SELECT_IMAGE:
                with contextlib.suppress(Exception):
                    self.query_one("#disks_table", DataTable).focus()
        elif table_id == "disks_table":
            disk = self._disks_by_key.get(key)
            if disk is None:
                return
            self._selected_disk = disk
            self._render_status()
            if prev_stage == _WizardStage.SELECT_DISK:
                with contextlib.suppress(Exception):
                    self.query_one("#flash-btn", Button).focus()

    def _render_status(self) -> None:
        """Refresh the action-pane state.

        Pre-flash: button reads ``Flash!``, disabled until both image
        and disk are committed. Post-flash: button reads ``Reboot``,
        always enabled (the natural next step after a successful
        flash). Pane border-title flips between "3: Flash" and
        "3: Reboot" too.
        """
        try:
            flash_btn = self.query_one("#flash-btn", Button)
            pane3 = self.query_one("#pane-3", Vertical)
        except Exception:  # pragma: no cover - defensive
            return
        if self._post_flash:
            flash_btn.label = "Reboot"
            flash_btn.disabled = False
            pane3.border_title = "  3: Reboot to use the freshly written image  "
        else:
            flash_btn.label = "Flash!"
            flash_btn.disabled = self._stage != _WizardStage.CONFIRM_FLASH
            pane3.border_title = "  3: Flash! Actually write the image!  "

    def _nav_text(self) -> str:
        """Static bottom-nav hint text. Forward navigation happens
        automatically when a row is committed via Enter; the keys
        listed here are only for actions that have no other
        natural affordance.
        """
        return (
            "<?> help       <q> quit       <r> refresh       "
            "<c> catalog       <i> install bty-server       "
            "<Esc/Backspace> back"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """The action-pane button. Pre-flash: triggers flash via
        ``action_flash`` (FlashConfirmScreen + FlashStatusScreen
        modals). Post-flash: triggers reboot. The label flips in
        ``_render_status`` so the visual matches the action.
        """
        if event.button.id == "flash-btn":
            if self._post_flash:
                self.action_reboot()
            elif self._stage == _WizardStage.CONFIRM_FLASH:
                # ``@work(exclusive=True)`` rewrites this call: at runtime
                # ``action_flash()`` returns a ``Worker`` rather than a
                # coroutine, so the result is correctly fire-and-forget
                # (the worker drives the modal sequence on its own
                # event-loop task). Pyright sees ``async def`` and
                # cannot follow the decorator's transformation.
                self.action_flash()  # pyright: ignore[reportUnusedCoroutine]

    @work(exclusive=True)
    async def action_catalog(self) -> None:
        """``c`` binding: open :class:`CatalogSelectScreen` so the
        operator can swap the catalog source (a local path or a
        URL) without restarting the TUI. ``@work`` for the
        push_screen_wait worker-context requirement (same as the
        flash + theme actions).
        """
        result = await self.push_screen_wait(
            CatalogSelectScreen(self._catalog_source, self._image_root)
        )
        if result is None:
            return
        if isinstance(result, str):
            self._catalog_source = result
            self._pxe_done_base = _pxe_done_base_from_source(result)
        else:
            self._catalog_source = None
            self._pxe_done_base = None
            self._image_root = result
        # Invalidate the cached remote catalog so the next populate
        # re-fetches from the new source.
        self._cached_remote_catalog = None
        # Update the pane-1 border-title to reflect the new source
        # and re-populate.
        if self._catalog_source is not None:
            source_label = f"{self._image_root} + {self._catalog_source}"
        else:
            source_label = str(self._image_root)
        with contextlib.suppress(Exception):
            self.query_one(
                "#pane-1", Vertical
            ).border_title = f"  1: Pick an image from {source_label}  "
        # Clear any in-flight selection since the catalog changed.
        self._selected_image = None
        self._selected_disk = None
        self._post_flash = False
        self._populate_images()
        self._render_status()
        with contextlib.suppress(Exception):
            self.query_one("#images_table", DataTable).focus()
        self._set_status_transient(f"Catalog: {source_label}")

    def action_default_catalog(self) -> None:
        """``d`` binding: swap the catalog source for bty's default
        release-asset catalog (``releases/latest/download/catalog.toml``
        on the bty repo). One keypress beats the ``c``-and-type-URL
        flow for the common "no local images, give me something to
        flash" case.

        Network constraint: the live env needs HTTPS reachability to
        github.com / objects.githubusercontent.com at fetch time --
        same as the bty-server bootstrap (``i``). Air-gapped operators
        ship their own ``.bri`` files on BTY_IMAGES instead.
        """
        self._catalog_source = _BTY_DEFAULT_CATALOG_URL
        self._pxe_done_base = _pxe_done_base_from_source(_BTY_DEFAULT_CATALOG_URL)
        # Invalidate cache so the next populate re-fetches from the
        # new source rather than serving an earlier session's rows.
        self._cached_remote_catalog = None
        source_label = f"{self._image_root} + {self._catalog_source}"
        with contextlib.suppress(Exception):
            self.query_one(
                "#pane-1", Vertical
            ).border_title = f"  1: Pick an image from {source_label}  "
        # Clear any in-flight selection since the catalog changed
        # (same as the ``c`` switch flow).
        self._selected_image = None
        self._selected_disk = None
        self._post_flash = False
        self._populate_images()
        self._render_status()
        with contextlib.suppress(Exception):
            self.query_one("#images_table", DataTable).focus()
        self._set_status_transient(f"Catalog: {_BTY_DEFAULT_CATALOG_URL} (bty default)")

    @work(exclusive=True)
    async def action_flash(self) -> None:
        # ``@work(exclusive=True)`` runs this in a worker context so
        # the ``push_screen_wait`` calls below are legal: Textual
        # 8.x rejects ``push_screen_wait`` outside a worker with
        # "screen must be from a worker when wait_for_dismiss is True".
        # ``exclusive=True`` cancels any prior in-flight flash worker
        # if the operator triggers the action again, matching the
        # single-flash-at-a-time semantics of the existing modal.
        #
        # Status pulses at each stage so the operator can see where
        # execution gets to if something silently fails (e.g. probe
        # hangs on a slow remote URL, validate_plan rejects with an
        # error displayed in the modal but the modal didn't render).
        self._set_status("Flash: triggered.")
        if os.geteuid() != 0:
            self._set_status("bty tui must run as root to flash; relaunch with sudo.")
            return

        # Prefer the wizard-flow committed selection (Enter on rows
        # populates ``_selected_image`` / ``_selected_disk``); fall
        # back to whatever the cursor is on for the ``f``-shortcut
        # path that bypasses the wizard.
        if self._selected_image is not None and self._selected_disk is not None:
            image = self._selected_image
            disk_path_str = self._selected_disk.get("path", "")
            if not isinstance(disk_path_str, str) or not disk_path_str:
                self._set_status("Flash: selected disk has no path; refresh and retry.")
                return
            disk_path = Path(disk_path_str)
        else:
            selection = self._current_selection()
            if selection is None:
                return
            image, disk_path = selection

        # ``flash.probe_image`` / ``flash.probe_target`` shell out to
        # ``qemu-img info`` / ``blockdev`` / ``lsblk`` and block. Run
        # them in a thread pool via ``asyncio.to_thread`` so the
        # event loop stays responsive, and pop a ``ProbingScreen``
        # modal so the operator sees an animated spinner during the
        # probe instead of staring at a static status line that
        # reads as a freeze.
        probing = ProbingScreen(image.name, str(disk_path))
        self.push_screen(probing)
        try:
            if image.url is not None:
                image_info = await asyncio.to_thread(flash.probe_image_url, image.url)
            else:
                assert image.path is not None  # local row guarantees a path
                image_info = await asyncio.to_thread(flash.probe_image, image.path)
        except (FileNotFoundError, ValueError) as exc:
            probing.dismiss(None)
            self._set_status(f"Image probe failed: {exc}")
            return
        probing.image_done()

        target_info = await asyncio.to_thread(flash.probe_target, disk_path)
        probing.target_done()
        plan = flash.make_plan(image_info, target_info)
        errors = flash.validate_plan(plan)

        # Brief hold so the operator sees both rows tick to
        # ``[X] done`` before the modal closes -- otherwise the
        # transition to FlashConfirmScreen feels like a flicker.
        await asyncio.sleep(0.25)
        probing.dismiss(None)

        confirmed = await self.push_screen_wait(FlashConfirmScreen(plan, errors))
        if not confirmed:
            self._set_status("Flash cancelled.")
            return

        flash_result = await self.push_screen_wait(FlashStatusScreen(plan))
        success = flash_result == "ok"
        cancelled = flash_result == "cancelled"
        if success and self._pxe_done_base is not None and self._mac is not None:
            # Catalog source was an http(s) URL; the derived base
            # might be a bty-web instance. POST the completion signal
            # so the server's ``last_flashed_at`` updates. Best-
            # effort - a failed signal (404 / non-bty-web host) is
            # logged but doesn't undo a successful flash.
            try:
                post_pxe_done(self._pxe_done_base, self._mac)
            except urllib.error.URLError as exc:
                self._set_status(f"Flash done but POST /pxe/{self._mac}/done failed: {exc}")
                self._populate_disks()
                return
        if success:
            self._set_status_transient("Flash completed.")
        elif cancelled:
            # Cancel returns the operator to the wizard with both
            # selections cleared so a follow-up flash starts from
            # Stage 1 -- the operator likely cancelled because they
            # want to pick a different image or target.
            self._selected_image = None
            self._selected_disk = None
            self._post_flash = False
            self._render_status()
            self._set_status("Flash cancelled. Pick an image to try again.")
            with contextlib.suppress(Exception):
                self.query_one("#images_table", DataTable).focus()
            self._populate_disks()
            return
        else:
            self._set_status("Flash failed; see status modal log.")
        # Disks may have new partition tables now; refresh.
        self._populate_disks()
        # On success: the action-pane button transforms into a
        # ``Reboot`` button (label + handler swap, see
        # ``_render_status`` and ``on_button_pressed``). Operator's
        # natural next step is to boot the freshly flashed disk;
        # one Enter does it.
        if success:
            self._post_flash = True
            self._render_status()
            with contextlib.suppress(Exception):
                self.query_one("#flash-btn", Button).focus()

    # ---------- helpers ------------------------------------------------------

    def _current_selection(self) -> tuple[_TuiImage, Path] | None:
        images_table = self.query_one("#images_table", DataTable)
        disks_table = self.query_one("#disks_table", DataTable)
        if images_table.row_count == 0 or disks_table.row_count == 0:
            self._set_status("Need at least one image and one disk to flash.")
            return None

        image_key = self._row_key_at(images_table)
        disk_key = self._row_key_at(disks_table)
        if image_key is None or disk_key is None:
            self._set_status("Select an image and a disk first.")
            return None

        image = self._images_by_key.get(image_key)
        if image is None:
            self._set_status("Selected image is no longer available; refresh.")
            return None

        return image, Path(disk_key)

    @staticmethod
    def _row_key_at(table: DataTable[str]) -> str | None:
        if table.cursor_row < 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except (KeyError, AttributeError):
            return None
        return row_key.value if row_key is not None else None

    def _set_status(self, message: str) -> None:
        # Any plain ``_set_status`` call invalidates the transient
        # tracking: a sticky error / state message has now taken
        # over the bottom row, and a stale auto-clear timer must
        # not wipe it out a few seconds later.
        self._transient_status = None
        with contextlib.suppress(Exception):  # pragma: no cover - defensive during teardown
            self.query_one("#status", Static).update(message)

    def _set_status_transient(self, message: str, *, delay: float = 4.0) -> None:
        """Show a status message that auto-clears after ``delay`` seconds.

        Used for post-action confirmations ("Refreshed.", "Theme: nord",
        "Flash completed.") where leaving the message up indefinitely
        clutters the bottom row. Errors keep using ``_set_status`` so
        they stay visible until the operator does something else.

        Routes through ``_set_status`` (which resets the transient
        marker to ``None``) and then sets the marker to this message
        so ``_clear_status_if`` can recognise it later. Tests that spy
        on ``_set_status`` continue to see transient messages.
        """
        self._set_status(message)
        self._transient_status = message
        self.set_timer(delay, lambda: self._clear_status_if(message))

    def _clear_status_if(self, expected: str) -> None:
        """Clear the status line iff it still matches the most recent
        transient message. If a newer message (transient or sticky)
        has replaced it, leave the line alone."""
        if self._transient_status != expected:
            return
        self._transient_status = None
        with contextlib.suppress(Exception):  # pragma: no cover - defensive during teardown
            self.query_one("#status", Static).update("")

    def _initial_status(self) -> str:
        if os.geteuid() != 0:
            return "Read-only mode (not root). Select to inspect; flashing requires sudo."
        return ""


def main() -> None:
    """Console-script entry point for ``bty-tui``."""
    BtyTui().run()
