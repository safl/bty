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
            "load_catalog_from_source",
            lambda _source, **_kw: list(remote_catalog),
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
            assert app._initial_status() == ""

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
            keys = list(app._images_by_key.values())
            urls = [k.url for k in keys]
            paths = [k.path for k in keys]
            assert "https://example.invalid/bty-server.img.gz" in urls
            assert any(p is not None for p in paths)

    _run(_drive())


def test_action_install_bty_server_preselects_image_and_focuses_disks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``i`` pre-selects the GitHub-latest bty-server image
    and focuses the disks pane so the next Enter commits a disk.
    The flash flow from there is the same as any URL-backed image."""
    _patch_data_sources(
        monkeypatch,
        images_list=[],
        disks_list=[_fake_disk(path="/dev/sda")],
    )

    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            from textual.widgets import DataTable

            selected = app._selected_image
            assert selected is not None
            assert selected.url == tui_app._BTY_SERVER_LATEST_URL
            assert selected.name == tui_app._BTY_SERVER_LATEST_NAME
            assert app.focused is not None
            assert app.focused.id == "disks_table"
            # Disks table still populated -- next Enter commits.
            disks_table = app.query_one("#disks_table", DataTable)
            assert disks_table.row_count == 1

    _run(_drive())


def test_app_shows_no_images_message_when_local_root_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty image root -> ``_set_status`` is called with the
    "No images at <path>. See screen for how to add some." message
    during the initial populate; the welcome panel carries the
    actionable detail."""
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
            assert "Read-only mode" in app._initial_status()

    _run(_drive())


def test_app_catalog_source_overlays_local_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--catalog URL`` runs alongside the local image-root scan and
    surfaces its entries in the catalog table. With an empty local
    image_root, only the catalog rows appear."""
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

    app = tui_app.BtyTui(catalog_source="http://server:8080/catalog.toml")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1
            # The catalog entry's src is the row key (used to drive the
            # URL flash path in action_flash); confirm round-trip.
            row = next(iter(app._images_by_key.values()))
            assert row.url == "http://server:8080/images/remote.img.zst"
            assert row.path is None  # catalog rows never carry a local path

    _run(_drive())


def test_app_catalog_source_oras_failure_does_not_freeze_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ORAS catalog fetch failure must surface in the status line and
    let the TUI keep running -- not propagate an unhandled
    :class:`OrasError` past Textual's event handler (which leaves
    the bottom-line error displayed but the event loop in a
    half-broken state, preventing Esc/q exit). Regression for the
    "TUI freezes after oras token fetch failed" hardware-test
    report.

    Drives :func:`load_catalog_from_source` to raise the same
    :class:`OrasError` that ``oras token fetch failed`` produces;
    asserts the TUI mounts cleanly, renders local rows, and an
    error status was set."""
    from bty import oras as _oras_module

    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="local.qcow2", size=4096)],
        disks_list=[_fake_disk()],
    )

    def _raise_oras_error(_source: str, **_kw: object) -> list[tui_app._TuiImage]:
        raise _oras_module.OrasError(
            "oras token fetch failed for ghcr.io/safl/nosi/ubuntu-sysdev: "
            "<urlopen error [Errno 101] Network is unreachable>"
        )

    monkeypatch.setattr(tui_app, "load_catalog_from_source", _raise_oras_error)
    statuses = _spy_status(monkeypatch)

    app = tui_app.BtyTui(catalog_source="oras://ghcr.io/safl/nosi/ubuntu-sysdev:latest")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            # Local row still rendered -- catalog failure didn't take
            # down the whole populate.
            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1
            # Some flavour of catalog-error message landed in the
            # status line. Match loosely (the exact wording is
            # operator-facing copy; pinning it forces test churn).
            assert any("oras" in s.lower() or "catalog" in s.lower() for s in statuses), (
                f"expected an oras/catalog error in status; got {statuses}"
            )

    _run(_drive())


