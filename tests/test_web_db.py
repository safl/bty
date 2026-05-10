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
        "image_sha256",
        "provisioning_mode",
        "hostname",
        "cijoe_task_ref",
        "discovered_at",
        "last_seen_at",
        "last_seen_ip",
        "boot_policy",
        "last_flashed_at",
        "last_task_run_at",
        "last_task_status",
        "last_task_output_path",
        "created_at",
        "updated_at",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_init_db_creates_catalog_entries_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(catalog_entries)")}
    assert {"src", "sha256", "name", "format", "added_at"} <= cols


def test_init_db_creates_events_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    assert {"id", "ts", "kind", "subject_kind", "subject_id", "summary"} <= cols


def test_init_db_idempotent(tmp_path: Path) -> None:
    """``init_db`` is called on every ``open_db``; double-call must be a no-op."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)  # second call must not raise


def test_init_db_raises_on_stale_schema(tmp_path: Path) -> None:
    """If state.db exists from an older bty-web (missing
    columns added in a later release), :func:`init_db` raises
    :class:`StaleSchemaError` with operator-actionable recovery
    instructions instead of letting a later ``SELECT`` blow up
    with ``no such column``. This was the silent failure mode
    a long-running bty-web hit when its on-disk DB was older
    than the running code."""
    import sqlite3

    import pytest

    state = tmp_path / "state.db"
    # Create an older-style machines table missing
    # ``last_task_status`` (added with the cancelable cijoe-task
    # work in v0.7.36).
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE machines (
                mac           TEXT PRIMARY KEY,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.commit()
    with pytest.raises(_db.StaleSchemaError, match="last_task_status"):
        _db.init_db(state)
