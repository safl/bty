"""Tests for the bty-tui module.

Two layers:

1. Free helpers (``load_catalog_from_source``, ``post_pxe_done``)
   covered without instantiating textual at all - they're just HTTP
   wrappers.
2. End-to-end interaction with the textual ``BtyTui`` app via
   ``App.run_test()`` (textual's headless test harness). The Pilot
   drives key presses and click events; assertions look at widget
   state via ``app.query_one(...)``. ``asyncio.run`` is used to drive
   the async test body so we don't have to take a pytest-asyncio
   dependency.

Data sources (``images.list_images``, ``disks.list_disks``,
``load_catalog_from_source``) are monkeypatched in the per-test fixtures
to return synthetic rows; the goal is to verify the wiring in
``_populate_images`` / ``_populate_disks`` / ``_load_images`` /
``action_refresh`` / ``_initial_status``, not to re-test the
underlying functions (those have their own coverage).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bty import images
from bty.tui import _app as tui_app


def _run(coro: Any) -> Any:
    """Drive an async test body without pulling in pytest-asyncio."""
    return asyncio.run(coro)


def _fake_image(
    name: str = "demo.qcow2",
    fmt: str = "qcow2",
    size: int = 1024,
) -> images.Image:
    """Synthetic ``images.Image`` for monkeypatched list_images."""
    return images.Image(
        path=Path("/fake/images") / name,
        name=name,
        format=fmt,
        size_bytes=size,
    )


def _fake_disk(
    path: str = "/dev/sda",
    size: str = "500G",
    model: str = "Test Disk",
) -> dict[str, Any]:
    """Synthetic disk row matching ``disks.list_disks`` output shape."""
    return {
        "path": path,
        "size": size,
        "type": "disk",
        "vendor": "ATA",
        "model": model,
        "serial": "TEST123",
        "tran": "sata",
        "removable": False,
        "readonly": False,
        "mountpoints": [],
    }


def _fake_resp(payload: Any) -> MagicMock:
    """A context-manageable fake matching urlopen's protocol."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


_VALID_CATALOG_TOML = b"""\
version = 1

[[images]]
name = "demo.qcow2"
src = "http://server:8080/images/abc123def456"
sha256 = "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123def4"
format = "qcow2"
size_bytes = 1024

[[images]]
name = "live.img.zst"
src = "https://github.com/safl/bty-images/releases/download/v1/live.img.zst"
sha256 = "fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98"
format = "img.zst"
size_bytes = 4096
"""


def _fake_bytes_resp(raw: bytes):
    """urllib.request.urlopen replacement that returns ``raw`` as the
    response body. Compatible with the ``with urlopen(...) as resp``
    + ``resp.read(n)`` pattern."""

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def read(self, _n: int = -1) -> bytes:
            return raw

    return _Resp()


def test_load_catalog_from_source_parses_http_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_catalog_from_source(http://...)`` issues a GET, parses the
    response body as TOML (via ``bty.catalog.load_bytes``), and emits
    one ``_TuiImage`` per ``[[images]]`` entry. ``src`` becomes the
    flashable URL the TUI later hands to the URL pipeline."""
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(_VALID_CATALOG_TOML),
    )

    rows = tui_app.load_catalog_from_source("http://server:8080/catalog.toml")

    assert len(rows) == 2
    assert rows[0].name == "demo.qcow2"
    assert rows[0].fmt == "qcow2"
    assert rows[0].size_bytes == 1024
    assert rows[0].url == "http://server:8080/images/abc123def456"
    assert rows[0].path is None  # remote rows never get a local path
    assert rows[1].url == "https://github.com/safl/bty-images/releases/download/v1/live.img.zst"


def test_load_catalog_from_source_parses_local_path(tmp_path: Path) -> None:
    """Bare path (no scheme) goes through the local read path,
    parses identically to the HTTP path."""
    catalog_file = tmp_path / "catalog.toml"
    catalog_file.write_bytes(_VALID_CATALOG_TOML)

    rows = tui_app.load_catalog_from_source(str(catalog_file))
    assert [r.name for r in rows] == ["demo.qcow2", "live.img.zst"]


def test_load_catalog_from_source_rejects_invalid_toml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage bytes -> ``CatalogError`` (catalog.load_bytes wraps
    tomllib's TOMLDecodeError). Same behaviour for local paths and
    URL fetches."""
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(b"not valid toml at all <<<"),
    )
    with pytest.raises(tui_app._catalog.CatalogError):
        tui_app.load_catalog_from_source("http://server/catalog.toml")


