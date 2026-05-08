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

Keymap (single-key direct bindings; no modifier-key prefixes, no
modal navigation -- bty has so few actions that the helix-style
``space``-prefix is overkill):

- ``q``   quit
- ``r``   refresh catalogs
- ``f``   flash the highlighted image to the highlighted disk
- ``/``   filter the image catalog by substring (helix-style)
- ``escape`` clear the active filter

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
import urllib.error
import urllib.request
from dataclasses import dataclass
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
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

import bty
from bty import disks, flash, images


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

    FlashConfirmScreen > Vertical {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    .header {
        height: 1;
        background: $primary;
        color: auto;
        text-align: center;
    }

    .errors {
        color: $error;
        margin: 1 0;
    }

    .actions {
        height: 3;
        align: right middle;
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
        with Vertical():
            yield Static("Flash plan", classes="header")
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
        background: $surface;
        border: thick $warning;
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
        with Vertical():
            yield Static(
                "FLASHING — DO NOT REMOVE STICK OR DISCONNECT",
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
        Binding("r", "refresh", "Refresh"),
        Binding("f", "flash", "Flash"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    DEFAULT_CSS = """
    Screen {
        layout: vertical;
    }

    #panes {
        height: 1fr;
        layout: horizontal;
    }

    .pane {
        layout: vertical;
        border: tall $primary;
    }

    #images-pane, #disks-pane {
        width: 2fr;
    }

    #details-pane {
        width: 3fr;
    }

    .pane-title {
        height: 1;
        background: $primary;
        color: auto;
        text-align: center;
    }

    DataTable {
        height: 1fr;
    }

    #filter-input {
        height: 3;
        margin: 0;
        border: none;
        background: $surface;
        display: none;
    }

    #filter-input.active {
        display: block;
    }

    #welcome {
        height: auto;
        padding: 1 2;
        color: $text-muted;
    }

    #details-body {
        height: 1fr;
        padding: 1 2;
    }

    #status {
        height: 1;
        padding: 0 1;
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        source_label = (
            f"Images @ {self._server_url}/images"
            if self._server_url is not None
            else f"Images @ {self._image_root}"
        )
        with Horizontal(id="panes"):
            with Vertical(classes="pane", id="images-pane"):
                yield Static(source_label, classes="pane-title")
                yield Input(
                    placeholder="filter (substring match on name)",
                    id="filter-input",
                )
                yield DataTable(id="images_table", cursor_type="row")
                yield Static("", id="welcome")
            with Vertical(classes="pane", id="disks-pane"):
                yield Static("Disks", classes="pane-title")
                yield DataTable(id="disks_table", cursor_type="row")
            with Vertical(classes="pane", id="details-pane"):
                yield Static("Details", classes="pane-title")
                yield Static("(select an image or disk)", id="details-body")
        yield Static(self._initial_status(), id="status")
        yield Footer()

    def on_mount(self) -> None:
        # Tokyo Night picks up the navy + warm-yellow palette of the
        # bty mascot (saturated cool background, yellow accents).
        self.theme = "tokyo-night"
        self.sub_title = bty.__version__
        self._populate_images()
        self._populate_disks()
        # Focus the images table so global key bindings (q/r/f/...)
        # fire instead of being eaten by the filter Input. The Input
        # only takes focus when the operator explicitly presses ``/``.
        try:
            self.query_one("#images_table", DataTable).focus()
        except Exception:  # pragma: no cover - defensive
            pass

    # ---------- data refresh ------------------------------------------------

    def _populate_images(self) -> None:
        table = self.query_one("#images_table", DataTable)
        welcome = self.query_one("#welcome", Static)
        table.clear(columns=True)
        table.add_columns("Name", "Format", "Size (B)")
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
                str(tui_img.size_bytes),
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
        table.add_columns("Path", "Size", "Model")
        self._disks_by_key.clear()
        try:
            entries = disks.list_disks()
        except OSError as exc:
            self._set_status(f"Error reading disks: {exc}")
            return
        for d in entries:
            key = str(d["path"])
            self._disks_by_key[key] = d
            model = (d.get("model") or "").strip() if isinstance(d.get("model"), str) else ""
            size = d.get("size") or ""
            table.add_row(str(d["path"]), str(size), model, key=key)

    # ---------- actions ------------------------------------------------------

    def action_refresh(self) -> None:
        self._populate_images()
        self._populate_disks()
        self._set_status("Refreshed.")

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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update the details pane when the operator moves the cursor."""
        table_id = event.data_table.id
        if event.row_key is None or event.row_key.value is None:
            return
        key = event.row_key.value
        if table_id == "images_table":
            tui_img = self._images_by_key.get(key)
            if tui_img is not None:
                self._show_image_details(tui_img)
        elif table_id == "disks_table":
            disk = self._disks_by_key.get(key)
            if disk is not None:
                self._show_disk_details(disk)

    def _show_image_details(self, tui_img: _TuiImage) -> None:
        try:
            body = self.query_one("#details-body", Static)
        except Exception:
            return
        lines = [
            "[b]Image[/]",
            f"  Name:    {tui_img.name}",
            f"  Format:  {tui_img.fmt or '?'}",
            f"  Size:    {tui_img.size_bytes:,} bytes ({tui_img.size_bytes / (1 << 30):.2f} GiB)",
        ]
        if tui_img.url is not None:
            lines.append(f"  Source:  remote ({tui_img.url})")
        else:
            lines.append(f"  Source:  local ({tui_img.path})")
        body.update("\n".join(lines))

    def _show_disk_details(self, disk: dict[str, object]) -> None:
        try:
            body = self.query_one("#details-body", Static)
        except Exception:
            return

        def _str(key: str) -> str:
            v = disk.get(key)
            return v.strip() if isinstance(v, str) else ""

        path = _str("path") or "?"
        size = _str("size") or "?"
        model = _str("model")
        vendor = _str("vendor")
        tran = _str("tran")
        serial = _str("serial")
        readonly = disk.get("readonly", False)
        removable = disk.get("removable", False)
        lines = [
            "[b]Disk[/]",
            f"  Path:      {path}",
            f"  Size:      {size}",
            f"  Vendor:    {vendor or '-'}",
            f"  Model:     {model or '-'}",
            f"  Transport: {tran or '-'}",
            f"  Serial:    {serial or '-'}",
            f"  Removable: {removable}",
            f"  Read-only: {readonly}",
        ]
        body.update("\n".join(lines))

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
