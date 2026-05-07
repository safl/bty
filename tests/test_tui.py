"""Tests for the bty-tui free helpers.

Driving the textual ``App`` itself in unit tests is painful (event-loop
+ DOM-renderer setup); we cover the helpers around it instead. The
helpers (``fetch_remote_catalog``, ``post_pxe_done``) are the
network-touching pieces and are where remote-mode logic lives.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock

import pytest

from bty.tui import _app as tui_app


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
