"""Tests for the display-timezone cache helper in ``bty.web._helpers``.

Pins the swallow-and-log behaviour of :func:`cached_display_tz`
against two failure modes: a stored value the resolver rejects
(:class:`SettingValueError` -- surfaced on the Settings page), and
a transient sqlite3 error (locked DB, missing file mid-rotation,
permissions) which previously fell through the same silent
``except Exception`` catch and left the operator staring at "why
is my clock in UTC?" without a signal.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bty.web import _db, _helpers, _settings_store


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Drop the module-level cache so each test starts from a cold
    resolve. Otherwise the first test's UTC fallback would satisfy
    every later test's ``cached_display_tz`` call."""
    _helpers._DISPLAY_TZ_CACHE.clear()


def test_returns_configured_zone_on_happy_path(tmp_path: Path) -> None:
    """The stored value round-trips as a ZoneInfo instance and the
    result gets cached (a subsequent call is served without touching
    the DB)."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        _settings_store.set_value(conn, _settings_store.KEY_DISPLAY_TZ, "Europe/Copenhagen")
        conn.commit()

    tz = _helpers.cached_display_tz(state)
    assert str(tz) == "Europe/Copenhagen"
    # Cached: the state DB path key lands with the resolved zone.
    assert _helpers._DISPLAY_TZ_CACHE[str(state)] is tz


def test_setting_value_error_falls_back_to_utc_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resolver ``SettingValueError`` (bad stored value: hand-edited
    state.db) falls back to UTC and logs at WARNING so the operator
    sees the failure in the log stream even if they never open the
    Settings page."""
    state = tmp_path / "state.db"
    _db.init_db(state)

    def _raise_bad_value(_conn: sqlite3.Connection) -> ZoneInfo:
        raise _settings_store.SettingValueError("unknown zone 'Mars/Phobos'")

    monkeypatch.setattr(_settings_store, "resolve_display_timezone", _raise_bad_value)
    with caplog.at_level(logging.WARNING, logger="bty.web._helpers"):
        tz = _helpers.cached_display_tz(state)
    assert str(tz) == "UTC"
    assert any("display_tz resolve failed" in r.message for r in caplog.records)
    assert any("Mars/Phobos" in r.message for r in caplog.records)


def test_sqlite_error_falls_back_to_utc_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The regression this narrowed except was written for: a
    ``sqlite3.OperationalError`` (locked DB, missing file mid-
    rotation, permissions) MUST fall through to UTC AND land in the
    log stream. Prior to the narrowing this ``except Exception``
    caught the error silently, and an operator staring at "clock
    shows UTC" had no signal to root-cause."""
    state = tmp_path / "state.db"
    _db.init_db(state)

    def _raise_sqlite(_conn: sqlite3.Connection) -> ZoneInfo:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(_settings_store, "resolve_display_timezone", _raise_sqlite)
    with caplog.at_level(logging.WARNING, logger="bty.web._helpers"):
        tz = _helpers.cached_display_tz(state)
    assert str(tz) == "UTC"
    assert any("display_tz resolve failed" in r.message for r in caplog.records)
    assert any("database is locked" in r.message for r in caplog.records)


def test_unexpected_exception_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing means genuinely unexpected exceptions (a bug in
    the resolver, an :class:`AssertionError` from a test-injected
    stub) now propagate rather than getting silently swallowed as
    UTC. This is the point of the narrowing: bugs should be loud."""
    state = tmp_path / "state.db"
    _db.init_db(state)

    def _raise_unexpected(_conn: sqlite3.Connection) -> ZoneInfo:
        raise RuntimeError("unexpected bug in the resolver")

    monkeypatch.setattr(_settings_store, "resolve_display_timezone", _raise_unexpected)
    with pytest.raises(RuntimeError, match="unexpected bug"):
        _helpers.cached_display_tz(state)