def test_load_catalog_from_source_propagates_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)

    with pytest.raises(urllib.error.URLError):
        tui_app.load_catalog_from_source("http://server/catalog.toml")


def test_pxe_done_base_from_source_for_http() -> None:
    """A http(s):// catalog URL derives scheme://host[:port] as the
    pxe-done base. Static-file and oras:// sources -> None (no POST)."""
    assert (
        tui_app._pxe_done_base_from_source("http://server:8080/catalog.toml")
        == "http://server:8080"
    )
    assert (
        tui_app._pxe_done_base_from_source("https://example.com/path/catalog.toml")
        == "https://example.com"
    )
    assert tui_app._pxe_done_base_from_source(None) is None
    assert tui_app._pxe_done_base_from_source("oras://ghcr.io/owner/repo:tag") is None
    assert tui_app._pxe_done_base_from_source("/local/catalog.toml") is None


def test_post_pxe_done_sends_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """``post_pxe_done`` issues a POST against ``<server>/pxe/<mac>/done``."""
    seen: dict[str, str] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)

    tui_app.post_pxe_done("http://server:8080", "aa:bb:cc:dd:ee:ff")

    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/done"
    assert seen["method"] == "POST"


def test_post_pxe_done_strips_trailing_slash_from_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)
    tui_app.post_pxe_done("http://server:8080/", "aa:bb:cc:dd:ee:ff")
    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/done"


def test_post_pxe_done_propagates_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("server gone")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)

    with pytest.raises(urllib.error.URLError):
        tui_app.post_pxe_done("http://server", "aa:bb:cc:dd:ee:ff")


def test_post_inventory_sends_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``post_inventory`` serialises the disk list to JSON, sets
    Content-Type and POSTs to ``<server>/pxe/<mac>/inventory``."""
    seen: dict[str, object] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        seen["content_type"] = req.get_header("Content-type")
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)
    tui_app.post_inventory(
        "http://server:8080",
        "aa:bb:cc:dd:ee:ff",
        [{"path": "/dev/sda", "serial": "S1234"}],
    )
    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/inventory"
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    import json as _json

    decoded = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert decoded == {"disks": [{"path": "/dev/sda", "serial": "S1234"}]}


def test_post_inventory_propagates_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport errors bubble up to the caller so the wrapper in
    BtyTui can surface them on the status bar."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)
    with pytest.raises(urllib.error.URLError):
        tui_app.post_inventory("http://server", "aa:bb:cc:dd:ee:ff", [])


def test_main_accepts_catalog_and_mac_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bty-tui --catalog URL --mac MAC`` reaches ``BtyTui(...)`` with
    the right kwargs. The actual ``run()`` is monkeypatched so we
    don't try to launch a real TUI from a unit test."""
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            image_root: object = None,
            *,
            catalog_source: object = None,
            mac: object = None,
        ) -> None:
            captured["image_root"] = image_root
            captured["catalog_source"] = catalog_source
            captured["mac"] = mac

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)

    # Re-import the entry-point to make sure it picks up the patched class.
    import bty.tui as tui_mod

    tui_mod.main(["--catalog", "http://srv:8080/catalog.toml", "--mac", "aa:bb:cc:dd:ee:ff"])

    assert captured["catalog_source"] == "http://srv:8080/catalog.toml"
    assert captured["mac"] == "aa:bb:cc:dd:ee:ff"
    assert captured["ran"] is True


