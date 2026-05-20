"""Tests for ``bty.web._db`` schema initialisation.

Pre-1.0: ``init_db`` is a one-liner over ``CREATE TABLE IF NOT
EXISTS`` -- no migration apparatus. These tests pin the table
shape so a future schema edit can't silently drop a column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bty.web import _db


def test_init_db_creates_machines_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
    expected = {
        "mac",
        "bty_image_ref",
        "hostname",
        "discovered_at",
        "last_seen_at",
        "last_seen_ip",
        "boot_policy",
        "last_flashed_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_init_db_creates_catalog_entries_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(catalog_entries)")}
    assert {"bty_image_ref", "src", "disk_image_sha", "name", "format", "added_at"} <= cols


def test_init_db_creates_events_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    assert {
        "id",
        "ts",
        "kind",
        "subject_kind",
        "subject_id",
        "summary",
        "acknowledged",
    } <= cols


def test_init_db_backfills_acknowledged_on_old_events_table(tmp_path: Path) -> None:
    """A state.db whose ``events`` table predates the ``acknowledged``
    column gets it added in place (additive migration), NOT a
    StaleSchemaError wipe -- the column carries a DEFAULT so existing
    rows survive. This is the persist-across-reflash contract: adding
    an events flag must not cost the operator their machine inventory.
    """
    state = tmp_path / "state.db"
    # Pre-``acknowledged`` events table with one row, plus the columns
    # _REQUIRED_COLUMNS checks so we exercise the additive path (not
    # the stale-wipe path).
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                kind          TEXT NOT NULL,
                subject_kind  TEXT,
                subject_id    TEXT,
                actor         TEXT,
                source_ip     TEXT,
                summary       TEXT NOT NULL,
                details       TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO events (ts, kind, summary) VALUES (?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "image.hash_failed", "old failure"),
        )
        conn.commit()
    _db.init_db(state)  # must not raise; must add the column
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "acknowledged" in cols
        # The pre-existing row is backfilled to the default (0).
        row = conn.execute("SELECT acknowledged FROM events").fetchone()
    assert row[0] == 0


def test_init_db_idempotent(tmp_path: Path) -> None:
    """``init_db`` is called on every ``open_db``; double-call must be a no-op."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)  # second call must not raise


def test_init_db_raises_on_machines_known_disks_missing(tmp_path: Path) -> None:
    """A state.db from v0.18 (machines table without
    ``known_disks`` / ``target_disk_serial``) must trigger
    StaleSchemaError so the operator gets the ``rm state.db``
    recovery hint, not a silent insert that fails on the next
    query."""
    import sqlite3

    import pytest

    state = tmp_path / "state.db"
    # Create the v0.18-shaped machines table -- missing the v0.19
    # disk columns.
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE machines (
                mac                TEXT PRIMARY KEY,
                bty_image_ref      TEXT,
                hostname           TEXT,
                discovered_at      TEXT,
                last_seen_at       TEXT,
                last_seen_ip       TEXT,
                boot_policy        TEXT NOT NULL DEFAULT 'local',
                last_flashed_at    TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            )
            """
        )
        conn.commit()
    with pytest.raises(_db.StaleSchemaError, match="known_disks"):
        _db.init_db(state)


def test_init_db_raises_on_stale_schema(tmp_path: Path) -> None:
    """If state.db exists from an older bty-web (missing
    columns added in a later release), :func:`init_db` raises
    :class:`StaleSchemaError` with operator-actionable recovery
    instructions instead of letting a later ``SELECT`` blow up
    with ``no such column``."""
    import sqlite3

    import pytest

    state = tmp_path / "state.db"
    # Create an older-style events table missing ``source_ip`` --
    # simulates a stale state.db from before audit-log IP tracking.
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                kind          TEXT NOT NULL,
                summary       TEXT NOT NULL
            )
            """
        )
        conn.commit()
    with pytest.raises(_db.StaleSchemaError, match="source_ip"):
        _db.init_db(state)
