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
from bty.web._releases import DEFAULT_NETBOOT_REPO, ENV_RELEASE_REPO


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_netboot_repo_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No override, no env -> the built-in default netboot repo (safl/bty)."""
    monkeypatch.delenv(ENV_RELEASE_REPO, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_netboot_repo(conn) == DEFAULT_NETBOOT_REPO


def test_netboot_repo_env_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_netboot_repo(conn) == "acme/widgets"


def test_netboot_repo_db_override_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_NETBOOT_REPO, "other/repo")
        assert _settings_store.resolve_netboot_repo(conn) == "other/repo"


def test_catalog_url_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No override -> the built-in default catalog URL (nosi's
    /releases/latest/download/catalog.toml). The catalog URL has NO
    env-layer fallback; only an explicit Settings override beats it."""
    monkeypatch.delenv(ENV_RELEASE_REPO, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_catalog_url(conn) == _settings_store.DEFAULT_CATALOG_URL


def test_catalog_url_independent_of_netboot_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$BTY_BOOT_RELEASE_REPO`` only repoints the netboot repo;
    the catalog URL stays on its default until explicitly overridden."""
    monkeypatch.setenv(ENV_RELEASE_REPO, "acme/widgets")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_netboot_repo(conn) == "acme/widgets"
        assert _settings_store.resolve_catalog_url(conn) == _settings_store.DEFAULT_CATALOG_URL


def test_catalog_url_override_wins(tmp_path: Path) -> None:
    """A ``KEY_CATALOG_URL`` override replaces the default verbatim;
    no repo/tag composition, no URL rewriting. The fetch handler GETs
    whatever string the operator pasted."""
    custom = "https://example.invalid/path/catalog.toml"
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_CATALOG_URL, custom)
        assert _settings_store.resolve_catalog_url(conn) == custom


def test_netboot_tag_default_and_override(tmp_path: Path) -> None:
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_netboot_tag(conn) == _settings_store.DEFAULT_TAG
        _settings_store.set_value(conn, _settings_store.KEY_NETBOOT_TAG, "v1.2.3")
        assert _settings_store.resolve_netboot_tag(conn) == "v1.2.3"


def test_clear_reverts_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_RELEASE_REPO, raising=False)
    custom = "https://example.invalid/forks/catalog.toml"
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_NETBOOT_REPO, "fork/netboot")
        _settings_store.set_value(conn, _settings_store.KEY_CATALOG_URL, custom)
        assert _settings_store.resolve_netboot_repo(conn) == "fork/netboot"
        assert _settings_store.resolve_catalog_url(conn) == custom
        _settings_store.clear(conn, _settings_store.KEY_NETBOOT_REPO)
        _settings_store.clear(conn, _settings_store.KEY_CATALOG_URL)
        assert _settings_store.resolve_netboot_repo(conn) == DEFAULT_NETBOOT_REPO
        assert _settings_store.resolve_catalog_url(conn) == _settings_store.DEFAULT_CATALOG_URL


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


def test_display_timezone_default_is_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No override + no env -> UTC. The bty storage standard."""
    monkeypatch.delenv(_settings_store.ENV_DISPLAY_TZ, raising=False)
    with _conn(tmp_path) as conn:
        tz = _settings_store.resolve_display_timezone(conn)
        assert str(tz) == "UTC"


def test_display_timezone_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_settings_store.ENV_DISPLAY_TZ, "Europe/Copenhagen")
    with _conn(tmp_path) as conn:
        tz = _settings_store.resolve_display_timezone(conn)
        assert str(tz) == "Europe/Copenhagen"


def test_display_timezone_db_override_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_settings_store.ENV_DISPLAY_TZ, "Europe/Copenhagen")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_DISPLAY_TZ, "America/New_York")
        tz = _settings_store.resolve_display_timezone(conn)
        assert str(tz) == "America/New_York"


def test_display_timezone_invalid_raises(tmp_path: Path) -> None:
    """A garbage stored value raises SettingValueError so the renderer
    surfaces the error rather than silently falling back to UTC (the
    form validates before persisting; this fires only on hand-edited
    state.db or stale rows)."""
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_DISPLAY_TZ, "Not/A/Real/Zone")
        with pytest.raises(_settings_store.SettingValueError):
            _settings_store.resolve_display_timezone(conn)