def test_main_accepts_image_root_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bty-tui --image-root /path`` reaches ``BtyTui(image_root=Path(...))``.

    The flag overrides ``BTY_IMAGE_ROOT`` env var and the live env
    default; useful for local development where the operator is
    running from a checkout, not the bty live env.
    """
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            image_root: object = None,
            *,
            catalog_source: object = None,
            mac: object = None,
        ) -> None:
            captured["image_root"] = image_root
            captured["catalog_source"] = catalog_source
            captured["mac"] = mac

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)
    import bty.tui as tui_mod

    tui_mod.main(["--image-root", "/tmp/bty-images"])

    assert captured["image_root"] == Path("/tmp/bty-images")
    assert captured["catalog_source"] is None
    assert captured["mac"] is None
    assert captured["ran"] is True


# ---------- type-level cross-cutting ---------------------------------------


def test_oras_error_is_an_os_error() -> None:
    """:class:`OrasError` must subclass :class:`OSError` so callers
    that handle remote-I/O generically (``except OSError``) catch
    it. ``urllib.error.URLError`` already follows this pattern;
    keeping OrasError on the same family closes the "TUI freezes
    after oras failure" class of bugs at the type level."""
    from bty import oras as _oras_module

    assert issubclass(_oras_module.OrasError, OSError)
    # And it remains catchable as itself for the call sites that
    # do want ORAS-specific handling.
    err = _oras_module.OrasError("boom")
    assert isinstance(err, _oras_module.OrasError)
    assert isinstance(err, OSError)


# --------------------------------------------------------------------------
# Wizard state machine: _State.stage() + _State.back()
# --------------------------------------------------------------------------


def test_state_stage_advances_with_each_commit() -> None:
    """``_State.stage()`` is derived from selection state. Empty state
    with ``catalog_chosen=False`` -> Stage 1 (SELECT_CATALOG); flipping
    ``catalog_chosen=True`` -> Stage 2 (SELECT_IMAGE); setting
    ``selected_image`` -> Stage 3; both selections -> Stage 4;
    ``post_flash`` -> Stage 5.
    """
    s = tui_app._State(image_root=Path("/tmp"))
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG

    s.catalog_chosen = True
    assert s.stage() is tui_app._WizardStage.SELECT_IMAGE

    s.selected_image = tui_app._TuiImage(name="x", fmt="img.gz", size_bytes=0, url="http://x")
    assert s.stage() is tui_app._WizardStage.SELECT_DISK

    s.selected_disk = {"path": "/dev/sda"}
    assert s.stage() is tui_app._WizardStage.CONFIRM_FLASH

    s.post_flash = True
    assert s.stage() is tui_app._WizardStage.REBOOT_OR_DONE


def test_state_back_clears_one_commit_at_a_time() -> None:
    """``_State.back()`` drops the most-recent commit, one stage at a
    time. Chain across all five stages:

      REBOOT_OR_DONE -> SELECT_DISK   (clear post_flash + disk)
      CONFIRM_FLASH  -> SELECT_DISK   (clear disk)
      SELECT_DISK    -> SELECT_IMAGE  (clear image)
      SELECT_IMAGE   -> SELECT_CATALOG (clear catalog_chosen)
      SELECT_CATALOG -> no-op (top of wizard)
    """
    s = tui_app._State(image_root=Path("/tmp"))
    s.catalog_chosen = True
    s.selected_image = tui_app._TuiImage(name="x", fmt="img.gz", size_bytes=0, url="http://x")
    s.selected_disk = {"path": "/dev/sda"}
    s.post_flash = True

    s.back()  # REBOOT_OR_DONE -> SELECT_DISK (keep image, clear disk + post_flash)
    assert s.stage() is tui_app._WizardStage.SELECT_DISK
    assert s.selected_image is not None
    assert s.selected_disk is None
    assert s.post_flash is False

    s.selected_disk = {"path": "/dev/sda"}
    s.back()  # CONFIRM_FLASH -> SELECT_DISK (clear disk)
    assert s.stage() is tui_app._WizardStage.SELECT_DISK
    assert s.selected_disk is None

    s.back()  # SELECT_DISK -> SELECT_IMAGE (clear image)
    assert s.stage() is tui_app._WizardStage.SELECT_IMAGE
    assert s.selected_image is None

    s.back()  # SELECT_IMAGE -> SELECT_CATALOG (clear catalog_chosen)
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG
    assert s.catalog_chosen is False

    s.back()  # SELECT_CATALOG -> no-op
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG


