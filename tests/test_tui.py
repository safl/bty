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
    """``GET /images`` returns ImageEntry[] with a single ``url``
    each. The TUI just unpacks it -- the server already chose
    server-vs-upstream based on cache state. Mixed shape here
    (one server URL, one upstream URL) verifies neither side
    gets special-cased on the client."""
    payload = [
        {
            "name": "demo.qcow2",
            "format": "qcow2",
            "size_bytes": 1024,
            "url": "http://server:8080/images/abc123def456",
            "ref": "abc123def456",
            "cached": True,
        },
        {
            "name": "live.img.zst",
            "format": "img.zst",
            "size_bytes": 4096,
            "url": "https://github.com/safl/bty-images/releases/download/v1/live.img.zst",
            "ref": "fedcba987654",
            "cached": False,
        },
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
    assert rows[0].url == "http://server:8080/images/abc123def456"
    assert rows[0].path is None  # remote rows never get a local path
    assert rows[1].url == "https://github.com/safl/bty-images/releases/download/v1/live.img.zst"


def test_fetch_remote_catalog_skips_entries_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries without a ``url`` (or with an empty one) are skipped --
    the server is supposed to elide them too, but the client is
    defensive. Same for entries that aren't dicts or have no name."""
    payload = [
        "not-a-dict",
        {"name": "", "url": "http://x"},  # blank name
        {"name": "no-url", "format": "img"},  # no url
        {"name": "ok.img", "format": "img", "size_bytes": 100, "url": "http://x/ok"},
    ]
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_resp(payload),
    )

    rows = tui_app.fetch_remote_catalog("http://server")
    assert [r.name for r in rows] == ["ok.img"]
    assert rows[0].url == "http://x/ok"


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
            server_url: object = None,
            mac: object = None,
        ) -> None:
            captured["image_root"] = image_root
            captured["server_url"] = server_url
            captured["mac"] = mac

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)
    import bty.tui as tui_mod

    tui_mod.main(["--image-root", "/tmp/bty-images"])

    assert captured["image_root"] == Path("/tmp/bty-images")
    assert captured["server_url"] is None
    assert captured["mac"] is None
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
    remote_images_list: list[images.RemoteImage] | None = None,
    disks_list: list[dict[str, Any]] | None = None,
    remote_catalog: list[Any] | None = None,
    geteuid: int = 0,
) -> None:
    """Wire fake data into the module-level references the TUI uses.

    ``images_list`` / ``disks_list`` feed the local-mode populate paths.
    ``remote_images_list`` feeds the local-mode ``.bri`` scan.
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
        tui_app.images,
        "list_remote_images",
        lambda _root: list(remote_images_list or []),
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
    DataTables populate. Initial status is empty when running as
    root (the wizard's pane labels carry the prompt instead);
    non-root surfaces the read-only mode message instead."""
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
            # Root user: initial status is empty (no nudge text;
            # the verbose pane border-titles carry the wizard
            # prompts instead).
            assert app._initial_status() == ""  # type: ignore[reportPrivateUsage]

    _run(_drive())


def test_app_local_mode_renders_bri_descriptors_alongside_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.bri`` descriptors in BTY_IMAGES surface as catalog rows
    next to local images, with ``url`` populated rather than
    ``path`` so flash dispatch can branch on origin."""
    bri_path = tmp_path / "images" / "bty-server.bri"
    remote = images.RemoteImage(
        name="bty-server.img.gz",
        url="https://example.invalid/bty-server.img.gz",
        path=bri_path,
        format="img.gz",
    )
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="alpha.qcow2", size=4096)],
        remote_images_list=[remote],
        disks_list=[_fake_disk(path="/dev/sda")],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 2
            # _images_by_key should have one local + one remote entry.
            keys = list(app._images_by_key.values())  # type: ignore[reportPrivateUsage]
            urls = [k.url for k in keys]
            paths = [k.path for k in keys]
            assert "https://example.invalid/bty-server.img.gz" in urls
            assert any(p is not None for p in paths)

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
    assert any("No images at" in s and "how to add some" in s for s in statuses)


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


# --------------------------------------------------------------------------
# M20: TUI polish (theme, filter, welcome panel, details pane, flash modal)
# --------------------------------------------------------------------------


def test_app_uses_tokyo_night_theme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The app picks Tokyo Night on mount (matches the bty mascot's
    navy + warm-yellow palette)."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.theme == "tokyo-night"

    _run(_drive())


def test_app_welcome_panel_has_local_onboarding_text_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty local catalog -> welcome Static is populated with
    local-mode onboarding (BTY_IMAGES partition guidance)."""
    _patch_data_sources(monkeypatch, images_list=[], disks_list=[_fake_disk()])

    app = tui_app.BtyTui(image_root=tmp_path / "empty")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            welcome = app.query_one("#welcome", Static)
            text_str = str(welcome.content)
            # Local-mode markers: BTY_IMAGES partition + image-root path
            assert "BTY_IMAGES" in text_str
            assert str(tmp_path / "empty") in text_str

    _run(_drive())


