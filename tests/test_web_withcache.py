"""Tests for ``bty.web._withcache`` and the withcache-url setting.

Covers the origin -> withcache serve-URL encoding (the contract
with withcache's own ``/b/`` decoding) + the
``resolve_withcache_url`` precedence (override -> env -> None).
The runtime HEAD probe (``is_cached``) went away in v0.68.0 --
since withcache v0.11.0 the catalog surface already guarantees
readiness, so there's nothing to probe.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from bty.web import _db, _settings_store, _withcache


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_blob_url_encodes_origin_and_keeps_basename() -> None:
    origin = "https://github.com/safl/bty/releases/download/v1/bty-server.img.gz"
    url = _withcache.blob_url("http://cache:8081/", origin)
    assert url.startswith("http://cache:8081/b/")  # trailing slash trimmed
    assert url.endswith("/bty-server.img.gz")  # cosmetic basename preserved
    token = url[len("http://cache:8081/b/") :].split("/")[0]
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    assert decoded == origin  # withcache can recover the exact origin


def test_resolve_withcache_url_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_settings_store.ENV_WITHCACHE_URL, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_withcache_url(conn) is None  # unset
        monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://env-cache:8081")
        assert _settings_store.resolve_withcache_url(conn) == "http://env-cache:8081"
        _settings_store.set_value(conn, _settings_store.KEY_WITHCACHE_URL, "http://db:8081")
        assert _settings_store.resolve_withcache_url(conn) == "http://db:8081"  # override wins


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
    toml.write_text('[withcache]\nurl = "http://from-toml:8081"\n', encoding="utf-8")
    _config.set_active_config(_config.load_config([toml]))

    with _conn(tmp_path) as conn:
        # No DB key, no env -> cfg.withcache.url wins.
        assert _settings_store.resolve_withcache_url(conn) == "http://from-toml:8081"
        # A DB override still beats bty.toml.
        _settings_store.set_value(conn, _settings_store.KEY_WITHCACHE_URL, "http://db:8081")
        assert _settings_store.resolve_withcache_url(conn) == "http://db:8081"

    # Restore the empty-config default so later tests aren't polluted.
    _config.set_active_config(_config.load_config([]))
