"""Tests for ``bty.web._withcache`` and the withcache-url setting.

Covers the origin -> withcache serve-URL encoding (the contract with
withcache's own ``/b/`` decoding), the graceful ``is_cached`` HEAD probe
(hit / miss / unreachable), and the ``resolve_withcache_url`` precedence
(override -> env -> None).
"""

from __future__ import annotations

import base64
import http.server
import socketserver
import sqlite3
import threading
from pathlib import Path

import pytest

from bty.web import _db, _settings_store, _withcache


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_blob_url_encodes_origin_and_keeps_basename() -> None:
    origin = "https://github.com/safl/bty/releases/download/v1/bty-server.img.gz"
    url = _withcache.blob_url("http://cache:3000/", origin)
    assert url.startswith("http://cache:3000/b/")  # trailing slash trimmed
    assert url.endswith("/bty-server.img.gz")  # cosmetic basename preserved
    token = url[len("http://cache:3000/b/") :].split("/")[0]
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    assert decoded == origin  # withcache can recover the exact origin


class _Cache(http.server.BaseHTTPRequestHandler):
    """Stand-in for withcache: 200 for one known token path, else 404."""

    cached_path = ""

    def do_HEAD(self) -> None:
        self.send_response(200 if self.path == self.cached_path else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass


def _serve() -> tuple[socketserver.TCPServer, str]:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Cache)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_is_cached_hit_and_miss() -> None:
    httpd, base = _serve()
    try:
        origin = "https://h/p/x.img.gz"
        # Tell the stub which path counts as cached.
        _Cache.cached_path = "/" + _withcache.blob_url(base, origin).split("/", 3)[3]
        assert _withcache.is_cached(base, origin) is True
        assert _withcache.is_cached(base, "https://h/p/other.img.gz") is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_is_cached_unreachable_is_false() -> None:
    # Nothing listening on this port -> graceful False, never raises.
    assert _withcache.is_cached("http://127.0.0.1:9", "https://h/x", timeout=0.5) is False


def test_resolve_withcache_url_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_settings_store.ENV_WITHCACHE_URL, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_withcache_url(conn) is None  # unset
        monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://env-cache:3000")
        assert _settings_store.resolve_withcache_url(conn) == "http://env-cache:3000"
        _settings_store.set_value(conn, _settings_store.KEY_WITHCACHE_URL, "http://db:3000")
        assert _settings_store.resolve_withcache_url(conn) == "http://db:3000"  # override wins


def test_resolve_withcache_url_reads_cfg_from_bty_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the URL lives in bty.toml ([withcache] url) on v0.42+
    container deploys, and the slim compose/Quadlet no longer set
    $BTY_WITHCACHE_URL. The resolver MUST consult cfg.withcache.url, or
    withcache is silently bypassed on the flash path. Precedence:
    DB override > cfg.withcache.url > $BTY_WITHCACHE_URL > None."""
    from bty.web import _config

    monkeypatch.delenv(_settings_store.ENV_WITHCACHE_URL, raising=False)
    toml = tmp_path / "bty.toml"
    toml.write_text('[withcache]\nurl = "http://from-toml:3000"\n', encoding="utf-8")
    _config.set_active_config(_config.load_config([toml]))

    with _conn(tmp_path) as conn:
        # No DB key, no env -> cfg.withcache.url wins.
        assert _settings_store.resolve_withcache_url(conn) == "http://from-toml:3000"
        # A DB override still beats bty.toml.
        _settings_store.set_value(conn, _settings_store.KEY_WITHCACHE_URL, "http://db:3000")
        assert _settings_store.resolve_withcache_url(conn) == "http://db:3000"

    # Restore the empty-config default so later tests aren't polluted.
    _config.set_active_config(_config.load_config([]))
