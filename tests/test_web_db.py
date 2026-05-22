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
        "boot_mode",
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
                boot_mode        TEXT NOT NULL DEFAULT 'local',
                last_flashed_at    TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            )
            """
        )
        conn.commit()
    with pytest.raises(_db.StaleSchemaError, match="known_disks"):
        _db.init_db(state)


def test_init_db_migrates_boot_policy_to_boot_mode(tmp_path: Path) -> None:
    """A v0.24.0 state.db (machines.boot_policy, value 'sanboot') is
    migrated in place to boot_mode / 'ipxe-exit' rather than crashing on
    the v0.25.0 schema -- the dedicated-state-disk-survives-a-reflash
    case. Other modes are preserved verbatim."""
    import sqlite3

    state = tmp_path / "state.db"
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE machines (
                mac TEXT PRIMARY KEY, bty_image_ref TEXT, hostname TEXT,
                discovered_at TEXT, last_seen_at TEXT, last_seen_ip TEXT,
                boot_policy TEXT NOT NULL DEFAULT 'sanboot', sanboot_drive TEXT,
                last_flashed_at TEXT, saw_flasher_boot INTEGER NOT NULL DEFAULT 0,
                known_disks TEXT, known_disks_at TEXT, hw_lshw TEXT, hw_lshw_at TEXT,
                target_disk_serial TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO machines (mac, boot_policy, created_at, updated_at) "
            "VALUES ('aa:bb:cc:dd:ee:ff', 'sanboot', 't', 't')"
        )
        conn.execute(
            "INSERT INTO machines (mac, boot_policy, created_at, updated_at) "
            "VALUES ('11:22:33:44:55:66', 'bty-flash-once', 't', 't')"
        )
        conn.commit()

    # Must NOT raise -- the rename fixup runs before the stale check.
    _db.init_db(state)

    with _db.open_db(state) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(machines)").fetchall()}
        assert "boot_mode" in cols and "boot_policy" not in cols
        modes = dict(conn.execute("SELECT mac, boot_mode FROM machines").fetchall())
    assert modes["aa:bb:cc:dd:ee:ff"] == "ipxe-exit"  # sanboot -> ipxe-exit
    assert modes["11:22:33:44:55:66"] == "bty-flash-once"  # preserved


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
