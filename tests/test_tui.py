"""Tests for the bty-tui module.

Two layers:

1. Free helpers (``fetch_remote_catalog``, ``post_pxe_done``) covered
   without instantiating textual at all - they're just HTTP wrappers.
2. End-to-end interaction with the textual ``BtyTui`` app via
   ``App.run_test()`` (textual's headless test harness). The Pilot
   drives key presses and click events; assertions look at widget
   state via ``app.query_one(...)``. ``asyncio.run`` is used to drive
   the async test body so we don't have to take a pytest-asyncio
   dependency.

Data sources (``images.list_images``, ``disks.list_disks``,
``fetch_remote_catalog``) are monkeypatched in the per-test fixtures
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


def test_fetch_remote_catalog_parses_image_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /images`` returns ``ImageEntry[]``; we wrap them as
    ``_TuiImage`` rows with the URL synthesised from ``<server>/images/<name>``."""
    payload = [
        {"name": "demo.qcow2", "format": "qcow2", "size_bytes": 1024, "path": "/x"},
        {"name": "live.img.zst", "format": "img.zst", "size_bytes": 4096, "path": "/y"},
    ]
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_args, **_kw: _fake_resp(payload),
    )

    rows = tui_app.fetch_remote_catalog("http://server:8080")

    assert len(rows) == 2
    assert rows[0].name == "demo.qcow2"
    assert rows[0].fmt == "qcow2"
    assert rows[0].size_bytes == 1024
    assert rows[0].url == "http://server:8080/images/demo.qcow2"
    assert rows[0].path is None  # remote rows never get a local path
    assert rows[1].url == "http://server:8080/images/live.img.zst"


def test_fetch_remote_catalog_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server URLs with a trailing slash get normalised; the synthesised
    image URLs are stable regardless of how the operator typed it."""
    payload = [{"name": "demo.qcow2", "format": "qcow2", "size_bytes": 1}]
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_resp(payload),
    )

    rows = tui_app.fetch_remote_catalog("http://server:8080/")
    assert rows[0].url == "http://server:8080/images/demo.qcow2"


def test_fetch_remote_catalog_skips_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries that are not dicts or have no name are skipped; the rest
    are returned. Defensive against a server emitting a partial schema."""
    payload = [
        "not-a-dict",
        {"name": "", "format": "img"},  # blank name
        {"name": "ok.img", "format": "img", "size_bytes": 100},
    ]
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_resp(payload),
    )

    rows = tui_app.fetch_remote_catalog("http://server")
    assert [r.name for r in rows] == ["ok.img"]


def test_fetch_remote_catalog_rejects_non_list_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_resp({"oops": "not a list"}),
    )

    with pytest.raises(ValueError, match="not a list"):
        tui_app.fetch_remote_catalog("http://server")


def test_fetch_remote_catalog_propagates_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)

    with pytest.raises(urllib.error.URLError):
        tui_app.fetch_remote_catalog("http://server")


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


def test_main_accepts_server_and_mac_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bty-tui --server URL --mac MAC`` reaches ``BtyTui(...)`` with
    the right kwargs. The actual ``run()`` is monkeypatched so we
    don't try to launch a real TUI from a unit test."""
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            image_root: object = None,
            *,
            server_url: object = None,
            mac: object = None,
        ) -> None:
            captured["image_root"] = image_root
            captured["server_url"] = server_url
            captured["mac"] = mac

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)

    # Re-import the entry-point to make sure it picks up the patched class.
    import bty.tui as tui_mod

    tui_mod.main(["--server", "http://srv:8080", "--mac", "aa:bb:cc:dd:ee:ff"])

    assert captured["server_url"] == "http://srv:8080"
    assert captured["mac"] == "aa:bb:cc:dd:ee:ff"
    assert captured["ran"] is True


# ---------- end-to-end: BtyTui driven via textual's Pilot ------------------
#
# These run the actual textual ``App`` headless in pytest. The Pilot
# simulates key presses; assertions look at widget state via
# ``app.query_one(...)``. Data sources are monkeypatched on the
# ``bty.tui._app`` module references so the app sees synthetic rows
# without touching the real filesystem / lsblk / network.


