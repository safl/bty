"""bty.tui — textual terminal UI for image inspection and flashing.

Targeted at interactive use from a live environment (serial console, SSH
session, minimal recovery image). Exposes the same operations as the
``bty`` CLI in a navigable, two-pane form.

Requires the ``[tui]`` install extra (pulls in textual).
"""

from __future__ import annotations

import os
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
    RichLog,
    Static,
)

import bty
from bty import disks, flash, images


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
                f"Image:      {plan.image.path}",
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
    """Modal that runs the flash in a worker and reports the result.

    Returns ``True`` on success, ``False`` on failure. Operator can close
    with the Close button once the run completes.
    """

    DEFAULT_CSS = """
    FlashStatusScreen {
        align: center middle;
    }

    FlashStatusScreen > Vertical {
        width: 80;
        height: 22;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    RichLog {
        height: 1fr;
    }
    """

    def __init__(self, plan: flash.FlashPlan) -> None:
        super().__init__()
        self._plan = plan
        self._result: bool | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Flashing {self._plan.image.path} -> {self._plan.target.path}")
            yield RichLog(highlight=False, markup=True, id="flash_log")
            yield Button("Close", id="close", variant="default", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("Starting flash...")
        self._run_flash()

    @work(thread=True, exclusive=True)
    def _run_flash(self) -> None:
        def on_progress(event: flash.FlashProgress) -> None:
            line = f"[{event.event}]"
            if event.note:
                line += f" {event.note}"
            if event.total_bytes is not None:
                line += f" total_bytes={event.total_bytes}"
            self.app.call_from_thread(self._append_log, line)

        try:
            flash.execute_plan(self._plan, progress=on_progress)
            self.app.call_from_thread(self._finish, True, "[green]✓ Flash completed.[/]")
        except flash.FlashError as exc:
            self.app.call_from_thread(self._finish, False, f"[red]✗ Flash failed: {exc}[/]")

    def _append_log(self, line: str) -> None:
        self.query_one(RichLog).write(line)

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
    """The bty terminal UI."""

    TITLE = "bty"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "flash", "Flash"),
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
        width: 1fr;
        layout: vertical;
        border: tall $primary;
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

    #status {
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, image_root: Path | None = None) -> None:
        super().__init__()
        self._image_root: Path = image_root or images.default_image_root()
        self._images_by_key: dict[str, images.Image] = {}
        self._disks_by_key: dict[str, dict[str, object]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="panes"):
            with Vertical(classes="pane"):
                yield Static(f"Images @ {self._image_root}", classes="pane-title")
                yield DataTable(id="images_table", cursor_type="row")
            with Vertical(classes="pane"):
                yield Static("Disks", classes="pane-title")
                yield DataTable(id="disks_table", cursor_type="row")
        yield Static(self._initial_status(), id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = bty.__version__
        self._populate_images()
        self._populate_disks()

    # ---------- data refresh ------------------------------------------------

    def _populate_images(self) -> None:
        table = self.query_one("#images_table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Format", "Size (B)")
        self._images_by_key.clear()
        try:
            entries = images.list_images(self._image_root)
        except OSError as exc:
            self._set_status(f"Error reading images: {exc}")
            return
        if not entries:
            self._set_status(f"No images under {self._image_root}; press R to refresh.")
            return
        for img in entries:
            key = str(img.path)
            self._images_by_key[key] = img
            table.add_row(img.name, img.format or "?", str(img.size_bytes), key=key)

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

    async def action_flash(self) -> None:
        if os.geteuid() != 0:
            self._set_status("bty-tui must run as root to flash; relaunch with sudo.")
            return

        selection = self._current_selection()
        if selection is None:
            return
        image, disk_path = selection

        try:
            image_info = flash.probe_image(image.path)
        except FileNotFoundError as exc:
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
        self._set_status("Flash completed." if success else "Flash failed; see status modal log.")
        # Disks may have new partition tables now; refresh.
        self._populate_disks()

    # ---------- helpers ------------------------------------------------------

    def _current_selection(self) -> tuple[images.Image, Path] | None:
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
        except Exception:  # pragma: no cover — defensive during teardown
            pass

    def _initial_status(self) -> str:
        if os.geteuid() != 0:
            return "Read-only mode (not root). Select to inspect; flashing requires sudo."
        return "Select an image and a disk; press F to flash."


def main() -> None:
    """Console-script entry point for ``bty-tui``."""
    BtyTui().run()