# --------------------------------------------------------------------------
# Index parsing: numeric prompt -> list index
# --------------------------------------------------------------------------


def test_parse_index_handles_valid_and_invalid_input(tmp_path: Path) -> None:
    """``_parse_index`` returns 0-based index for a valid 1-based
    numeric choice within ``[1, n]``; returns ``None`` otherwise
    (empty, non-numeric, out of range).
    """
    app = tui_app.BtyTui(image_root=tmp_path)
    assert app._parse_index("1", 3) == 0
    assert app._parse_index("3", 3) == 2
    assert app._parse_index("4", 3) is None
    assert app._parse_index("0", 3) is None
    assert app._parse_index("", 3) is None
    assert app._parse_index("q", 3) is None
    assert app._parse_index("1.5", 3) is None
    assert app._parse_index("-1", 3) is None


# --------------------------------------------------------------------------
# Refresh paths: local + remote catalog blending
# --------------------------------------------------------------------------


def test_refresh_images_blends_local_and_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_refresh_images`` combines the local image-root scan and
    the optional ``--catalog`` overlay. Both feeds end up on
    ``_state._images``.
    """
    monkeypatch.setattr(
        tui_app,
        "_list_local_images",
        lambda _root: [
            tui_app._TuiImage(
                name="local.img.gz",
                fmt="img.gz",
                size_bytes=1024,
                path=tmp_path / "local.img.gz",
            )
        ],
    )
    monkeypatch.setattr(
        tui_app,
        "load_catalog_from_source",
        lambda _src, **_kw: [
            tui_app._TuiImage(
                name="remote (rolling)",
                fmt="img.gz",
                size_bytes=2048,
                url="https://example.invalid/remote.img.gz",
            )
        ],
    )
    app = tui_app.BtyTui(image_root=tmp_path, catalog_source="https://example.invalid/catalog.toml")
    app._refresh_images()
    names = [i.name for i in app._state._images]
    assert "local.img.gz" in names
    assert "remote (rolling)" in names
    assert app._catalog_load_error is None


def test_refresh_images_surfaces_catalog_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed catalog fetch (URL unreachable, bad TOML, oras
    miss) must NOT abort the TUI -- it sets
    ``_catalog_load_error`` and the next screen render shows a
    soft banner.
    """
    monkeypatch.setattr(tui_app, "_list_local_images", lambda _root: [])

    def _boom(_src, **_kw):
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(tui_app, "load_catalog_from_source", _boom)
    app = tui_app.BtyTui(image_root=tmp_path, catalog_source="https://example.invalid/catalog.toml")
    app._refresh_images()
    assert app._state._images == []
    assert app._catalog_load_error is not None
    assert "URLError" in app._catalog_load_error


# --------------------------------------------------------------------------
# Rendering smoke tests: tables don't crash on various input shapes
# --------------------------------------------------------------------------


def test_print_image_table_handles_empty_and_partial_rows(
    tmp_path: Path,
) -> None:
    """``_print_image_table`` must render cleanly on rows with
    missing fmt / size_bytes. The framebuffer console renders
    ``?`` placeholders; the test just confirms no exceptions.
    """
    app = tui_app.BtyTui(image_root=tmp_path)
    app._print_image_table(
        [
            tui_app._TuiImage(name="complete.img.gz", fmt="img.gz", size_bytes=1024, path=tmp_path),
            tui_app._TuiImage(name="missing-fmt", fmt=None, size_bytes=0, url="http://x"),
        ]
    )  # no raise


def test_print_disk_table_handles_partial_rows(tmp_path: Path) -> None:
    """``_print_disk_table`` must render even when lsblk omits
    optional fields (vendor / model / serial). Crashing here used
    to be the v0.19.x "TUI freezes after lsblk reports a sparse
    row" class of bug.
    """
    app = tui_app.BtyTui(image_root=tmp_path)
    app._print_disk_table(
        [
            {"path": "/dev/sda", "size": "500G"},  # minimal: just path + size
            {"name": "nvme0n1", "size": "1T", "model": "Samsung 980", "serial": "S1"},
        ]
    )  # no raise