def _patch_data_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    images_list: list[images.Image] | None = None,
    disks_list: list[dict[str, Any]] | None = None,
    remote_catalog: list[Any] | None = None,
    geteuid: int = 0,
) -> None:
    """Wire fake data into the module-level references the TUI uses.

    ``images_list`` / ``disks_list`` feed the local-mode populate paths.
    ``remote_catalog`` feeds the remote-mode catalog fetch.
    ``geteuid`` controls the read-only-vs-flashable status string.
    """
    monkeypatch.setattr(tui_app.os, "geteuid", lambda: geteuid)
    monkeypatch.setattr(
        tui_app.images,
        "list_images",
        lambda _root: list(images_list or []),
    )
    monkeypatch.setattr(
        tui_app.disks,
        "list_disks",
        lambda: list(disks_list or []),
    )
    if remote_catalog is not None:
        monkeypatch.setattr(
            tui_app,
            "fetch_remote_catalog",
            lambda _url: list(remote_catalog),
        )


def _spy_status(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every ``_set_status`` call. Cleaner than poking textual
    Static internals (the rendered-text accessor is private)."""
    captured: list[str] = []
    original = tui_app.BtyTui._set_status

    def _capturing(self: tui_app.BtyTui, message: str) -> None:
        captured.append(message)
        original(self, message)

    monkeypatch.setattr(tui_app.BtyTui, "_set_status", _capturing)
    return captured


def test_app_renders_local_panes_with_seeded_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch the app with one seeded image + one seeded disk; both
    DataTables populate and the initial status surfaces the
    flash-allowed prompt (geteuid=0)."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="alpha.qcow2", size=4096)],
        disks_list=[_fake_disk(path="/dev/sda")],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            disks_table = app.query_one("#disks_table", DataTable)
            assert images_table.row_count == 1
            assert disks_table.row_count == 1
            assert "press F to flash" in app._initial_status()  # type: ignore[reportPrivateUsage]

    _run(_drive())


def test_app_shows_no_images_message_when_local_root_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty image root -> ``_set_status`` is called with the
    "No images at <path>; press R to refresh" message during the
    initial populate."""
    statuses = _spy_status(monkeypatch)
    _patch_data_sources(monkeypatch, images_list=[], disks_list=[_fake_disk()])

    app = tui_app.BtyTui(image_root=tmp_path / "empty")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()

    _run(_drive())
    assert any("No images at" in s and "press R to refresh" in s for s in statuses)


def test_app_refresh_action_repopulates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing ``r`` re-runs the populate path. First call returns an
    empty list, second call returns one image; after the keypress the
    images table has the new row and ``Refreshed.`` shows in status."""
    statuses = _spy_status(monkeypatch)
    images_returns = [
        [],  # first populate (on mount)
        [_fake_image()],  # second populate (after R)
    ]
    call_count = 0

    def _list_images(_root: Path) -> list[images.Image]:
        nonlocal call_count
        call_count += 1
        return images_returns.pop(0) if images_returns else []

    monkeypatch.setattr(tui_app.os, "geteuid", lambda: 0)
    monkeypatch.setattr(tui_app.images, "list_images", _list_images)
    monkeypatch.setattr(tui_app.disks, "list_disks", lambda: [_fake_disk()])

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 0  # initial: empty

            await pilot.press("r")
            await pilot.pause()

            assert images_table.row_count == 1  # second populate landed

    _run(_drive())
    assert call_count == 2  # populate ran on mount + on refresh
    assert any("Refreshed" in s for s in statuses)


def test_app_non_root_status_says_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without root (geteuid != 0), ``_initial_status`` returns the
    read-only message so the operator knows ``F`` won't work."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
        geteuid=1000,
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Read-only mode" in app._initial_status()  # type: ignore[reportPrivateUsage]

    _run(_drive())


def test_app_remote_mode_renders_catalog_from_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--server URL`` swaps the local image-root scan for
    ``fetch_remote_catalog``. The Images pane title shows the server
    URL; the table populates from the mocked remote catalog."""
    remote_rows = [
        tui_app._TuiImage(
            name="remote.img.zst",
            fmt="img.zst",
            size_bytes=8192,
            url="http://server:8080/images/remote.img.zst",
        )
    ]
    _patch_data_sources(
        monkeypatch,
        disks_list=[_fake_disk()],
        remote_catalog=remote_rows,
    )

    app = tui_app.BtyTui(server_url="http://server:8080")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1
            # The remote URL is the row key (used to drive the URL flash
            # path in action_flash); confirm the entry round-trips.
            row = next(iter(app._images_by_key.values()))  # type: ignore[reportPrivateUsage]
            assert row.url == "http://server:8080/images/remote.img.zst"
            assert row.path is None  # remote rows never carry a local path

    _run(_drive())
