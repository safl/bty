"""bty.tui - terminal UI for image inspection and flashing.

Rich-based. No Textual. No event loop. No alt-screen.

Each "screen" is a sequence of Rich-rendered Panels followed by a
``Prompt.ask`` input. Boring, stable, fast: the screen draws once
(~30-100ms on a kernel framebuffer console), the operator types a
choice, the next screen draws. No reactive properties, no compose
tree, no CSS cascading, no DataTable layout passes.

The wizard flow is a plain Python ``while True`` loop dispatching
on the current ``_WizardStage``. Esc-back-nav is the literal
``b`` / ``back`` token returned from the prompt; Enter-to-advance
is the number the operator types.

Performance design notes:

  * Rich prints are synchronous one-shot writes -- no per-frame
    diffing, no allocation of intermediate render trees.
  * The only Live-update region is the flash-progress bar, and
    even that is bounded: one update per second from the
    ``FlashProgress`` callback, no more.
  * Lists are static tables rendered once. Filter? The operator
    reads the list; on framebuffer console with <30 entries the
    cost of "live filter" isn't worth its complexity.
  * No modal overlays: "confirm before flash" is a panel printed
    inline, followed by a y/N prompt. Visually identical to the
    operator (one focused question at a time); zero alt-screen
    overhead.

Catalog sources (same as the old Textual UI):

  * Local image-root (always scanned).
  * Optional ``--catalog SOURCE`` overlay (local TOML, http(s),
    or oras://).

PXE-interactive use: ``--catalog http://bty-server:8080/catalog.toml``
plus ``--mac <MAC>`` so the TUI POSTs ``/pxe/<mac>/done`` after a
successful flash (derived from the catalog URL's scheme+host).

Public surface preserved from the prior Textual implementation:

  * ``BtyTui`` class with ``run()``.
  * ``_TuiImage`` dataclass (catalog row shape).
  * ``load_catalog_from_source(...)``, ``_pxe_done_base_from_source(...)``,
    ``post_pxe_done(...)``, ``post_inventory(...)`` helpers.
  * ``_format_mib``, ``_parse_size_to_bytes`` formatters.
  * ``_WizardStage`` enum.

This module no longer imports textual; pure-rich + stdlib.
"""

from __future__ import annotations

import contextlib
import json as _json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Prompt
from rich.table import Table

import bty
from bty import catalog as _catalog
from bty import disks, flash, images

# ---------------------------------------------------------------------------
# Public-API helpers (preserved across the textual -> rich rewrite so
# external callers + the test suite's model layer don't have to change).
# ---------------------------------------------------------------------------


class _WizardStage(IntEnum):
    """The four wizard stages, derived from selection state.

    Forward advance: an operator commit (Enter on an image / disk
    / confirm) sets the corresponding state field, which flips the
    derived stage. Esc / ``b`` back-nav clears the most-recent
    commit, dropping the stage by one.
    """

    SELECT_IMAGE = 1
    SELECT_DISK = 2
    CONFIRM_FLASH = 3
    REBOOT_OR_DONE = 4


@dataclass
class _TuiImage:
    """Unified catalog row.

    Either ``path`` (local file) or ``url`` (remote / oras / .bri
    pointer) is populated. The rest of the TUI consumes this
    shape uniformly so local + remote sources blend into one list.
    """

    name: str
    fmt: str | None
    size_bytes: int
    path: Path | None = None
    url: str | None = None