def test_display_timezone_empty_string_falls_through_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty stored value (or empty env) -> UTC, not a parse error."""
    monkeypatch.delenv(_settings_store.ENV_DISPLAY_TZ, raising=False)
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_DISPLAY_TZ, "")
        assert str(_settings_store.resolve_display_timezone(conn)) == "UTC"


# ----- Ramboot resolvers ------------------------------------------------


def test_nbdmux_url_unset_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No DB override + no env + no bty.toml -> None (ramboot unavailable)."""
    monkeypatch.delenv(_settings_store.ENV_NBDMUX_URL, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_nbdmux_url(conn) is None


def test_nbdmux_url_env_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_settings_store.ENV_NBDMUX_URL, "http://nbdmux-env:8082")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_nbdmux_url(conn) == "http://nbdmux-env:8082"


def test_nbdmux_url_db_override_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_settings_store.ENV_NBDMUX_URL, "http://nbdmux-env:8082")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_NBDMUX_URL, "http://nbdmux-db:8082")
        assert _settings_store.resolve_nbdmux_url(conn) == "http://nbdmux-db:8082"


def test_nbdmux_url_reads_cfg_from_bty_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression symmetric with the withcache-url version: on v0.42+
    container deploys the URL lives in ``[nbdmux] url`` of bty.toml
    and the slim compose / Quadlet no longer sets
    ``$BTY_NBDMUX_URL``. The resolver MUST consult
    ``cfg().nbdmux.url``, else ramboot is silently unavailable
    despite what the operator wrote in the config file. Precedence:
    DB override > cfg.nbdmux.url > $BTY_NBDMUX_URL > None."""
    from bty.web import _config

    monkeypatch.delenv(_settings_store.ENV_NBDMUX_URL, raising=False)
    toml = tmp_path / "bty.toml"
    toml.write_text('[nbdmux]\nurl = "http://from-toml:8082"\n', encoding="utf-8")
    _config.set_active_config(_config.load_config([toml]))

    with _conn(tmp_path) as conn:
        # No DB key, no env -> cfg.nbdmux.url wins.
        assert _settings_store.resolve_nbdmux_url(conn) == "http://from-toml:8082"
        # A DB override still beats bty.toml.
        _settings_store.set_value(conn, _settings_store.KEY_NBDMUX_URL, "http://db:8082")
        assert _settings_store.resolve_nbdmux_url(conn) == "http://db:8082"

    # Restore the empty-config default so later tests aren't polluted.
    _config.set_active_config(_config.load_config([]))


def test_nbdmux_url_falls_through_when_bty_toml_lacks_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-nbdmux bty.toml (loaded on upgrade before the operator
    adds ``[nbdmux]``) has ``cfg().nbdmux`` missing entirely --
    the resolver's ``AttributeError`` guard MUST catch this and
    fall through to env / None rather than 500'ing a Settings-form
    render. The comment on ``resolve_nbdmux_url`` names this
    exact case; pin it so a refactor can't drop the guard."""
    from bty.web import _config

    monkeypatch.delenv(_settings_store.ENV_NBDMUX_URL, raising=False)
    # An empty bty.toml has no [nbdmux] section, so cfg().nbdmux
    # raises AttributeError on access.
    toml = tmp_path / "bty.toml"
    toml.write_text("# empty bty.toml\n", encoding="utf-8")
    _config.set_active_config(_config.load_config([toml]))

    with _conn(tmp_path) as conn:
        # No env, no DB, no cfg.nbdmux.url -> None (not a crash).
        assert _settings_store.resolve_nbdmux_url(conn) is None
        # Env fills in if set.
        monkeypatch.setenv(_settings_store.ENV_NBDMUX_URL, "http://env:8082")
        assert _settings_store.resolve_nbdmux_url(conn) == "http://env:8082"

    _config.set_active_config(_config.load_config([]))


def test_ramboot_overlay_size_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset -> the built-in conservative default."""
    monkeypatch.delenv(_settings_store.ENV_RAMBOOT_OVERLAY_SIZE, raising=False)
    with _conn(tmp_path) as conn:
        assert (
            _settings_store.resolve_ramboot_overlay_size(conn)
            == _settings_store.DEFAULT_RAMBOOT_OVERLAY_SIZE
        )


def test_ramboot_overlay_size_env_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-only path: no DB row, ``$BTY_RAMBOOT_OVERLAY_SIZE`` set ->
    the env value wins over the built-in default. Fills the gap
    between the default-only and DB-override tests (the middle
    precedence tier had no direct coverage)."""
    monkeypatch.setenv(_settings_store.ENV_RAMBOOT_OVERLAY_SIZE, "8G")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_ramboot_overlay_size(conn) == "8G"


def test_ramboot_overlay_size_db_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_settings_store.ENV_RAMBOOT_OVERLAY_SIZE, "4G")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_RAMBOOT_OVERLAY_SIZE, "16G")
        assert _settings_store.resolve_ramboot_overlay_size(conn) == "16G"