def test_app_shows_fetching_status_when_catalog_worker_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawning the background catalog worker must surface a
    ``Fetching catalog from ...`` status so the operator knows
    something is happening. Without it, the local-only view
    renders immediately with no signal that more rows may be on
    the way -- confusing on slow networks where the worker can
    take many seconds.

    Drive a slow loader (blocks on a thread Event), assert the
    fetching status appears between mount and worker completion.
    """
    import threading

    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="local.qcow2", size=4096)],
        disks_list=[_fake_disk()],
    )

    release = threading.Event()
    catalog_rows = [
        tui_app._TuiImage(
            name="catalog-row.img.gz",
            fmt="img.gz",
            size_bytes=12345,
            url="http://example/catalog-row.img.gz",
        )
    ]

    def _slow_loader(_source: str, **_kw: object) -> list[tui_app._TuiImage]:
        # Block until the test explicitly releases us; lets the
        # test observe the in-flight state.
        release.wait(timeout=5.0)
        return list(catalog_rows)

    monkeypatch.setattr(tui_app, "load_catalog_from_source", _slow_loader)
    statuses = _spy_status(monkeypatch)

    app = tui_app.BtyTui(catalog_source="https://example/catalog.toml")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            # While the worker is in-flight, the fetching status
            # must have been set at least once.
            assert any("Fetching catalog" in s for s in statuses), (
                f"expected 'Fetching catalog ...' in statuses; got {statuses}"
            )
            # Release the worker so the test doesn't hang on
            # threading.Event.wait().
            release.set()
            await pilot.pause()

    _run(_drive())


def test_action_refresh_retries_catalog_after_earlier_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for: TUI catalog fetch fails (network down), TUI
    caches the failure as an empty list, operator plugs in a USB
    ethernet dongle, connectivity returns, operator presses ``r``
    -- and nothing happens because ``_cached_remote_catalog`` is
    still ``[]`` (not ``None``), so ``_load_images`` short-circuits.

    Drive the catalog loader through one failed and one successful
    call; press ``r`` between them; assert the second populate
    surfaces the now-reachable catalog rows."""
    from bty import oras as _oras_module

    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image(name="local.qcow2", size=4096)],
        disks_list=[_fake_disk()],
    )

    call_count = {"n": 0}
    success_rows = [
        tui_app._TuiImage(
            name="catalog-row.img.gz",
            fmt="img.gz",
            size_bytes=12345,
            url="oras://ghcr.io/safl/nosi/ubuntu-sysdev@sha256:deadbeef",
        )
    ]

    def _flaky_loader(_source: str, **_kw: object) -> list[tui_app._TuiImage]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _oras_module.OrasError(
                "oras token fetch failed for ghcr.io/safl/nosi/ubuntu-sysdev: "
                "<urlopen error [Errno 101] Network is unreachable>"
            )
        return list(success_rows)

    monkeypatch.setattr(tui_app, "load_catalog_from_source", _flaky_loader)

    app = tui_app.BtyTui(catalog_source="oras://ghcr.io/safl/nosi/ubuntu-sysdev:latest")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            # First populate: local row only (catalog fetch failed,
            # cached as empty list).
            assert images_table.row_count == 1
            assert call_count["n"] == 1
            # Press ``r``. Should bust the cache and re-fetch -- the
            # second call returns the catalog row this time.
            await pilot.press("r")
            await pilot.pause()
            assert call_count["n"] == 2, (
                f"refresh should bust the cache and re-fetch; call_count={call_count['n']!r}"
            )
            assert images_table.row_count == 2, (
                f"local + freshly-fetched catalog row expected after refresh; "
                f"got row_count={images_table.row_count}"
            )

    _run(_drive())


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
# TUI polish (theme, filter, welcome panel, details pane, flash modal)
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
    """Empty catalog source -> welcome Static is populated with the
    catalog-source onboarding (catalog URL plus the local-root
    fallback line, and the bty-server install hint)."""
    _patch_data_sources(monkeypatch, disks_list=[_fake_disk()], remote_catalog=[])

    app = tui_app.BtyTui(catalog_source="http://server:8080/catalog.toml")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            welcome = app.query_one("#welcome", Static)
            text_str = str(welcome.content)
            # The configured catalog source URL must be visible.
            assert "http://server:8080/catalog.toml" in text_str
            # Operator still gets the "install bty-server" hint.
            assert "Install bty-server" in text_str

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
            app._filter = "qcow2"
            app._populate_images()
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
            app._filter = "alpha"
            app._populate_images()
            await pilot.pause()
            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1

            # escape should clear the filter
            await pilot.press("escape")
            await pilot.pause()
            assert app._filter == ""
            assert images_table.row_count == 2  # both rows back
            assert any("Filter cleared" in s for s in statuses)

    _run(_drive())


