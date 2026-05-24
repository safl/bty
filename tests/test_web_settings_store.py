"""Tests for ``bty.web._settings_store``.

Direct unit coverage of the upstream-source resolvers (override -> env
-> default precedence). These back the Settings page's "Upstream
sources" controls; getting the precedence wrong silently points the
catalog / release fetch at the wrong place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bty.web import _db, _settings_store
from bty.web._releases import DEFAULT_REPO, ENV_RELEASE_REPO


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_release_repo_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No override, no env -> the built-in default repo."""
    monkeypatch.delenv(ENV_RELEASE_REPO, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_release_repo(conn) == DEFAULT_REPO


def test_release_repo_env_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_release_repo(conn) == "acme/widgets"


def test_release_repo_db_override_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_RELEASE_REPO, "other/repo")
        assert _settings_store.resolve_release_repo(conn) == "other/repo"


def test_catalog_url_derives_from_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit catalog override, the URL is built from the
    effective release repo."""
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        url = _settings_store.resolve_catalog_url(conn)
    assert url == "https://github.com/acme/widgets/releases/latest/download/catalog.toml"


def test_catalog_url_explicit_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(
            conn, _settings_store.KEY_CATALOG_URL, "https://example.test/catalog.toml"
        )
        assert _settings_store.resolve_catalog_url(conn) == "https://example.test/catalog.toml"


def test_release_tag_default_and_override(tmp_path: Path) -> None:
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_release_tag(conn) == _settings_store.DEFAULT_RELEASE_TAG
        _settings_store.set_value(conn, _settings_store.KEY_RELEASE_TAG, "v1.2.3")
        assert _settings_store.resolve_release_tag(conn) == "v1.2.3"


def test_clear_reverts_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_RELEASE_REPO, raising=False)
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_RELEASE_REPO, "other/repo")
        assert _settings_store.resolve_release_repo(conn) == "other/repo"
        _settings_store.clear(conn, _settings_store.KEY_RELEASE_REPO)
        assert _settings_store.resolve_release_repo(conn) == DEFAULT_REPO


# ----- Backup schedule resolvers ----------------------------------------


def test_backup_enabled_default_off(tmp_path: Path) -> None:
    """No override -> False; scheduled backups are off until the operator
    opts in via the Settings form."""
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_backup_enabled(conn) is False


def test_backup_enabled_strict_canonical(tmp_path: Path) -> None:
    """Only the canonical ``"1"`` / ``"0"`` spellings round-trip; anything
    else (legacy ``"true"`` / ``"on"``, empty string, garbage) raises
    :class:`SettingValueError` so a hand-edited state.db is loud rather
    than silently rolling forward as enabled-or-not."""
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, "1")
        assert _settings_store.resolve_backup_enabled(conn) is True
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, "0")
        assert _settings_store.resolve_backup_enabled(conn) is False
        for raw in ("true", "false", "yes", "no", "on", "off", "", "TRUE"):
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, raw)
            with pytest.raises(_settings_store.SettingValueError):
                _settings_store.resolve_backup_enabled(conn)


def test_backup_cadence_default_and_strict_validation(tmp_path: Path) -> None:
    """Unset -> default; known values stick; unknown value RAISES
    rather than silently falling back to the default -- a hand-edited
    cadence has to be corrected before the scheduler will run."""
    default = _settings_store.DEFAULT_BACKUP_CADENCE
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_backup_cadence(conn) == default
        for cadence in _settings_store.BACKUP_CADENCES:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, cadence)
            assert _settings_store.resolve_backup_cadence(conn) == cadence
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, "fortnightly")
        with pytest.raises(_settings_store.SettingValueError):
            _settings_store.resolve_backup_cadence(conn)


def test_backup_retention_default_and_strict_validation(tmp_path: Path) -> None:
    """Unset -> default; positive int sticks; non-numeric or sub-1
    RAISES rather than silently falling back -- pre-1.0 wants loud
    state.db corruption, not silent rollover."""
    with _conn(tmp_path) as conn:
        assert (
            _settings_store.resolve_backup_retention(conn)
            == _settings_store.DEFAULT_BACKUP_RETENTION
        )
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "14")
        assert _settings_store.resolve_backup_retention(conn) == 14
        for raw in ("abc", "0", "-1", "1.5", ""):
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, raw)
            with pytest.raises(_settings_store.SettingValueError):
                _settings_store.resolve_backup_retention(conn)


def test_backup_last_run_at_round_trip(tmp_path: Path) -> None:
    """``last_run_at`` is written by the scheduler, not the form;
    plain string round-trip."""
    with _conn(tmp_path) as conn:
        assert _settings_store.get_backup_last_run_at(conn) is None
        _settings_store.set_backup_last_run_at(conn, "2026-05-24T08:00:00+00:00")
        assert _settings_store.get_backup_last_run_at(conn) == "2026-05-24T08:00:00+00:00"
