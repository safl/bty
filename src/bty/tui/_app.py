"""bty.tui - textual terminal UI for image inspection and flashing.

Targeted at interactive use from a live environment (serial console,
SSH session, minimal recovery image). Exposes the same operations as
the ``bty`` CLI in a navigable, three-pane form (images | disks |
details), styled with the Tokyo Night theme to match the bty mascot's
navy + warm-yellow palette.

Two image-source modes:

- **Local** (default). Scans an image-root directory (USB live env's
  ``BTY_IMAGES`` partition or any local path).
- **Remote** (``--server URL``). Fetches the catalog from a running
  ``bty-web`` over HTTP; selecting an image streams it from the server
  straight to the target disk via ``flash.probe_image_url`` /
  ``execute_plan``. This is the path the TUI-on-PXE flow uses: an
  unknown MAC PXE-boots, lands at the live env in interactive mode,
  and the operator picks an image from the server's catalog without
  prior server-side configuration.

Keymap (forward navigation is automatic on Enter-to-commit;
the keys below cover everything else):

- ``Enter``       forward (commit row, trigger active button)
- ``Esc`` / ``Backspace``  back (clear most recent commit, or
                  clear filter if one is active)
- ``q``           quit
- ``r``           refresh catalogs
- ``Shift+R``     reboot the machine running bty-tui
- ``s``           switch image source (local <-> remote bty-web)
- ``t``           theme picker
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

import json
import os
import subprocess
import urllib.error
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
    OptionList,
    ProgressBar,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

import bty
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


def _format_mib(size_bytes: int) -> str:
    """Format a size in bytes as a comma-grouped MiB string.

    Used for both the images-table size column and the disk-details
    body so all size displays in the TUI share one format.
    """
    if size_bytes < 0:
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


def fetch_remote_catalog(server_url: str, *, timeout: float = 30.0) -> list[_TuiImage]:
    """``GET <server_url>/images`` and return ``_TuiImage`` rows.

    Free function so unit tests can mock ``urllib.request.urlopen``
    without instantiating a textual ``App``. Raises
    ``urllib.error.URLError`` / ``ValueError`` for surface-level
    problems; the caller (the TUI's image-pane refresh) catches and
    surfaces them in the status bar.
    """
    base = server_url.rstrip("/")
    catalog_url = f"{base}/images"
    with urllib.request.urlopen(catalog_url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"unexpected /images payload from {server_url}: not a list")
    out: list[_TuiImage] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if not name:
            continue
        out.append(
            _TuiImage(
                name=name,
                fmt=entry.get("format") or None,
                size_bytes=int(entry.get("size_bytes") or 0),
                url=f"{base}/images/{name}",
            )
        )
    return out


def post_pxe_done(server_url: str, mac: str, *, timeout: float = 10.0) -> None:
    """Best-effort ``POST <server>/pxe/{mac}/done`` after a successful
    remote flash. Silent on success; raises ``urllib.error.URLError``
    on transport failure (caller decides whether to surface)."""
    base = server_url.rstrip("/")
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

    .actions Button {
        margin-left: 2;
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
                yield Button("Cancel", id="cancel", variant="default")
                yield Button(
                    "Flash now",
                    id="confirm",
                    variant="primary",
                    disabled=bool(self._errors),
                )

    def _plan_text(self) -> str:
        plan = self._plan
        return "\n".join(
            [
                f"Image:      {plan.image.display}",
                f"Format:     {plan.image.format}",
                f"Size:       {plan.image.virtual_size_bytes} bytes (virtual)",
                f"Target:     {plan.target.path}",
                f"Target sz:  {plan.target.size_bytes} bytes",
                f"Provision:  {plan.provisioning_mode}",
            ]
        )

    def _errors_text(self) -> str:
        return "Validation FAILED:\n" + "\n".join(f"  - {e}" for e in self._errors)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class FlashStatusScreen(ModalScreen[bool]):
    """Floating modal that runs the flash in a worker and reports progress.

    Designed to feel like helix/zellij modals: bold stop-the-world
    "DO NOT REMOVE STICK" framing, a visual stage track that ticks
    as ``flash.execute_plan`` emits each lifecycle event, and a
    streaming event log below. The Close button is disabled until
    the flash either completes or fails -- the operator can't bail
    out mid-flash.

    Returns ``True`` on success, ``False`` on failure.
    """

    # Stable order of FlashProgress.event values that this modal
    # treats as "stages" with a visible row in the tracker. Anything
    # else (``provisioning``, intermediate notes) lands in the log
    # but doesn't tick a stage.
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
    """

    def __init__(self, plan: flash.FlashPlan) -> None:
        super().__init__()
        self._plan = plan
        self._result: bool | None = None
        self._completed_stages: set[str] = set()
        # Used to compute MB/s as ``writing_progress`` events arrive.
        # Set on first event; reset on each successful run.
        self._progress_start_t: float | None = None
        self._progress_start_bytes: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Flashing  "
            yield Static(
                "FLASHING - DO NOT REMOVE STICK OR DISCONNECT",
                classes="flash-warning",
            )
            yield Static(
                f"{self._plan.image.display} → {self._plan.target.path}",
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
            flash.execute_plan(self._plan, progress=on_progress)
            self.app.call_from_thread(self._mark_stage_active, "done")
            self.app.call_from_thread(self._finish, True, "[green]✓ Flash completed.[/]")
        except flash.FlashError as exc:
            self.app.call_from_thread(self._mark_stage_failed)
            self.app.call_from_thread(self._finish, False, f"[red]✗ Flash failed: {exc}[/]")

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

        ``event_name`` may be a stage we don't render (e.g.
        ``provisioning``); in that case the tracker doesn't change.
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
                    self._set_stage_class(name, "done", marker="✓", label=label)
                else:
                    self._set_stage_class(name, "active", marker="*", label=label)
                self._completed_stages.add(name)
            elif not seen_current:
                # Earlier stage; mark done if not already.
                self._set_stage_class(name, "done", marker="✓", label=label)
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
                self._set_stage_class(name, "failed", marker="✗", label=label)
                return

    def _set_stage_class(self, name: str, state: str, *, marker: str, label: str) -> None:
        try:
            widget = self.query_one(f"#stage-{name}", Static)
        except Exception:  # pragma: no cover - defensive
            return
        widget.update(f"[{marker}] {label}")
        widget.set_classes(f"flash-stage {state}")

    def _finish(self, success: bool, message: str) -> None:
        log = self.query_one(RichLog)
        log.write(message)
        self._result = success
        close_btn = self.query_one("#close", Button)
        close_btn.disabled = False
        close_btn.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(self._result is True)


class SourceSelectScreen(ModalScreen["str | Path | None"]):
    """Modal for switching between local image-root and remote
    bty-web server as the catalog source.

    Returns:
    - ``str`` (server URL) when the operator picks Remote and confirms.
    - ``Path`` (local image root) when they pick Local + confirm.
    - ``None`` on Esc.

    The current source is pre-filled in the input. Apply re-populates
    pane-1 and updates its border-title via the caller.
    """

    DEFAULT_CSS = """
    SourceSelectScreen {
        align: center middle;
    }

    SourceSelectScreen > Vertical {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    SourceSelectScreen Input {
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
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, current_server: str | None, current_image_root: Path) -> None:
        super().__init__()
        self._current_server = current_server
        self._current_image_root = current_image_root

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Switch image source  "
            yield Static("Server URL (blank = local image-root):")
            initial = self._current_server if self._current_server else ""
            yield Input(
                value=initial,
                placeholder="http://server:8080",
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


class ThemeSelectScreen(ModalScreen[str | None]):
    """Modal showing the available Textual themes; Enter applies, Esc dismisses.

    Triggered from ``BtyTui.action_theme`` (``t`` binding). The
    list comes from ``App.available_themes`` (Textual's built-in
    catalog: textual-dark, textual-light, nord, gruvbox,
    catppuccin-mocha, dracula, tokyo-night, monokai). The
    currently active theme is pre-highlighted so the operator
    can confirm or change with arrow keys + Enter; Esc bails
    without changing anything.

    Returns the selected theme name on Enter, or ``None`` on Esc.
    """

    DEFAULT_CSS = """
    ThemeSelectScreen {
        align: center middle;
    }

    ThemeSelectScreen > Vertical {
        width: 50;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $panel;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
        border-title-align: left;
    }

    ThemeSelectScreen OptionList {
        height: auto;
        max-height: 20;
        background: transparent;
        border: none;
    }

    .theme-help {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("enter", "apply", "Apply"),
    ]

    def __init__(self, current_theme: str, available: list[str]) -> None:
        super().__init__()
        self._current = current_theme
        self._available = sorted(available)

    def compose(self) -> ComposeResult:
        with Vertical() as panel:
            panel.border_title = "  Select theme  "
            options = [
                Option(
                    f"  {name}{'  *' if name == self._current else ''}",
                    id=name,
                )
                for name in self._available
            ]
            yield OptionList(*options, id="theme-list")
            yield Static("Enter to apply, Esc to cancel", classes="theme-help")

    def on_mount(self) -> None:
        # Pre-highlight the active theme so the operator sees what's
        # in effect without scrolling.
        ol = self.query_one("#theme-list", OptionList)
        try:
            idx = self._available.index(self._current)
            ol.highlighted = idx
        except ValueError:
            pass
        ol.focus()

    def action_apply(self) -> None:
        ol = self.query_one("#theme-list", OptionList)
        if ol.highlighted is None:
            self.dismiss(None)
            return
        self.dismiss(self._available[ol.highlighted])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Pressing Enter on a highlighted row also applies; this fires
        # in addition to ``action_apply`` for explicit Enter, but
        # ``ModalScreen.dismiss`` is idempotent so the second call is
        # a no-op once the screen is gone.
        if event.option.id is not None:
            self.dismiss(event.option.id)


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
        Binding("R", "reboot", "Reboot", show=False),
        Binding("t", "theme", "Theme", show=False),
        Binding("s", "source", "Source", show=False),
        Binding("slash", "focus_filter", "Filter", show=False),
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

    /* Focused pane: border + interior both highlight, so the whole
       pane reads as the active region rather than just the border
       outline. The 1-char padding (above) gives the highlight room
       to wrap around the content. */
    .pane:focus-within {
        border: round $accent;
        border-title-color: $accent;
        background: $boost;
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
       is the operator's commit handle. */
    .action-button {
        width: 24;
        height: 3;
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
        server_url: str | None = None,
        mac: str | None = None,
    ) -> None:
        super().__init__()
        self._server_url: str | None = server_url.rstrip("/") if server_url else None
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
        # Operators can swap themes at runtime via the ``t`` binding
        # (ThemeSelectScreen).
        self.theme = "tokyo-night"
        self.sub_title = bty.__version__
        # Border-title labels on each pane (harlequin / posting style:
        # the title sits in the top-border, not as a separate Static
        # row, so the panel feels like one piece). The images label
        # carries the source so the operator can see where the catalog
        # is coming from at a glance.
        source = (
            f"{self._server_url}/images" if self._server_url is not None else str(self._image_root)
        )
        self.query_one("#pane-1", Vertical).border_title = f"  1: Pick an image from {source}  "
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
        try:
            self.query_one("#images_table", DataTable).focus()
        except Exception:  # pragma: no cover - defensive
            pass

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
            source = self._server_url or str(self._image_root)
            self._set_status(f"No images at {source}; press R to refresh.")
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

        Differs by source so the operator gets actionable next steps:
        local catalog -> "drop images onto BTY_IMAGES from a host OS";
        remote catalog -> "the server has no images; upload via the
        web UI or PUT /images".
        """
        if self._server_url is not None:
            return (
                "[b]No images on the server yet.[/]\n\n"
                f"Catalog endpoint: [accent]{self._server_url}/images[/]\n\n"
                "Upload via the bty-web Images page in your browser, or PUT\n"
                "an image directly:\n"
                "  [dim]curl -X PUT --upload-file my.qcow2 \\\n"
                "       http://server:8080/images/my.qcow2[/]\n\n"
                "Then press [b]r[/] in this TUI to refresh."
            )
        return (
            "[b]No images in the catalog yet.[/]\n\n"
            f"Local catalog: [accent]{self._image_root}[/]\n\n"
            "On the bty USB stick, this directory is the BTY_IMAGES exFAT\n"
            "partition. Drop your cooked images onto it from any host OS\n"
            "(Linux / macOS / Windows all read exFAT):\n\n"
            "  [dim]cp my-image.img.zst /path/to/BTY_IMAGES/[/]\n"
            "  [dim]cp my-image.qcow2  /path/to/BTY_IMAGES/[/]\n\n"
            "Then press [b]r[/] in this TUI to refresh."
        )

    def _load_images(self) -> list[_TuiImage]:
        """Load the catalog from either a remote bty-web or the local
        image root, returning a unified ``_TuiImage`` list."""
        if self._server_url is not None:
            return fetch_remote_catalog(self._server_url)
        return [
            _TuiImage(
                name=img.name,
                fmt=img.format,
                size_bytes=img.size_bytes,
                path=img.path,
            )
            for img in images.list_images(self._image_root)
        ]

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
        self._set_status("Refreshed.")

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
            try:
                self.query_one("#disks_table", DataTable).focus()
            except Exception:
                pass
            return
        stage = self._stage
        if stage == _WizardStage.CONFIRM_FLASH:
            self._selected_disk = None
            self._render_status()
            try:
                self.query_one("#disks_table", DataTable).focus()
            except Exception:
                pass
            return
        if stage == _WizardStage.SELECT_DISK:
            self._selected_image = None
            self._render_status()
            try:
                self.query_one("#images_table", DataTable).focus()
            except Exception:
                pass
            return
        # Stage 1: nothing to undo.

    def action_reboot(self) -> None:
        """``Shift+R`` binding: dispatch a graceful reboot of the
        machine running bty-tui. Always available (the operator may
        be running bty-tui on the same box they want to reboot --
        e.g. the bty-usb live env after a flash). Non-root invocations
        get a status message; ``systemctl reboot`` itself enforces the
        privilege check.
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
        except Exception:
            return
        filter_input.value = ""
        filter_input.remove_class("active")
        self._filter = ""
        self._populate_images()
        self._set_status("Filter cleared.")
        # Return focus to the catalog so navigation keys work again.
        try:
            self.query_one("#images_table", DataTable).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter-input":
            return
        self._filter = event.value.strip()
        self._populate_images()
        # Move focus back to the table so navigation keys work.
        try:
            self.query_one("#images_table", DataTable).focus()
        except Exception:
            pass

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
                try:
                    self.query_one("#disks_table", DataTable).focus()
                except Exception:
                    pass
        elif table_id == "disks_table":
            disk = self._disks_by_key.get(key)
            if disk is None:
                return
            self._selected_disk = disk
            self._render_status()
            if prev_stage == _WizardStage.SELECT_DISK:
                try:
                    self.query_one("#flash-btn", Button).focus()
                except Exception:
                    pass

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
        except Exception:
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
            "<q> quit       <Shift+R> reboot       <s> source       "
            "<t> theme       <Esc/Backspace> back"
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
                self.action_flash()  # @work decorator -> Worker

    @work(exclusive=True)
    async def action_source(self) -> None:
        """Open the SourceSelectScreen modal so the operator can
        switch between local image-root and a remote bty-web server
        without restarting the TUI. ``@work`` for the
        push_screen_wait worker-context requirement (same as the
        flash + theme actions).
        """
        result = await self.push_screen_wait(SourceSelectScreen(self._server_url, self._image_root))
        if result is None:
            return
        if isinstance(result, str):
            self._server_url = result.rstrip("/")
        else:
            self._server_url = None
            self._image_root = result
        # Update the pane-1 border-title to reflect the new source
        # and re-populate.
        source = (
            f"{self._server_url}/images" if self._server_url is not None else str(self._image_root)
        )
        try:
            self.query_one("#pane-1", Vertical).border_title = f"  1: Pick an image from {source}  "
        except Exception:
            pass
        # Clear any in-flight selection since the catalog changed.
        self._selected_image = None
        self._selected_disk = None
        self._post_flash = False
        self._populate_images()
        self._render_status()
        try:
            self.query_one("#images_table", DataTable).focus()
        except Exception:
            pass
        self._set_status(f"Source: {source}")

    @work(exclusive=True)
    async def action_theme(self) -> None:
        """Open the theme picker; apply the selected theme on dismiss.

        ``@work(exclusive=True)`` for the same reason as
        ``action_flash`` -- ``push_screen_wait`` requires worker
        context. Operators have asked for theme switching at
        runtime since the default Tokyo Night palette doesn't
        suit every hardware terminal; the picker lists every
        theme Textual ships and applies on Enter / dismisses
        without change on Esc.
        """
        available = list(self.available_themes.keys())
        selected = await self.push_screen_wait(ThemeSelectScreen(self.theme, available))
        if selected is not None and selected != self.theme:
            self.theme = selected
            self._set_status(f"Theme: {selected}")

    @work(exclusive=True)
    async def action_flash(self) -> None:
        # ``@work(exclusive=True)`` runs this in a worker context so
        # the ``push_screen_wait`` calls below are legal: Textual
        # 8.x rejects ``push_screen_wait`` outside a worker with
        # "screen must be from a worker when wait_for_dismiss is True".
        # ``exclusive=True`` cancels any prior in-flight flash worker
        # if the operator triggers the action again, matching the
        # single-flash-at-a-time semantics of the existing modal.
        if os.geteuid() != 0:
            self._set_status("bty-tui must run as root to flash; relaunch with sudo.")
            return

        # Prefer the wizard-flow committed selection (Enter on rows
        # populates ``_selected_image`` / ``_selected_disk``); fall
        # back to whatever the cursor is on for the ``f``-shortcut
        # path that bypasses the wizard.
        if self._selected_image is not None and self._selected_disk is not None:
            image = self._selected_image
            disk_path_str = self._selected_disk.get("path", "")
            if not isinstance(disk_path_str, str) or not disk_path_str:
                return
            disk_path = Path(disk_path_str)
        else:
            selection = self._current_selection()
            if selection is None:
                return
            image, disk_path = selection

        try:
            if image.url is not None:
                image_info = flash.probe_image_url(image.url)
            else:
                assert image.path is not None  # local row guarantees a path
                image_info = flash.probe_image(image.path)
        except (FileNotFoundError, ValueError) as exc:
            self._set_status(f"Image probe failed: {exc}")
            return

        target_info = flash.probe_target(disk_path)
        plan = flash.make_plan(image_info, target_info, "none")
        errors = flash.validate_plan(plan)

        confirmed = await self.push_screen_wait(FlashConfirmScreen(plan, errors))
        if not confirmed:
            self._set_status("Flash cancelled.")
            return

        success = await self.push_screen_wait(FlashStatusScreen(plan))
        if success and self._server_url is not None and self._mac is not None:
            # Remote flow: signal completion so the server's
            # ``last_flashed_at`` is updated. Best-effort - a failed
            # signal doesn't undo a successful flash.
            try:
                post_pxe_done(self._server_url, self._mac)
            except urllib.error.URLError as exc:
                self._set_status(f"Flash done but POST /pxe/{self._mac}/done failed: {exc}")
                self._populate_disks()
                return
        self._set_status("Flash completed." if success else "Flash failed; see status modal log.")
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
            try:
                self.query_one("#flash-btn", Button).focus()
            except Exception:
                pass

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
        try:
            self.query_one("#status", Static).update(message)
        except Exception:  # pragma: no cover - defensive during teardown
            pass

    def _initial_status(self) -> str:
        if os.geteuid() != 0:
            return "Read-only mode (not root). Select to inspect; flashing requires sudo."
        return "Select an image and a disk; press F to flash."


def main() -> None:
    """Console-script entry point for ``bty-tui``."""
    BtyTui().run()