def _format_mib(size_bytes: int | None) -> str:
    """Format a byte count as comma-grouped MiB.

    Negative / None render as ``?`` so a probe that couldn't
    determine a virtual size (e.g. a streamed raw URL whose
    Content-Length the server didn't advertise) shows a clean
    placeholder rather than crashing the prompt.
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
    """Parse an lsblk-style human-readable size (``500G``, ``1.5T``)
    to bytes. Empty / unrecognised input returns 0 (caller can
    render as ``?``).
    """
    s = s.strip().upper()
    if not s:
        return 0
    if s[-1] in _SIZE_SUFFIX_MULTIPLIERS:
        try:
            n = float(s[:-1])
        except ValueError:
            return 0
        return int(n * _SIZE_SUFFIX_MULTIPLIERS[s[-1]])
    try:
        return int(s)
    except ValueError:
        return 0


def load_catalog_from_source(source: str, *, timeout: float = 30.0) -> list[_TuiImage]:
    """Load catalog rows from a path / URL into the TUI shape.

    Thin projection over :func:`bty.catalog.load_source`. Same
    accepted sources as before: local TOML path, http(s):// URL,
    oras:// reference.
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
    ``--catalog`` source. Returns ``None`` when the source isn't
    an http(s) URL.
    """
    if source is None:
        return None
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def post_pxe_done(pxe_done_base: str, mac: str, *, timeout: float = 10.0) -> None:
    """POST ``<pxe_done_base>/pxe/{mac}/done``. Silent on success;
    raises ``urllib.error.URLError`` on transport failure (caller
    decides whether to surface).
    """
    base = pxe_done_base.rstrip("/")
    req = urllib.request.Request(f"{base}/pxe/{mac}/done", method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def post_inventory(
    pxe_done_base: str,
    mac: str,
    disks_payload: list[dict[str, object]],
    *,
    timeout: float = 10.0,
) -> None:
    """POST ``<pxe_done_base>/pxe/{mac}/inventory`` with the live
    env's local disk inventory.
    """
    base = pxe_done_base.rstrip("/")
    body = _json.dumps({"disks": disks_payload}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/pxe/{mac}/inventory",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


_BTY_SERVER_LATEST_URL = (
    "https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz"
)
_BTY_SERVER_LATEST_NAME = "bty-server (latest from GitHub)"

_BTY_DEFAULT_CATALOG_URL = "https://github.com/safl/bty/releases/latest/download/catalog.toml"


# ---------------------------------------------------------------------------
# Rendering style: blue/gray dominant, with a sparing dash of
# muted yellow. Pure-text fallback works on the framebuffer
# console too; the colour bytes are ANSI escapes the kernel
# terminal driver understands. Names are kept in the 16-colour
# set so the look is identical across SSH, serial, and the live
# env's framebuffer tty1 (where any 256-colour mapping collapses
# to its nearest neighbour and the design intent gets lost).
# ---------------------------------------------------------------------------

_PRIMARY = "blue"  # dominant -- headers, table titles, primary columns
_MUTED = "bright_black"  # secondary -- byline columns, parenthesised hints. Canonical
# 16-colour ANSI gray with no 256-colour tint (grey62 read as
# teal-ish on dark dev terminals); renders identically across SSH,
# serial, and the live env's framebuffer.
_ACCENT = "yellow"  # the dash: row indices + prompts + stage breadcrumb only
_DANGER = "red"
_OK = "green"
# Very dark grey for subtle zebra striping. On 256-colour terminals
# (SSH, dev consoles) renders as a faint band; on the live env's
# 16-colour framebuffer it down-converts to black and disappears,
# which is the desired behaviour -- the stripe is a nicety on
# capable terminals, not a feature anyone should depend on.
_STRIPE = "grey11"


# ---------------------------------------------------------------------------
# Wizard state. Tiny dataclass; no reactive properties, just fields.
# ---------------------------------------------------------------------------


@dataclass
class _State:
    image_root: Path
    catalog_source: str | None = None
    mac: str | None = None
    pxe_done_base: str | None = None

    selected_image: _TuiImage | None = None
    selected_disk: dict[str, Any] | None = None
    post_flash: bool = False

    # Cached lists; refreshed on demand.
    _images: list[_TuiImage] = field(default_factory=list)
    _disks: list[dict[str, Any]] = field(default_factory=list)

    def stage(self) -> _WizardStage:
        if self.post_flash:
            return _WizardStage.REBOOT_OR_DONE
        if self.selected_image is None:
            return _WizardStage.SELECT_IMAGE
        if self.selected_disk is None:
            return _WizardStage.SELECT_DISK
        return _WizardStage.CONFIRM_FLASH

    def back(self) -> None:
        """Clear the most-recent commit. Esc / ``b`` from a prompt
        calls this. Stage 1 -> no-op (already at the top).
        """
        if self.post_flash:
            self.post_flash = False
            self.selected_disk = None
            return
        if self.selected_disk is not None:
            self.selected_disk = None
            return
        if self.selected_image is not None:
            self.selected_image = None
            return
        # Stage 1: no-op.


# ---------------------------------------------------------------------------
# Local-side enumeration: images + disks. Pure functions that build
# the TUI shape. The catalog overlay is loaded separately via
# ``load_catalog_from_source``.
# ---------------------------------------------------------------------------


def _list_local_images(image_root: Path) -> list[_TuiImage]:
    """Local image-root scan -> TUI rows. ``.bri`` descriptors
    surface as remote rows (url-bearing) so they flash through the
    same URL pipeline as catalog entries.
    """
    if not image_root.exists() or not image_root.is_dir():
        return []
    out: list[_TuiImage] = [
        _TuiImage(
            name=img.name,
            fmt=img.format,
            size_bytes=img.size_bytes or 0,
            path=img.path,
        )
        for img in images.list_images(image_root)
    ]
    out.extend(
        _TuiImage(
            name=bri.name,
            fmt=bri.format,
            size_bytes=bri.size_bytes or 0,
            url=bri.url,
        )
        for bri in images.list_remote_images(image_root)
    )
    return out


def _list_disks() -> list[dict[str, Any]]:
    """Disk inventory via ``disks.list_disks``. Filters down to
    flash-eligible candidates: must be a block device, not
    read-only, not a loop / ram device.
    """
    try:
        all_disks = disks.list_disks()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    return [d for d in all_disks if d.get("type") == "disk" and not d.get("ro")]


# ---------------------------------------------------------------------------
# The TUI itself. Sequential-screen wizard driven by a plain loop.
# ---------------------------------------------------------------------------


class BtyTui:
    """The bty terminal UI -- Rich-based, no event loop.

    ``run()`` is the entry point. The wizard advances through four
    stages (SELECT_IMAGE, SELECT_DISK, CONFIRM_FLASH,
    REBOOT_OR_DONE) until the operator quits.

    Each screen is a method that renders + prompts + returns a
    string token. The dispatcher uses the token to advance, back,
    quit, refresh, or switch catalog source.
    """

    def __init__(
        self,
        image_root: Path | None = None,
        catalog_source: str | None = None,
        mac: str | None = None,
    ) -> None:
        self._console = Console(highlight=False)
        env_image_root = os.environ.get("BTY_IMAGE_ROOT")
        resolved_root = image_root or (
            Path(env_image_root) if env_image_root else Path("/var/lib/bty/images")
        )
        self._state = _State(
            image_root=resolved_root,
            catalog_source=catalog_source,
            mac=mac,
            pxe_done_base=_pxe_done_base_from_source(catalog_source),
        )
        # Catalog load errors (transient network / bad TOML) -- surface
        # via a soft banner on the image-pick screen rather than
        # aborting the TUI.
        self._catalog_load_error: str | None = None

    # ---------- entry --------------------------------------------------

    def run(self) -> None:
        """Drive the wizard until the operator quits."""
        # Best-effort inventory post at startup. Network failures are
        # non-fatal; the TUI is still useful even if bty-web can't be
        # reached.
        if self._state.pxe_done_base and self._state.mac:
            self._auto_post_inventory()

        try:
            self._main_loop()
        except KeyboardInterrupt:
            self._console.print()
            self._console.print(
                f"[{_MUTED}]Interrupted -- exiting.[/]",
            )
        except SystemExit:
            raise
        except Exception:  # pragma: no cover - last-resort safety net
            self._console.print_exception(show_locals=False)
            sys.exit(1)

    # ---------- main loop ----------------------------------------------

    def _main_loop(self) -> None:
        while True:
            stage = self._state.stage()
            if stage is _WizardStage.SELECT_IMAGE:
                action = self._screen_select_image()
            elif stage is _WizardStage.SELECT_DISK:
                action = self._screen_select_disk()
            elif stage is _WizardStage.CONFIRM_FLASH:
                action = self._screen_confirm_flash()
            else:
                action = self._screen_reboot_or_done()

            if action == "quit":
                return
            # All other actions (back / continue / refresh) loop.

    # ---------- screens ------------------------------------------------

    def _screen_select_image(self) -> str:
        """Stage 1: pick an image.

        Combines local image-root scan + the optional ``--catalog``
        overlay into one numbered list. Operator types a number or
        a single-letter command.
        """
        self._refresh_images()
        self._console.clear()
        self._print_header(stage=1, title="Pick an image to flash")
        self._print_source_summary()
        if self._state._images:
            self._print_image_table(self._state._images)
        else:
            self._print_empty_catalog_panel()

        prompt_text = self._render_prompt_line(
            choice_hint="image #",
            extras=(
                ("c", "switch catalog source"),
                ("d", "default catalog (bty release)"),
                ("i", "install bty-server"),
                ("r", "refresh"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("r", "refresh", ""):
            return "continue"
        if choice in ("c", "catalog"):
            self._screen_change_catalog()
            return "continue"
        if choice in ("d", "default"):
            self._state.catalog_source = _BTY_DEFAULT_CATALOG_URL
            self._state.pxe_done_base = _pxe_done_base_from_source(_BTY_DEFAULT_CATALOG_URL)
            return "continue"
        if choice in ("i", "install"):
            self._screen_install_bty_server()
            return "continue"
        idx = self._parse_index(choice, len(self._state._images))
        if idx is not None:
            self._state.selected_image = self._state._images[idx]
        else:
            self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
            self._pause_for_ack()
        return "continue"

    def _screen_select_disk(self) -> str:
        """Stage 2: pick a disk.

        Refreshed every entry to catch hotplug. Filtered to block
        devices of type ``disk`` (skips loop / ram / partitions).
        """
        self._refresh_disks()
        self._console.clear()
        self._print_header(stage=2, title="Pick a target disk")
        self._print_selection_so_far()
        if self._state._disks:
            self._print_disk_table(self._state._disks)
        else:
            self._console.print(
                Panel(
                    f"[{_DANGER}]No flash-eligible disks detected.[/]\n\n"
                    f"[{_MUTED}]Check ``lsblk`` on tty2 to see what the kernel sees.[/]",
                    border_style=_DANGER,
                    title="No disks",
                )
            )

        prompt_text = self._render_prompt_line(
            choice_hint="disk #",
            extras=(
                ("b", "back"),
                ("r", "refresh"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("b", "back", "esc"):
            self._state.back()
            return "continue"
        if choice in ("r", "refresh", ""):
            return "continue"
        idx = self._parse_index(choice, len(self._state._disks))
        if idx is not None:
            self._state.selected_disk = self._state._disks[idx]
        else:
            self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
            self._pause_for_ack()
        return "continue"

    def _screen_confirm_flash(self) -> str:
        """Stage 3: probe image + target, render plan, y/N confirm.

        Probing runs synchronously with a Rich Status spinner so
        the operator sees something during the 1-3s of subprocess
        calls (``lsblk``, ``qemu-img info``, etc.).
        """
        image = self._state.selected_image
        disk = self._state.selected_disk
        assert image is not None and disk is not None  # stage gate

        disk_path = Path(str(disk.get("path") or disk.get("name") or ""))
        self._console.clear()
        self._print_header(stage=3, title="Confirm flash plan")
        self._print_selection_so_far()

        # Probe both ends with a spinner so the screen isn't blank
        # during the lsblk + qemu-img info round-trips.
        plan_or_error = self._probe_and_plan(image, disk_path)
        if isinstance(plan_or_error, str):
            self._console.print(
                Panel(
                    f"[{_DANGER}]Probe failed:[/]\n\n{plan_or_error}",
                    border_style=_DANGER,
                    title="Plan rejected",
                )
            )
            choice = self._ask(
                self._render_prompt_line(
                    choice_hint="",
                    extras=(("b", "back"), ("q", "quit")),
                )
            )
            if choice in ("q", "quit"):
                return "quit"
            self._state.back()
            return "continue"

        plan, errors = plan_or_error
        self._print_flash_plan(plan, errors)

        if errors:
            self._console.print(
                Panel(
                    f"[{_DANGER}]Validation FAILED:[/]\n" + "\n".join(f"  - {e}" for e in errors),
                    border_style=_DANGER,
                    title="Plan rejected",
                )
            )
            choice = self._ask(
                self._render_prompt_line(
                    choice_hint="",
                    extras=(("b", "back"), ("q", "quit")),
                )
            )
            if choice in ("q", "quit"):
                return "quit"
            self._state.back()
            return "continue"

        prompt_text = self._render_prompt_line(
            # Backslash-escapes prevent Rich from parsing ``[y]``/``[b]``/
            # ``[q]`` as (non-existent) markup style tags and swallowing
            # them. Closing ``]`` does not need escaping.
            choice_hint="\\[y]es to flash, \\[b]ack, \\[q]uit",
            extras=(),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("b", "back", "n", "no", ""):
            self._state.back()
            return "continue"
        if choice in ("y", "yes"):
            self._screen_flash_running(plan)
            return "continue"
        self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
        self._pause_for_ack()
        return "continue"

    def _screen_flash_running(self, plan: flash.FlashPlan) -> None:
        """Run the flash in a background thread; the main thread
        sits in a Rich Live() with a Progress bar updated from the
        ``FlashProgress`` callback.

        On success, sets ``self._state.post_flash = True`` so the
        next ``stage()`` returns REBOOT_OR_DONE.
        """
        self._console.clear()
        self._print_header(stage=3, title="Flashing...")

        progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("[{task.fields[bytes_human]}]"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=False,
            expand=True,
        )

        # Shared state between the flash worker thread and the
        # rendering loop. Only the worker writes; the main thread
        # reads via the Progress callback shim below.
        shared: dict[str, Any] = {"result": None, "error": None, "stage": "starting"}

        with progress:
            task_id = progress.add_task(
                "queued",
                total=plan.image.virtual_size_bytes or plan.image.size_bytes or None,
                bytes_human="0 / ?",
            )

            def _on_progress(ev: flash.FlashProgress) -> None:
                # Called from the flash thread. ``progress`` is
                # thread-safe (Rich's Progress mutex), so direct
                # updates are fine.
                shared["stage"] = ev.event
                if ev.event == "started":
                    if ev.total_bytes:
                        progress.update(task_id, total=ev.total_bytes)
                    progress.update(task_id, description="starting flash")
                elif ev.event == "writing":
                    progress.update(task_id, description=f"writing ({ev.note or '?'})")
                elif ev.event == "writing_progress":
                    if ev.bytes_written is not None:
                        progress.update(
                            task_id,
                            completed=ev.bytes_written,
                            bytes_human=_format_progress_bytes(ev.bytes_written, ev.total_bytes),
                        )
                elif ev.event == "synced":
                    progress.update(task_id, description="syncing buffers")
                elif ev.event == "partprobed":
                    progress.update(task_id, description="partprobed")
                elif ev.event == "done":
                    progress.update(task_id, description="done")
                elif ev.event == "failed":
                    progress.update(task_id, description=f"FAILED: {ev.note}")
                elif ev.event == "subprocess_log":
                    # Rich's Progress is a Live; ``console.print``
                    # inside the live context erases the live
                    # region, prints the line, and redraws -- so
                    # the log line lands above the progress widget
                    # without corrupting it.
                    self._console.print(f"[{_MUTED}]{ev.note}[/]")

            def _runner() -> None:
                try:
                    flash.execute_plan(plan, progress=_on_progress)
                    shared["result"] = "ok"
                except flash.FlashError as exc:
                    shared["result"] = "failed"
                    shared["error"] = str(exc)
                except Exception as exc:
                    shared["result"] = "failed"
                    shared["error"] = f"unexpected: {exc!r}"

            t = threading.Thread(target=_runner, name="bty-flash", daemon=True)
            t.start()
            t.join()

        if shared["result"] == "ok":
            self._console.print(
                Panel(
                    f"[{_OK}]Flash completed.[/]",
                    border_style=_OK,
                    title="Done",
                )
            )
            self._post_pxe_done_if_configured()
            self._state.post_flash = True
        else:
            self._console.print(
                Panel(
                    f"[{_DANGER}]Flash FAILED.[/]\n\n"
                    f"[{_MUTED}]{shared.get('error') or 'unknown error'}[/]",
                    border_style=_DANGER,
                    title="Flash failed",
                )
            )
            self._pause_for_ack()

    def _screen_reboot_or_done(self) -> str:
        """Stage 4: flash succeeded. Offer reboot.

        Esc / ``b`` from here goes back to Stage 2 (same disk, pick
        again) so the operator can flash another disk with the same
        image without re-selecting the image. Full reset happens on
        a further ``b``.
        """
        self._console.clear()
        self._print_header(stage=4, title="Flash complete -- ready to reboot")
        self._console.print(
            Panel(
                f"[{_OK}]Image written to {self._state.selected_disk}.[/]\n\n"
                f"Reboot now to boot the freshly flashed disk.",
                border_style=_OK,
                title="Done",
            )
        )

        prompt_text = self._render_prompt_line(
            choice_hint="\\[y]es reboot now, \\[b]ack, \\[q]uit",
            extras=(),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit", "n", "no", ""):
            return "quit"
        if choice in ("b", "back"):
            self._state.back()
            return "continue"
        if choice in ("y", "yes", "r", "reboot"):
            self._do_reboot()
            return "quit"  # unreachable on success; defensive
        self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
        self._pause_for_ack()
        return "continue"

    # ---------- auxiliary screens -------------------------------------

    def _screen_change_catalog(self) -> None:
        """Switch the catalog source. Prompt for a new URL / path.

        Empty input = clear catalog (local-only mode). Invalid
        sources surface as a soft banner on the next image-pick
        screen rather than crashing.
        """
        self._console.clear()
        self._print_header(stage=1, title="Switch catalog source")
        self._console.print(
            Panel(
                f"Current source: [{_PRIMARY}]{self._state.catalog_source or '(local only)'}[/]\n\n"
                "Enter a new source:\n"
                "  - local TOML path:    ``/etc/bty/catalog.toml``\n"
                "  - HTTP URL:           ``http://bty-server:8080/catalog.toml``\n"
                "  - ORAS reference:     ``oras://ghcr.io/owner/repo:tag``\n"
                "  - empty:              clear catalog (local image-root only)",
                title="Catalog source",
            )
        )
        prompt = "[bold]>[/] [new catalog source, empty to clear, q to abort]"
        new_source = self._ask(prompt).strip()
        if new_source in ("q", "quit"):
            return
        self._state.catalog_source = new_source or None
        self._state.pxe_done_base = _pxe_done_base_from_source(self._state.catalog_source)

    def _screen_install_bty_server(self) -> None:
        """Synthesize a one-shot flash plan for the latest bty-server
        release URL. Doesn't go through the wizard; runs immediately
        on confirm.
        """
        self._console.clear()
        self._print_header(stage=1, title="Install bty-server (latest GitHub release)")
        self._console.print(
            Panel(
                f"This will flash [{_PRIMARY}]{_BTY_SERVER_LATEST_URL}[/]\n"
                f"as a fresh appliance on a target disk you pick next.",
                title="bty-server install",
            )
        )

        confirm = self._ask("[bold]>[/] proceed with bty-server flash? [y/N]").strip().lower()
        if confirm not in ("y", "yes"):
            return

        # Stage the install as a synthetic image + force-pick disk.
        self._state.selected_image = _TuiImage(
            name=_BTY_SERVER_LATEST_NAME,
            fmt="img.gz",
            size_bytes=0,
            url=_BTY_SERVER_LATEST_URL,
        )
        # Now the wizard's next iteration will land at SELECT_DISK
        # then CONFIRM_FLASH automatically.

    # ---------- rendering helpers -------------------------------------

    def _print_header(self, *, stage: int, title: str) -> None:
        """Single-line header: bty version + stage breadcrumb +
        title. The breadcrumb is the wizard map: 1 -> 2 -> 3 -> 4
        with the current stage in accent colour.
        """
        crumb_parts = []
        for n, label in enumerate(("Image", "Disk", "Flash", "Reboot"), start=1):
            if n == stage:
                crumb_parts.append(f"[bold {_ACCENT}]{n}.{label}[/]")
            else:
                crumb_parts.append(f"[{_MUTED}]{n}.{label}[/]")
        crumb = " -> ".join(crumb_parts)
        self._console.print(
            f"[bold]bty[/] [{_MUTED}]v{bty.__version__}[/]   {crumb}   [{_MUTED}]({title})[/]"
        )
        self._console.print()

    def _print_source_summary(self) -> None:
        """One-line summary of where the catalog rows came from."""
        parts = [f"image_root: [{_PRIMARY}]{self._state.image_root}[/]"]
        if self._state.catalog_source:
            parts.append(f"catalog: [{_PRIMARY}]{self._state.catalog_source}[/]")
        if self._state.mac:
            parts.append(f"mac: [{_PRIMARY}]{self._state.mac}[/]")
        self._console.print(f"[{_MUTED}]" + "   ".join(parts) + "[/]")
        if self._catalog_load_error:
            self._console.print(f"[{_DANGER}]catalog load failed: {self._catalog_load_error}[/]")
        self._console.print()

    def _print_selection_so_far(self) -> None:
        """Echo what's been committed to the wizard. Lets the
        operator see at each stage what they've picked + what's
        still pending.
        """
        rows = []
        if self._state.selected_image:
            rows.append(("Image", self._state.selected_image.name))
        if self._state.selected_disk:
            d = self._state.selected_disk
            rows.append(("Disk", f"{d.get('path')} ({d.get('size', '?')} {d.get('model') or ''})"))
        if not rows:
            return
        for label, value in rows:
            self._console.print(f"  [{_MUTED}]{label}:[/] [{_PRIMARY}]{value}[/]")
        self._console.print()

    def _print_image_table(self, rows: list[_TuiImage]) -> None:
        table = Table(
            show_header=True,
            header_style=f"bold {_PRIMARY}",
            row_styles=("", f"on {_STRIPE}"),
            expand=True,
        )
        table.add_column("#", justify="right", style=_ACCENT, no_wrap=True)
        table.add_column("Name")
        table.add_column("Format", style=_PRIMARY, no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Source", style=_MUTED)
        for i, row in enumerate(rows, start=1):
            source = "local" if row.path else "remote"
            table.add_row(
                str(i),
                row.name,
                row.fmt or "?",
                _format_mib(row.size_bytes) if row.size_bytes else "-",
                source,
            )
        self._console.print(table)
        self._console.print()

    def _print_disk_table(self, rows: list[dict[str, Any]]) -> None:
        table = Table(
            show_header=True,
            header_style=f"bold {_PRIMARY}",
            row_styles=("", f"on {_STRIPE}"),
            expand=True,
        )
        table.add_column("#", justify="right", style=_ACCENT, no_wrap=True)
        table.add_column("Path", style=_PRIMARY, no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Model")
        table.add_column("Transport", style=_MUTED, no_wrap=True)
        table.add_column("Serial", style=_MUTED, no_wrap=True)
        for i, d in enumerate(rows, start=1):
            table.add_row(
                str(i),
                str(d.get("path") or d.get("name") or "?"),
                str(d.get("size") or "?"),
                str(d.get("model") or ""),
                str(d.get("tran") or d.get("transport") or ""),
                str(d.get("serial") or ""),
            )
        self._console.print(table)
        self._console.print()

    def _print_empty_catalog_panel(self) -> None:
        body = (
            f"No images visible.\n\n"
            f"[{_MUTED}]Add some via:[/]\n"
            f"  - dropping files into [{_PRIMARY}]{self._state.image_root}[/]\n"
            f"  - [{_ACCENT}]d[/] to load bty's default release catalog\n"
            f"  - [{_ACCENT}]c[/] to switch to a custom catalog source"
        )
        self._console.print(Panel(body, title="Catalog is empty"))
        self._console.print()

    def _print_flash_plan(self, plan: flash.FlashPlan, errors: list[str]) -> None:
        """Rich rendering of the plan -- replaces the
        FlashConfirmScreen modal's body.
        """
        image_lines = [
            f"  image:        {plan.image.url or plan.image.path}",
            f"  format:       {plan.image.format or '?'}",
            f"  size on disk: {_format_mib(plan.image.size_bytes)}"
            f" ({plan.image.size_bytes or 0} bytes)",
        ]
        if plan.image.virtual_size_bytes is not None:
            image_lines.append(f"  virtual size: {_format_mib(plan.image.virtual_size_bytes)}")
        target_lines = [
            f"  target:       {plan.target.path}",
            f"  size:         {_format_mib(plan.target.size_bytes)}",
        ]
        body = "[bold]Image[/]\n" + "\n".join(image_lines)
        body += "\n\n[bold]Target[/]\n" + "\n".join(target_lines)
        border_style = _DANGER if errors else _OK
        title = "[red]Flash plan (rejected)[/]" if errors else "[green]Flash plan[/]"
        self._console.print(Panel(body, border_style=border_style, title=title))

    def _render_prompt_line(
        self,
        *,
        choice_hint: str,
        extras: tuple[tuple[str, str], ...],
    ) -> str:
        """Build the prompt suffix shown by ``Prompt.ask``.

        Returns a Rich-markup-formatted prompt label. ``choice_hint``
        is the primary action description; ``extras`` is a list of
        (key, label) pairs for secondary actions.
        """
        parts: list[str] = []
        if choice_hint:
            parts.append(f"[bold]{choice_hint}[/]")
        for key, label in extras:
            parts.append(f"[{_ACCENT}]{key}[/]={label}")
        return "[bold]>[/] " + "  ".join(parts)

    def _ask(self, prompt_text: str) -> str:
        """Single-line prompt with a leading newline so it's clearly
        separated from the rendered panel above.
        """
        try:
            answer = Prompt.ask(prompt_text, console=self._console, default="").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"
        return answer

    def _pause_for_ack(self) -> None:
        """Tiny ``press Enter to continue``. Used after an error
        message so the operator sees it before the screen redraws.
        """
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            Prompt.ask(
                f"[{_MUTED}](press Enter to continue)[/]",
                console=self._console,
                default="",
            )

    # ---------- model helpers (probe, plan, post) ---------------------

    def _refresh_images(self) -> None:
        """Combine local + (optional) catalog overlay into one
        sorted list. Catalog load errors surface via
        ``self._catalog_load_error``.

        Prints a one-line ``loading catalog ...`` indicator BEFORE
        the blocking fetch so an operator on a slow / broken network
        sees where the wait is going. On a healthy LAN the fetch
        finishes inside a second and the indicator scrolls past
        instantly; on a stuck DNS / slow server it tells the
        operator the box is waiting on the network, not wedged.
        """
        local = _list_local_images(self._state.image_root)
        remote: list[_TuiImage] = []
        self._catalog_load_error = None
        if self._state.catalog_source:
            self._console.print(
                f"[{_MUTED}]loading catalog from {self._state.catalog_source} (timeout 30s) ...[/]"
            )
            try:
                remote = load_catalog_from_source(self._state.catalog_source)
            except (
                _catalog.CatalogError,
                urllib.error.URLError,
                OSError,
                ValueError,
            ) as exc:
                self._catalog_load_error = f"{type(exc).__name__}: {exc}"
        self._state._images = local + remote

    def _refresh_disks(self) -> None:
        self._state._disks = _list_disks()

    def _parse_index(self, choice: str, n: int) -> int | None:
        """Parse a 1-based numeric choice into a 0-based list index.
        Returns ``None`` for non-numeric / out-of-range input.
        """
        if not choice:
            return None
        try:
            idx = int(choice) - 1
        except ValueError:
            return None
        if 0 <= idx < n:
            return idx
        return None

    def _probe_and_plan(
        self,
        image: _TuiImage,
        disk_path: Path,
    ) -> tuple[flash.FlashPlan, list[str]] | str:
        """Probe both ends + build + validate. Returns ``(plan,
        errors)`` on success, or a string error message on probe
        failure (image URL unreachable, target gone, etc.).

        Rendered with a Rich Status spinner so the 1-3s of
        subprocess calls don't look like a wedge.
        """
        from rich.status import Status

        with Status(
            f"[{_ACCENT}]probing image + target ...[/]",
            console=self._console,
        ):
            try:
                if image.url is not None:
                    image_info = flash.probe_image_url(image.url, format_hint=image.fmt)
                else:
                    assert image.path is not None  # local row guarantees a path
                    image_info = flash.probe_image(image.path)
            except (FileNotFoundError, ValueError) as exc:
                return f"image probe failed: {exc}"

            try:
                target_info = flash.probe_target(disk_path)
            except (FileNotFoundError, ValueError) as exc:
                return f"target probe failed: {exc}"

        plan = flash.make_plan(image_info, target_info)
        errors = flash.validate_plan(plan)
        return plan, errors

    def _post_pxe_done_if_configured(self) -> None:
        """Best-effort: POST ``/pxe/<mac>/done`` after a successful
        flash so the bty-web server's last_flashed_at + flash-once
        flip can fire. Failure is logged via the soft banner; does
        NOT block the post-flash transition (lesson from v0.20.1).
        """
        if self._state.pxe_done_base is None or self._state.mac is None:
            return
        try:
            post_pxe_done(self._state.pxe_done_base, self._state.mac)
        except urllib.error.URLError as exc:
            self._console.print(
                f"[{_DANGER}]post-flash signal failed:[/] {exc} "
                f"[{_MUTED}](flash succeeded; bty-web didn't update)[/]"
            )

    def _auto_post_inventory(self) -> None:
        """Background-thread post of the disk inventory so a slow
        bty-web doesn't delay the first paint.
        """
        if self._state.pxe_done_base is None or self._state.mac is None:
            return
        base = self._state.pxe_done_base
        mac = self._state.mac

        def _runner() -> None:
            try:
                payload = disks.list_disks()
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                return
            with contextlib.suppress(urllib.error.URLError, ConnectionError, TimeoutError):
                post_inventory(base, mac, payload)

        t = threading.Thread(target=_runner, name="bty-inventory", daemon=True)
        t.start()

    def _do_reboot(self) -> None:
        self._console.print(f"[{_ACCENT}]Rebooting now ...[/]")
        with contextlib.suppress(FileNotFoundError, OSError):
            subprocess.run(["systemctl", "reboot"], check=False)


# ---------------------------------------------------------------------------
# Module-level helpers used by the screens but exposed for test
# isolation.
# ---------------------------------------------------------------------------


def _format_progress_bytes(written: int | None, total: int | None) -> str:
    """Format ``{written} / {total}`` in MiB. Either side may be
    None; renders as ``?`` in that case.
    """
    w = _format_mib(written) if written is not None else "?"
    t = _format_mib(total) if total is not None else "?"
    return f"{w} / {t}"


# ---------------------------------------------------------------------------
# Convenience standalone runner. Imported by ``bty tui`` via the
# ``bty-tui`` console script in pyproject.toml's [project.scripts].
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry. Kept as a no-arg wrapper so callers
    that imported it from the old location keep working.
    """
    BtyTui().run()


__all__ = [
    "BtyTui",
    "_TuiImage",
    "_WizardStage",
    "_format_mib",
    "_parse_size_to_bytes",
    "_pxe_done_base_from_source",
    "load_catalog_from_source",
    "main",
    "post_inventory",
    "post_pxe_done",
]