def test_app_welcome_panel_has_remote_onboarding_text_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty remote catalog -> welcome Static is populated with
    remote-mode onboarding (PUT /images guidance)."""
    _patch_data_sources(monkeypatch, disks_list=[_fake_disk()], remote_catalog=[])

    app = tui_app.BtyTui(server_url="http://server:8080")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            welcome = app.query_one("#welcome", Static)
            text_str = str(welcome.content)
            # Remote-mode markers: server URL + the PUT example
            assert "http://server:8080/images" in text_str
            assert "curl -X PUT" in text_str

    _run(_drive())


def test_app_welcome_panel_clears_when_catalog_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-empty catalog -> welcome Static is cleared so the panel
    collapses and the table fills the space."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            welcome = app.query_one("#welcome", Static)
            text_str = str(welcome.content)
            assert text_str == ""

    _run(_drive())


def test_app_filter_action_focuses_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/`` makes the filter Input visible + focused so the operator
    can type a substring. Pressing it from the table (initial focus)
    should not type ``/`` into the table."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Input

            await pilot.press("slash")
            await pilot.pause()
            filter_input = app.query_one("#filter-input", Input)
            assert filter_input.has_class("active")
            assert filter_input.has_focus

    _run(_drive())


def test_app_filter_narrows_catalog_to_matching_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submitting the filter Input narrows the images table to rows
    whose name contains the substring (case-insensitive). Other
    rows go away; non-matching filter -> empty table."""
    _patch_data_sources(
        monkeypatch,
        images_list=[
            _fake_image(name="alpha.qcow2"),
            _fake_image(name="beta.img.zst"),
            _fake_image(name="gamma.qcow2"),
        ],
        disks_list=[_fake_disk()],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable, Input

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 3  # all three pre-filter

            # Apply the filter via the Input's value (instead of
            # typing each character through pilot, which is slow and
            # easy to fight focus on).
            filter_input = app.query_one("#filter-input", Input)
            filter_input.value = "qcow2"
            # Trigger the submitted handler the way Enter does.
            app._filter = "qcow2"  # type: ignore[reportPrivateUsage]
            app._populate_images()  # type: ignore[reportPrivateUsage]
            await pilot.pause()
            assert images_table.row_count == 2  # alpha + gamma match qcow2

    _run(_drive())


def test_app_filter_escape_clears_and_repopulates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``escape`` clears the active filter, re-populates the catalog,
    and emits a ``Filter cleared.`` status."""
    _patch_data_sources(
        monkeypatch,
        images_list=[
            _fake_image(name="alpha.qcow2"),
            _fake_image(name="beta.img.zst"),
        ],
        disks_list=[_fake_disk()],
    )
    statuses = _spy_status(monkeypatch)

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            # Apply a filter that excludes ``beta``
            app._filter = "alpha"  # type: ignore[reportPrivateUsage]
            app._populate_images()  # type: ignore[reportPrivateUsage]
            await pilot.pause()
            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1

            # escape should clear the filter
            await pilot.press("escape")
            await pilot.pause()
            assert app._filter == ""  # type: ignore[reportPrivateUsage]
            assert images_table.row_count == 2  # both rows back
            assert any("Filter cleared" in s for s in statuses)

    _run(_drive())


def test_app_no_details_pane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Details pane was removed in v0.5.x to give the two
    interactive panes (Images / Disks) more horizontal room.
    Verify the layout no longer mounts a ``#details-pane`` /
    ``#details-body`` so future regressions that re-introduce
    duplicated table-vs-body rendering surface immediately.
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.query("#details-pane")
            assert not app.query("#details-body")

    _run(_drive())


# --------------------------------------------------------------------------
# FlashStatusScreen stage tracker (M20)
# --------------------------------------------------------------------------


def _fake_flash_plan() -> tui_app.flash.FlashPlan:
    """Synthesize a minimal FlashPlan for FlashStatusScreen tests.

    The plan's content doesn't matter; we mock ``flash.execute_plan``
    so it never actually touches the disk.
    """
    image = tui_app.flash.ImageInfo(
        path=Path("/fake/images/demo.qcow2"),
        format="qcow2",
        size_bytes=1024,
        virtual_size_bytes=2048,
    )
    target = tui_app.flash.TargetInfo(
        path=Path("/dev/sdz"),
        exists=True,
        is_block_device=True,
        size_bytes=8192,
        mountpoints=[],
    )
    return tui_app.flash.FlashPlan(image=image, target=target)


def test_flash_status_screen_ticks_stages_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As ``flash.execute_plan`` emits each lifecycle event the stage
    tracker advances: prior stages get ``done``, the active one gets
    ``active``. After completion all five stages (started, writing,
    synced, partprobed, done) are marked ``done``."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    def _fake_execute_plan(plan: Any, progress: Any = None) -> None:
        # Walk through the four lifecycle events the screen renders
        # as stages (the fifth, ``done``, is ticked by the screen
        # itself after execute_plan returns).
        if progress is None:
            return
        for ev in ("started", "writing", "synced", "partprobed"):
            progress(tui_app.flash.FlashProgress(event=ev))

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute_plan)

    plan = _fake_flash_plan()
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Button, Static

            screen = tui_app.FlashStatusScreen(plan)
            app.push_screen(screen)
            # Drain the worker thread + queued call_from_thread updates.
            for _ in range(20):
                await pilot.pause()

            # All five stages should be done.
            for ev_name, _ in tui_app.FlashStatusScreen._STAGES:  # type: ignore[reportPrivateUsage]
                widget = screen.query_one(f"#stage-{ev_name}", Static)
                assert "done" in widget.classes, (
                    f"stage {ev_name} not marked done: classes={widget.classes}"
                )

            # Close button is now enabled (worker reported success).
            close_btn = screen.query_one("#close", Button)
            assert close_btn.disabled is False

    _run(_drive())


def test_flash_status_screen_marks_failed_stage_on_FlashError(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``flash.execute_plan`` raises a ``FlashError`` partway
    through, the currently-active stage is marked ``failed`` (not
    done) and the close button enables so the operator can dismiss."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    def _fake_execute_plan(plan: Any, progress: Any = None) -> None:
        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started"))
            progress(tui_app.flash.FlashProgress(event="writing"))
        raise tui_app.flash.FlashError("boom: simulated write failure")

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute_plan)

    plan = _fake_flash_plan()
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Button, Static

            screen = tui_app.FlashStatusScreen(plan)
            app.push_screen(screen)
            for _ in range(20):
                await pilot.pause()

            # ``writing`` was active when the failure landed; expect
            # it marked ``failed``. Earlier ``started`` is ``done``;
            # later stages are still ``pending``.
            started = screen.query_one("#stage-started", Static)
            writing = screen.query_one("#stage-writing", Static)
            synced = screen.query_one("#stage-synced", Static)
            assert "done" in started.classes
            assert "failed" in writing.classes
            assert "pending" in synced.classes

            close_btn = screen.query_one("#close", Button)
            assert close_btn.disabled is False  # enabled to let operator dismiss

    _run(_drive())


def test_action_flash_pushes_confirm_modal_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``f`` with an image + disk in the catalogs pushes the
    FlashConfirmScreen modal without raising.

    Regression test for the v0.5.4 ``@work`` decorator gap: before
    that fix, ``action_flash`` was a plain ``async def`` and called
    ``push_screen_wait`` which Textual 8.x rejects outside a worker
    context with "screen must be from a worker when wait_for_dismiss
    is True". The bug shipped through several releases because no
    test exercised the binding end-to-end -- the existing
    ``FlashStatusScreen`` tests instantiate that screen directly via
    ``app.push_screen(...)`` and never go through ``action_flash``.

    This test drives the binding via Pilot.press("f") and asserts
    that within a few event-loop turns the FlashConfirmScreen is on
    the screen stack. If a future change drops or misconfigures the
    ``@work`` decorator on ``action_flash``, this test surfaces it.
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="probe-test.qcow2")],
        disks_list=[_fake_disk()],
    )

    # ``action_flash`` calls ``flash.probe_image`` (real qcow2 read)
    # and ``flash.probe_target`` (real block-device introspection);
    # stub both so the test runs without the synthetic paths
    # needing to exist.
    plan = _fake_flash_plan()
    monkeypatch.setattr(tui_app.flash, "probe_image", lambda _path: plan.image)
    monkeypatch.setattr(tui_app.flash, "probe_target", lambda _path: plan.target)

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f")
            # The worker spins up, builds the plan, calls
            # ``push_screen_wait``. A few pause turns let the worker
            # run far enough for the modal to mount.
            for _ in range(20):
                await pilot.pause()
            # Two scenarios both prove the worker context is right:
            # the modal is on top, OR the worker already finished and
            # the screen stack is back to the main screen. The bug
            # we're guarding against would have raised ScreenStackError
            # before either of those states could be reached -- the
            # ``run_test`` context manager would propagate the exception.
            top = app.screen
            assert isinstance(top, (tui_app.FlashConfirmScreen, type(top))), (
                f"unexpected screen on stack: {type(top).__name__}"
            )
            # If the modal is up, dismiss it cleanly so the worker
            # finishes and the test exits without hanging.
            if isinstance(top, tui_app.FlashConfirmScreen):
                top.dismiss(False)
                for _ in range(10):
                    await pilot.pause()

    _run(_drive())


def test_wizard_advances_on_image_row_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter on an image row commits the selection,
    advances the wizard to Stage 2 (SELECT_DISK), and moves focus
    to the Disks table."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="advance.qcow2")],
        disks_list=[_fake_disk()],
    )
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Initial state: Stage 1, focus on images_table.
            assert app._stage == tui_app._WizardStage.SELECT_IMAGE  # type: ignore[reportPrivateUsage]
            assert app.focused is not None
            assert app.focused.id == "images_table"

            await pilot.press("enter")
            await pilot.pause()

            assert app._selected_image is not None  # type: ignore[reportPrivateUsage]
            assert app._selected_image.name == "advance.qcow2"  # type: ignore[reportPrivateUsage]
            assert app._stage == tui_app._WizardStage.SELECT_DISK  # type: ignore[reportPrivateUsage]
            assert app.focused is not None
            assert app.focused.id == "disks_table"

    _run(_drive())


def test_wizard_back_clears_disk_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From Stage 3, Esc clears _selected_disk and returns the
    wizard to Stage 2 with focus on the Disks table."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="back.qcow2")],
        disks_list=[_fake_disk()],
    )
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Drive to Stage 3 by committing image then disk.
            await pilot.press("enter")  # Stage 1 -> Stage 2
            await pilot.pause()
            await pilot.press("enter")  # Stage 2 -> Stage 3
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH  # type: ignore[reportPrivateUsage]

            # Esc -> back to Stage 2.
            await pilot.press("escape")
            await pilot.pause()
            assert app._selected_disk is None  # type: ignore[reportPrivateUsage]
            assert app._selected_image is not None  # type: ignore[reportPrivateUsage]  # image preserved
            assert app._stage == tui_app._WizardStage.SELECT_DISK  # type: ignore[reportPrivateUsage]
            assert app.focused is not None
            assert app.focused.id == "disks_table"

    _run(_drive())


def test_wizard_back_via_backspace_all_the_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backspace is wired as an alias for Esc on the back binding;
    pressing it twice from Stage 3 should clear both selections
    and land on Stage 1 with focus on the Images table.
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="back.qcow2")],
        disks_list=[_fake_disk()],
    )
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH  # type: ignore[reportPrivateUsage]

            # Backspace #1: Stage 3 -> Stage 2.
            await pilot.press("backspace")
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.SELECT_DISK  # type: ignore[reportPrivateUsage]

            # Backspace #2: Stage 2 -> Stage 1.
            await pilot.press("backspace")
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.SELECT_IMAGE  # type: ignore[reportPrivateUsage]
            assert app._selected_image is None  # type: ignore[reportPrivateUsage]
            assert app._selected_disk is None  # type: ignore[reportPrivateUsage]
            assert app.focused is not None
            assert app.focused.id == "images_table"

    _run(_drive())


def test_action_flash_success_flips_button_to_reboot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end happy path: when both modals dismiss with True,
    ``action_flash`` flips ``_post_flash`` and the action-pane
    button transforms from ``Flash!`` into ``Reboot``.

    Stubs ``push_screen_wait`` to skip the modals; flash pipeline
    (probe / make_plan / validate_plan) stubbed so the test
    isolates the post-success state transition.
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )
    plan = _fake_flash_plan()
    monkeypatch.setattr(tui_app.flash, "probe_image", lambda _path: plan.image)
    monkeypatch.setattr(tui_app.flash, "probe_target", lambda _path: plan.target)

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    confirmed_then_success = iter([True, True])

    async def _fake_push_screen_wait(_screen: object) -> bool:
        return next(confirmed_then_success)

    monkeypatch.setattr(app, "push_screen_wait", _fake_push_screen_wait)

    async def _drive() -> None:
        from textual.widgets import Button

        async with app.run_test() as pilot:
            await pilot.pause()
            app._selected_image = next(  # type: ignore[reportPrivateUsage]
                iter(app._images_by_key.values())  # type: ignore[reportPrivateUsage]
            )
            app._selected_disk = next(  # type: ignore[reportPrivateUsage]
                iter(app._disks_by_key.values())  # type: ignore[reportPrivateUsage]
            )
            app._render_status()  # type: ignore[reportPrivateUsage]
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH  # type: ignore[reportPrivateUsage]

            app.action_flash()  # type: ignore[unused-coroutine]
            for _ in range(20):
                await pilot.pause()

            assert app._post_flash is True  # type: ignore[reportPrivateUsage]
            flash_btn = app.query_one("#flash-btn", Button)
            # Button label flipped to "Reboot" so the operator's
            # next press fires the reboot action instead of flash.
            assert str(flash_btn.label) == "Reboot"
            assert flash_btn.disabled is False

    _run(_drive())


def test_source_picker_opens_and_dismisses_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``s`` pushes a SourceSelectScreen modal; pressing
    Esc dismisses it without changing the active source.

    Same regression-class as ``test_theme_picker_*`` and
    ``test_action_flash_*`` -- guards against the ``@work``
    decorator being dropped from ``action_source`` (Textual 8.x
    requires worker context for ``push_screen_wait``).
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            initial_root = app._image_root  # type: ignore[reportPrivateUsage]
            initial_server = app._server_url  # type: ignore[reportPrivateUsage]

            await pilot.press("s")
            for _ in range(10):
                await pilot.pause()

            top = app.screen
            assert isinstance(top, tui_app.SourceSelectScreen), (
                f"expected SourceSelectScreen, got {type(top).__name__}"
            )
            top.dismiss(None)
            for _ in range(10):
                await pilot.pause()
            # Source unchanged when dismissed without selection.
            assert app._image_root == initial_root  # type: ignore[reportPrivateUsage]
            assert app._server_url == initial_server  # type: ignore[reportPrivateUsage]

    _run(_drive())
