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


def test_backup_enabled_truthy_spellings(tmp_path: Path) -> None:
    """Tolerant boolean parsing -- ``1`` is the canonical value the form
    writes, but ``true`` / ``yes`` / ``on`` survive a hand-edit of
    state.db."""
    with _conn(tmp_path) as conn:
        for raw in ("1", "true", "TRUE", "yes", "on"):
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, raw)
            assert _settings_store.resolve_backup_enabled(conn) is True, raw
        for raw in ("0", "false", "no", "off", ""):
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, raw)
            assert _settings_store.resolve_backup_enabled(conn) is False, raw


def test_backup_cadence_default_and_validation(tmp_path: Path) -> None:
    """Unset -> default; unknown value -> default; known values stick."""
    default = _settings_store.DEFAULT_BACKUP_CADENCE
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_backup_cadence(conn) == default
        for cadence in _settings_store.BACKUP_CADENCES:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, cadence)
            assert _settings_store.resolve_backup_cadence(conn) == cadence
        # Hand-edited typo / unknown value falls back to default rather
        # than wedging the scheduler.
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, "fortnightly")
        assert _settings_store.resolve_backup_cadence(conn) == default


def test_backup_retention_default_and_validation(tmp_path: Path) -> None:
    """Unset -> default; non-numeric -> default; sub-1 -> default;
    positive int sticks."""
    with _conn(tmp_path) as conn:
        assert (
            _settings_store.resolve_backup_retention(conn)
            == _settings_store.DEFAULT_BACKUP_RETENTION
        )
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "14")
        assert _settings_store.resolve_backup_retention(conn) == 14
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "abc")
        assert (
            _settings_store.resolve_backup_retention(conn)
            == _settings_store.DEFAULT_BACKUP_RETENTION
        )
        _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "0")
        assert (
            _settings_store.resolve_backup_retention(conn)
            == _settings_store.DEFAULT_BACKUP_RETENTION
        )


def test_backup_last_run_at_round_trip(tmp_path: Path) -> None:
    """``last_run_at`` is written by the scheduler, not the form;
    plain string round-trip."""
    with _conn(tmp_path) as conn:
        assert _settings_store.get_backup_last_run_at(conn) is None
        _settings_store.set_backup_last_run_at(conn, "2026-05-24T08:00:00+00:00")
        assert _settings_store.get_backup_last_run_at(conn) == "2026-05-24T08:00:00+00:00"