def test_app_no_details_pane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The layout has no Details pane -- the two interactive panes
    (Images / Disks) take the horizontal room. Verify the layout
    does not mount a ``#details-pane`` / ``#details-body`` so a
    regression re-introducing duplicated table-vs-body rendering
    surfaces immediately.
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
# FlashStatusScreen stage tracker
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

    def _fake_execute_plan(plan: Any, progress: Any = None, cancel: Any = None) -> None:
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
            for ev_name, _ in tui_app.FlashStatusScreen._STAGES:
                widget = screen.query_one(f"#stage-{ev_name}", Static)
                assert "done" in widget.classes, (
                    f"stage {ev_name} not marked done: classes={widget.classes}"
                )

            # Close button is now enabled (worker reported success).
            close_btn = screen.query_one("#close", Button)
            assert close_btn.disabled is False

    _run(_drive())


def test_flash_status_screen_cancel_button_sets_event_and_dismisses_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing the Cancel button (or Esc) sets the screen's
    ``threading.Event``, which the flash worker passes to
    ``execute_plan`` as the ``cancel`` callback. The worker's fake
    ``execute_plan`` then raises ``FlashCancelled`` and the screen
    settles into the "cancelled" state with Close enabled."""
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )

    fake_execute_started = asyncio.Event()

    def _fake_execute_plan(plan: Any, progress: Any = None, cancel: Any = None) -> None:
        # Signal the test that the worker is running, then loop
        # polling cancel until it goes True (mirrors the watchdog
        # behaviour in the real flash code).
        import time as _time

        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started"))
            progress(tui_app.flash.FlashProgress(event="writing"))
        # call_from_thread isn't available here (we're in the test
        # thread, not the worker thread the screen would spawn for
        # call_from_thread to reach); just set a plain event.
        fake_execute_started.set()
        for _ in range(50):  # ~5s safety bound
            if cancel is not None and cancel():
                raise tui_app.flash.FlashCancelled("flash cancelled by operator")
            _time.sleep(0.1)
        raise AssertionError("cancel callback never returned True")

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute_plan)

    plan = _fake_flash_plan()
    app = tui_app.BtyTui(image_root=tmp_path / "images")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Button

            screen = tui_app.FlashStatusScreen(plan)
            app.push_screen(screen)
            # Let the worker thread start.
            for _ in range(40):
                await pilot.pause()
                if fake_execute_started.is_set():
                    break
            assert fake_execute_started.is_set(), "worker never ran"

            # Press Esc -- exercises the same ``action_cancel_flash``
            # path as the Cancel button. Picking Esc over a direct
            # ``pilot.click(cancel_btn)`` because the textual pilot's
            # bounds check (textual 8.x on cpython 3.14.5+ in CI)
            # rejects clicks outside the default 80x24 harness
            # screen, and the FlashStatusScreen's 30-row layout
            # pushes the action-bar buttons just past row 24. The
            # binding test covers the same flow without depending
            # on harness-default geometry.
            await pilot.press("escape")
            for _ in range(40):
                await pilot.pause()
                # ``_result`` flips to "cancelled" once the worker
                # raises FlashCancelled and ``_finish`` runs.
                if screen._result is not None:
                    break
            assert screen._result == "cancelled", f"expected cancelled, got {screen._result!r}"
            # Close button is now enabled; Cancel button is now disabled.
            cancel_btn = screen.query_one("#cancel-flash", Button)
            close_btn = screen.query_one("#close", Button)
            assert close_btn.disabled is False
            assert cancel_btn.disabled is True

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

    def _fake_execute_plan(plan: Any, progress: Any = None, cancel: Any = None) -> None:
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

    Regression guard for the ``@work`` decorator contract:
    ``action_flash`` calls ``push_screen_wait`` which Textual 8.x
    rejects outside a worker context with "screen must be from a
    worker when wait_for_dismiss is True". The existing
    ``FlashStatusScreen`` tests instantiate that screen directly
    via ``app.push_screen(...)`` and never go through
    ``action_flash``, so the binding path needs its own coverage.

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
            assert app._stage == tui_app._WizardStage.SELECT_IMAGE
            assert app.focused is not None
            assert app.focused.id == "images_table"

            await pilot.press("enter")
            await pilot.pause()

            assert app._selected_image is not None
            assert app._selected_image.name == "advance.qcow2"
            assert app._stage == tui_app._WizardStage.SELECT_DISK
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
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH

            # Esc -> back to Stage 2.
            await pilot.press("escape")
            await pilot.pause()
            assert app._selected_disk is None
            assert app._selected_image is not None  # image preserved
            assert app._stage == tui_app._WizardStage.SELECT_DISK
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
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH

            # Backspace #1: Stage 3 -> Stage 2.
            await pilot.press("backspace")
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.SELECT_DISK

            # Backspace #2: Stage 2 -> Stage 1.
            await pilot.press("backspace")
            await pilot.pause()
            assert app._stage == tui_app._WizardStage.SELECT_IMAGE
            assert app._selected_image is None
            assert app._selected_disk is None
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

    # First modal is FlashConfirmScreen (returns bool); second is
    # FlashStatusScreen (returns str "ok" / "failed" / "cancelled").
    modal_results: list[Any] = [True, "ok"]
    modal_results_iter = iter(modal_results)

    async def _fake_push_screen_wait(_screen: object) -> Any:
        return next(modal_results_iter)

    monkeypatch.setattr(app, "push_screen_wait", _fake_push_screen_wait)

    async def _drive() -> None:
        from textual.widgets import Button

        async with app.run_test() as pilot:
            await pilot.pause()
            app._selected_image = next(iter(app._images_by_key.values()))
            app._selected_disk = next(iter(app._disks_by_key.values()))
            app._render_status()
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH

            app.action_flash()  # type: ignore[unused-coroutine]
            for _ in range(20):
                await pilot.pause()

            assert app._post_flash is True
            flash_btn = app.query_one("#flash-btn", Button)
            # Button label flipped to "Reboot" so the operator's
            # next press fires the reboot action instead of flash.
            assert str(flash_btn.label) == "Reboot"
            assert flash_btn.disabled is False

    _run(_drive())


def test_action_flash_pxe_done_failure_still_flips_to_reboot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when the flash succeeds but the post-pxe-done
    POST raises URLError (e.g. catalog URL was a static GitHub
    release / non-bty-web HTTP host that does not implement
    ``POST /pxe/<mac>/done`` -> 404), the post-flash UI transition
    must still fire. Previous behaviour: a URLError caused
    ``action_flash`` to return early, leaving ``_post_flash``
    False and the button stuck on ``Flash!`` despite the disk
    having been written. Operator reading the screen saw
    a "Flash!" button after a successful flash and concluded
    the flash had failed.
    """
    _patch_data_sources(
        monkeypatch,
        images_list=[_fake_image()],
        disks_list=[_fake_disk()],
    )
    plan = _fake_flash_plan()
    monkeypatch.setattr(tui_app.flash, "probe_image", lambda _path: plan.image)
    monkeypatch.setattr(tui_app.flash, "probe_target", lambda _path: plan.target)

    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("pxe-done host unreachable / 404")

    monkeypatch.setattr(tui_app, "post_pxe_done", _boom)

    app = tui_app.BtyTui(image_root=tmp_path / "images")
    # Simulate the catalog-source flow that derives a pxe-done base
    # so the failing POST actually fires.
    app._pxe_done_base = "http://server.invalid:8080"
    app._mac = "aa:bb:cc:dd:ee:ff"

    modal_results: list[Any] = [True, "ok"]
    modal_results_iter = iter(modal_results)

    async def _fake_push_screen_wait(_screen: object) -> Any:
        return next(modal_results_iter)

    monkeypatch.setattr(app, "push_screen_wait", _fake_push_screen_wait)

    async def _drive() -> None:
        from textual.widgets import Button

        async with app.run_test() as pilot:
            await pilot.pause()
            app._selected_image = next(iter(app._images_by_key.values()))
            app._selected_disk = next(iter(app._disks_by_key.values()))
            app._render_status()
            assert app._stage == tui_app._WizardStage.CONFIRM_FLASH

            app.action_flash()  # type: ignore[unused-coroutine]
            for _ in range(20):
                await pilot.pause()

            assert app._post_flash is True, (
                "Flash succeeded but _post_flash never flipped -- the URLError "
                "branch was skipping the post-flash UI transition."
            )
            flash_btn = app.query_one("#flash-btn", Button)
            assert str(flash_btn.label) == "Reboot"

    _run(_drive())


