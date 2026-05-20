"""Tests for ``bty.web._settings_store``.

The settle-policy resolver (``resolve_flash_settle_policy`` /
``default_flash_settle_policy``) is the one knob that decides what a
``bty-flash-once`` machine boots into after it has been imaged. It was
covered only indirectly (one ``BTY_FLASH_SETTLE_POLICY=sanboot`` env
case in test_web.py's PXE flow). These pin the resolution layer
directly: the override -> env -> default precedence, and -- the part
with no other coverage -- the safety fallback where a typo'd override
or env value can never wedge a machine into an invalid policy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bty.web import _db, _settings_store


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_default_when_nothing_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No override, no env -> the built-in default (``local``)."""
    monkeypatch.delenv(_settings_store.ENV_FLASH_SETTLE_POLICY, raising=False)
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_flash_settle_policy(conn) == "local"
    assert _settings_store.DEFAULT_FLASH_SETTLE_POLICY == "local"


def test_env_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recognised env value wins over the built-in default."""
    monkeypatch.setenv(_settings_store.ENV_FLASH_SETTLE_POLICY, "sanboot")
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_flash_settle_policy(conn) == "sanboot"
    assert _settings_store.default_flash_settle_policy() == "sanboot"


def test_db_override_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored override takes precedence over the env layer."""
    monkeypatch.setenv(_settings_store.ENV_FLASH_SETTLE_POLICY, "local")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_FLASH_SETTLE_POLICY, "sanboot")
        assert _settings_store.resolve_flash_settle_policy(conn) == "sanboot"


def test_bad_env_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised env value (typo) resolves to the default rather
    than propagating an invalid policy."""
    monkeypatch.setenv(_settings_store.ENV_FLASH_SETTLE_POLICY, "bty-flash-always")
    assert _settings_store.default_flash_settle_policy() == "local"
    with _conn(tmp_path) as conn:
        assert _settings_store.resolve_flash_settle_policy(conn) == "local"


def test_bad_override_falls_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised stored override falls back through to the env
    layer (not silently honoured) so a bad setting can never wedge a
    machine into an invalid boot policy."""
    monkeypatch.setenv(_settings_store.ENV_FLASH_SETTLE_POLICY, "sanboot")
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_FLASH_SETTLE_POLICY, "garbage")
        # bad override -> skip -> env layer -> "sanboot"
        assert _settings_store.resolve_flash_settle_policy(conn) == "sanboot"


def test_clear_reverts_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the override reverts resolution to the env / default."""
    monkeypatch.delenv(_settings_store.ENV_FLASH_SETTLE_POLICY, raising=False)
    with _conn(tmp_path) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_FLASH_SETTLE_POLICY, "sanboot")
        assert _settings_store.resolve_flash_settle_policy(conn) == "sanboot"
        _settings_store.clear(conn, _settings_store.KEY_FLASH_SETTLE_POLICY)
        assert _settings_store.resolve_flash_settle_policy(conn) == "local"


def test_settle_policies_are_a_subset_of_boot_policies() -> None:
    """Every value a machine can settle into must be a real boot policy
    -- otherwise the flip on ``POST /pxe/{mac}/done`` would write a
    policy the PXE handler can't serve."""
    from bty.web._models import BOOT_POLICIES

    assert set(_settings_store.FLASH_SETTLE_POLICIES) <= set(BOOT_POLICIES)