# --------------------------------------------------------------------------
# Format-progress helper
# --------------------------------------------------------------------------


def test_format_progress_bytes_handles_unknowns() -> None:
    """``_format_progress_bytes`` renders ``?`` for None inputs so
    a flash whose total isn't yet known doesn't crash the live
    progress callback.
    """
    assert tui_app._format_progress_bytes(0, 1 << 20) == "0.0 MiB / 1.0 MiB"
    assert tui_app._format_progress_bytes(None, 1 << 20) == "? / 1.0 MiB"
    assert tui_app._format_progress_bytes(1 << 20, None) == "1.0 MiB / ?"
    assert tui_app._format_progress_bytes(None, None) == "? / ?"


# --------------------------------------------------------------------------
# Flash plumbing: progress callback receives flash.FlashProgress events
# --------------------------------------------------------------------------


def test_screen_flash_running_drives_callback_and_sets_post_flash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_screen_flash_running`` runs ``flash.execute_plan`` in a
    thread, joins, and flips ``_state.post_flash = True`` on
    success. The progress callback is invoked synchronously from
    the flash thread; we stub execute_plan to emit a small
    sequence of events + return cleanly.
    """
    captured_events: list[str] = []

    def _fake_execute(plan, *, progress=None, cancel=None):
        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started", total_bytes=1024))
            captured_events.append("started")
            progress(tui_app.flash.FlashProgress(event="writing", note="img.gz"))
            captured_events.append("writing")
            progress(
                tui_app.flash.FlashProgress(
                    event="writing_progress", total_bytes=1024, bytes_written=1024
                )
            )
            captured_events.append("writing_progress")
            progress(tui_app.flash.FlashProgress(event="synced"))
            progress(tui_app.flash.FlashProgress(event="partprobed"))
            progress(tui_app.flash.FlashProgress(event="done"))

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute)
    # Stub post_pxe_done so the test doesn't try to reach the network.
    monkeypatch.setattr(
        tui_app,
        "post_pxe_done",
        lambda *_a, **_kw: None,
    )

    app = tui_app.BtyTui(image_root=tmp_path)
    app._state.selected_image = tui_app._TuiImage(
        name="x", fmt="img.gz", size_bytes=0, url="http://x"
    )
    app._state.selected_disk = {"path": "/dev/null"}

    plan = tui_app.flash.FlashPlan(
        image=tui_app.flash.ImageInfo(
            path=None,
            url="http://x",
            format="img.gz",
            size_bytes=1024,
            virtual_size_bytes=1024,
        ),
        target=tui_app.flash.TargetInfo(
            path=Path("/dev/null"),
            size_bytes=10 * 1024 * 1024,
            exists=True,
            is_block_device=True,
            mountpoints=[],
        ),
    )
    app._screen_flash_running(plan)

    assert "started" in captured_events
    assert "writing_progress" in captured_events
    assert app._state.post_flash is True


def test_screen_flash_running_does_not_advance_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a flash that raises ``FlashError`` must NOT
    flip ``post_flash`` -- the operator stays on Stage 3 and
    sees the failure panel.
    """

    def _fake_execute(plan, *, progress=None, cancel=None):
        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started"))
        raise tui_app.flash.FlashError("simulated dd failure")

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute)
    monkeypatch.setattr(tui_app, "post_pxe_done", lambda *_a, **_kw: None)

    app = tui_app.BtyTui(image_root=tmp_path)
    plan = tui_app.flash.FlashPlan(
        image=tui_app.flash.ImageInfo(
            path=None,
            url="http://x",
            format="img.gz",
            size_bytes=1024,
            virtual_size_bytes=1024,
        ),
        target=tui_app.flash.TargetInfo(
            path=Path("/dev/null"),
            size_bytes=10 * 1024 * 1024,
            exists=True,
            is_block_device=True,
            mountpoints=[],
        ),
    )
    # Stub the ack-pause so the test doesn't block on input.
    monkeypatch.setattr(app, "_pause_for_ack", lambda: None)
    app._screen_flash_running(plan)
    assert app._state.post_flash is False