def test_catalog_picker_opens_and_dismisses_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``c`` pushes a CatalogSelectScreen modal; pressing
    Esc dismisses it without changing the active catalog.

    Guards against the ``@work`` decorator being dropped from
    ``action_catalog`` -- Textual 8.x requires worker context for
    ``push_screen_wait``.
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
            initial_root = app._image_root
            initial_source = app._catalog_source

            await pilot.press("c")
            for _ in range(10):
                await pilot.pause()

            top = app.screen
            assert isinstance(top, tui_app.CatalogSelectScreen), (
                f"expected CatalogSelectScreen, got {type(top).__name__}"
            )
            top.dismiss(None)
            for _ in range(10):
                await pilot.pause()
            # Catalog unchanged when dismissed without selection.
            assert app._image_root == initial_root
            assert app._catalog_source == initial_source

    _run(_drive())


def test_d_binding_loads_default_catalog_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``d`` sets the catalog source to bty's default
    release-asset URL and derives the pxe-done base from its host.
    The catalog itself is fetched on the next populate (mocked here
    via ``_patch_data_sources(remote_catalog=...)``); the binding's
    job is just to land the new source on the App."""
    remote_rows = [
        tui_app._TuiImage(
            name="nosi-debian",
            fmt="img.gz",
            size_bytes=8192,
            url="oras://ghcr.io/safl/nosi/debian-sysdev:latest",
        )
    ]
    _patch_data_sources(
        monkeypatch,
        disks_list=[_fake_disk()],
        remote_catalog=remote_rows,
    )
    app = tui_app.BtyTui(image_root=tmp_path / "empty")

    async def _drive() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._catalog_source is None

            await pilot.press("d")
            for _ in range(10):
                await pilot.pause()

            # Catalog source flipped to bty's published catalog URL.
            assert app._catalog_source == tui_app._BTY_DEFAULT_CATALOG_URL
            # pxe-done base derived from the URL's host (so a TUI
            # launched without --mac/--catalog can still POST done
            # to bty-web instances reached via the default catalog).
            assert app._pxe_done_base == "https://github.com"
            # The catalog row from the mocked source landed in the
            # table; confirms repopulate triggered.
            from textual.widgets import DataTable

            images_table = app.query_one("#images_table", DataTable)
            assert images_table.row_count == 1

    _run(_drive())
