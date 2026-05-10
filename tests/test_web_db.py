"""Tests for ``bty.web._db`` schema initialisation + migrations.

The migration layer has historically been small (``init_db`` runs
``CREATE TABLE IF NOT EXISTS`` plus a list of additive
``ALTER TABLE ADD COLUMN`` calls). v0.7.35 added a column-rename
list to mirror CIJOE's "workflow"->"task" vocabulary change; this
file pins the rename behaviour so a future change to ``init_db``
can't accidentally drop the migration step.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bty.web import _db


def test_init_db_renames_legacy_workflow_columns(tmp_path: Path) -> None:
    """An existing state.db carrying the pre-v0.7.35 column names
    (``cijoe_workflow_ref``, ``last_workflow_*``) should be migrated
    in place to the post-rename names (``cijoe_task_ref``,
    ``last_task_*``) on the first ``init_db`` after upgrade.

    Data carried by the old columns must survive the rename: SQLite's
    ``RENAME COLUMN`` preserves all rows.
    """
    state = tmp_path / "state.db"

    # Seed a legacy schema by hand (skipping init_db so we don't
    # invoke the migration we're testing). This mimics what an
    # operator's appliance has on disk after running v0.7.34.
    with sqlite3.connect(state) as conn:
        conn.executescript(
            """
            CREATE TABLE machines (
                mac                       TEXT PRIMARY KEY,
                image_sha256              TEXT,
                provisioning_mode         TEXT NOT NULL DEFAULT 'none',
                hostname                  TEXT,
                cijoe_workflow_ref        TEXT,
                last_known_good           TEXT,
                discovered_at             TEXT,
                last_seen_at              TEXT,
                last_seen_ip              TEXT,
                boot_policy               TEXT NOT NULL DEFAULT 'local',
                last_flashed_at           TEXT,
                last_workflow_run_at      TEXT,
                last_workflow_status      TEXT,
                last_workflow_output_path TEXT,
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO machines
                (mac, cijoe_workflow_ref, last_workflow_status,
                 last_workflow_run_at, last_workflow_output_path,
                 created_at, updated_at)
            VALUES ('aa:bb:cc:dd:ee:ff',
                    '/tasks/post-flash.yaml', 'success',
                    '2026-05-10T00:00:00+00:00', '/var/lib/bty/tasks/aa:bb:cc:dd:ee:ff/abc',
                    '2026-05-10T00:00:00+00:00', '2026-05-10T00:00:00+00:00')
            """
        )
        conn.commit()

    # Now run the real init_db. The migration list should rename
    # the four cijoe-task columns in place.
    _db.init_db(state)

    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}

    # Old names gone, new names present.
    assert "cijoe_workflow_ref" not in cols
    assert "last_workflow_run_at" not in cols
    assert "last_workflow_status" not in cols
    assert "last_workflow_output_path" not in cols
    assert "cijoe_task_ref" in cols
    assert "last_task_run_at" in cols
    assert "last_task_status" in cols
    assert "last_task_output_path" in cols

    # Data survived the rename.
    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT cijoe_task_ref, last_task_status, last_task_run_at, "
            "last_task_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["cijoe_task_ref"] == "/tasks/post-flash.yaml"
    assert row["last_task_status"] == "success"
    assert row["last_task_run_at"] == "2026-05-10T00:00:00+00:00"
    assert row["last_task_output_path"].endswith("abc")


def test_init_db_idempotent_after_rename(tmp_path: Path) -> None:
    """Running ``init_db`` twice must be a no-op the second time:
    the rename only fires when the old column still exists, and
    the additive-column step only fires when the column is missing.
    """
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)  # must not raise

    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
    assert "cijoe_task_ref" in cols
    assert "cijoe_workflow_ref" not in cols


def test_init_db_creates_fresh_schema_with_new_names(tmp_path: Path) -> None:
    """A brand-new state.db (no legacy table) must come up with
    only the new ``*_task_*`` columns, never the old names."""
    state = tmp_path / "state.db"
    _db.init_db(state)

    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
    assert "cijoe_task_ref" in cols
    assert "cijoe_workflow_ref" not in cols
