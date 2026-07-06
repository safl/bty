"""Tests for ``bty.web._db`` schema initialisation.

Pre-1.0: ``init_db`` is a one-liner over ``CREATE TABLE IF NOT
EXISTS`` plus auto-rotation on version mismatch. The DB carries a
``bty_version`` marker; when it disagrees with the running code (or
data tables exist without a marker -- a pre-versioning DB), ``init_db``
renames the old ``state.db`` to ``state.db.<from>.<ts>.bak`` and
creates a fresh one in its place. A ``system.schema.reset`` event is
recorded so the dashboard tripwire surfaces the rotation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import bty
from bty.web import _db


def test_init_db_creates_machines_table(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
    expected = {
        "mac",
        "bty_image_ref",
        "discovered_at",
        "last_seen_at",
        "last_seen_ip",
        "boot_mode",
        "last_flashed_at",
        "created_at",
        "updated_at",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"
    # Labels live in their own side-table since v0.58.0; the singular
    # ``hostname`` / ``label`` column is gone.
    assert "label" not in cols, (
        "label column should have been dropped in v0.58.0 -- "
        "labels now live in the machine_labels side table"
    )


def test_init_db_creates_machine_labels_table(tmp_path: Path) -> None:
    """The plural-labels side table replaced the singular column in
    v0.58.0. Composite primary key (mac, label) so the same tag can't
    be applied twice; index on label so ``WHERE label = ?`` lookups
    don't full-scan."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machine_labels)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(machine_labels)")}
    assert cols == {"mac", "label"}
    assert "machine_labels_label_idx" in indexes


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


def test_init_db_stamps_current_version_on_fresh_db(tmp_path: Path) -> None:
    """A freshly-created state.db has the running ``bty.__version__``
    in the ``bty_version`` table -- this is what the rotate-or-keep
    decision checks on subsequent inits."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        row = conn.execute("SELECT version FROM bty_version").fetchone()
    assert row is not None
    assert row[0] == bty.__version__


def test_init_db_idempotent(tmp_path: Path) -> None:
    """``init_db`` is called on every ``open_db``; double-call against
    a DB that already has the current version row must be a no-op (no
    rotation, no duplicate marker row)."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        rows = conn.execute("SELECT version FROM bty_version").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == bty.__version__
    # No spurious .bak files on the idempotent path.
    assert not list(state.parent.glob("state.db.*.bak"))


def test_init_db_rotates_pre_versioning_db(tmp_path: Path) -> None:
    """A state.db with data tables but no ``bty_version`` row is a
    pre-versioning DB from an older bty release. ``init_db`` rotates
    it to ``.bak`` and creates a fresh DB in its place. The old DB
    is preserved on disk for forensics."""
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as conn:
        conn.execute(
            "CREATE TABLE machines (mac TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO machines (mac, created_at, updated_at) VALUES (?, ?, ?)",
            ("aa:bb:cc:dd:ee:ff", "2026-05-25T00:00:00+00:00", "2026-05-25T00:00:00+00:00"),
        )
        conn.commit()

    _db.init_db(state)

    # Fresh DB at the original path, stamped with the running version.
    with sqlite3.connect(state) as conn:
        stored = conn.execute("SELECT version FROM bty_version").fetchone()[0]
        machines_rows = conn.execute("SELECT mac FROM machines").fetchall()
    assert stored == bty.__version__
    assert machines_rows == [], "fresh DB must have no machine rows from the rotated-out DB"

    # The rotated .bak file exists and contains the pre-versioning
    # tables (no bty_version table; the original machine row preserved).
    baks = list(state.parent.glob("state.db.pre-versioning.*.bak"))
    assert len(baks) == 1, f"expected one .bak, found {baks!r}"
    with sqlite3.connect(baks[0]) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        old_macs = [r[0] for r in conn.execute("SELECT mac FROM machines")]
    assert "bty_version" not in tables, ".bak preserves the original (pre-versioning) shape"
    assert "machines" in tables
    assert old_macs == ["aa:bb:cc:dd:ee:ff"]


def test_init_db_rotates_mismatched_version_db(tmp_path: Path) -> None:
    """A state.db whose ``bty_version`` row doesn't match the running
    code is rotated and replaced with a fresh DB. The .bak file is
    named after the stored (old) version so the operator can grep
    history."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.27.4",))
        conn.commit()

    _db.init_db(state)

    with sqlite3.connect(state) as conn:
        stored = conn.execute("SELECT version FROM bty_version").fetchone()[0]
    assert stored == bty.__version__

    baks = list(state.parent.glob("state.db.0.27.4.*.bak"))
    assert len(baks) == 1, f"expected one .bak named after old version, found {baks!r}"


def test_init_db_records_schema_reset_event_on_rotation(tmp_path: Path) -> None:
    """The rotation is recorded as a ``system.schema.reset`` event
    with details {from_version, to_version, archived_at} so the
    operator can see + acknowledge the upgrade from /ui/events."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.27.4",))
        conn.commit()

    _db.init_db(state)

    with sqlite3.connect(state) as conn:
        rows = conn.execute(
            "SELECT kind, actor, summary, details, acknowledged "
            "FROM events WHERE kind = 'system.schema.reset'"
        ).fetchall()
    assert len(rows) == 1, f"expected one schema_reset event, got {rows!r}"
    kind, actor, summary, details_json, acknowledged = rows[0]
    assert kind == "system.schema.reset"
    assert actor == "system"
    assert "0.27.4" in summary
    assert bty.__version__ in summary
    assert "image bytes" in summary  # withcache volume + oras registry, not on this DB
    assert acknowledged == 0, "schema_reset must surface as unacknowledged tripwire"
    details = json.loads(details_json)
    assert details["from_version"] == "0.27.4"
    assert details["to_version"] == bty.__version__
    assert ".bak" in details["archived_at"]


def test_init_db_no_event_on_idempotent_call(tmp_path: Path) -> None:
    """Calling ``init_db`` against an already-matching DB must not
    record a schema_reset event -- the tripwire would fire on every
    request otherwise."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'system.schema.reset'"
        ).fetchone()[0]
    assert count == 0


def test_init_db_rotation_drops_sidecars(tmp_path: Path) -> None:
    """sqlite's -journal / -wal / -shm sidecars refer to the .db
    file by name. After rotation they would orphan (pointing to a
    file the next ``state.db`` doesn't own). ``init_db`` unlinks
    them so the fresh DB starts with clean WAL state."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    # Force a stale stored version so rotation will fire.
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-old",))
        conn.commit()
    # Synthesise WAL sidecars next to state.db. ``init_db`` doesn't
    # care about the contents -- only that they're gone after rotation.
    journal = state.parent / "state.db-journal"
    wal = state.parent / "state.db-wal"
    shm = state.parent / "state.db-shm"
    journal.write_bytes(b"stale-journal")
    wal.write_bytes(b"stale-wal")
    shm.write_bytes(b"stale-shm")

    _db.init_db(state)

    assert not journal.exists(), "stale -journal must be unlinked on rotation"
    assert not wal.exists(), "stale -wal must be unlinked on rotation"
    assert not shm.exists(), "stale -shm must be unlinked on rotation"


def test_init_db_rotation_handles_bak_collision(tmp_path: Path) -> None:
    """Two rotations in the same second (or against a pre-existing
    .bak with the same name) get distinct filenames so neither
    overwrites the other."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-old",))
        conn.commit()
    # First rotation.
    _db.init_db(state)
    # Force a second mismatch and rotate again.
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-old",))
        conn.commit()
    _db.init_db(state)

    baks = sorted(state.parent.glob("state.db.0.0.1-old.*.bak"))
    assert len(baks) == 2, f"expected two distinct .bak files, found {baks!r}"


def test_init_db_does_not_touch_existing_bak_on_idempotent_init(tmp_path: Path) -> None:
    """A pre-existing .bak file from a prior rotation must be left
    alone on an idempotent init -- it's the operator's forensics
    archive, not something the next boot rewrites."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    sentinel = state.parent / "state.db.0.27.4.20260101T000000Z.bak"
    sentinel.write_bytes(b"older-rotation-sentinel")
    _db.init_db(state)  # idempotent; should not touch the .bak
    assert sentinel.read_bytes() == b"older-rotation-sentinel"


# -----------------------------------------------------------------------
# row_value: the ``key in row.keys()`` guard that avoids the
# ``key in row`` footgun (which searches values, not column names)
# -----------------------------------------------------------------------


def _one_row(sql: str, params: tuple = ()) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (a TEXT, b INTEGER, c TEXT)")
    conn.execute("INSERT INTO t VALUES (?, ?, ?)", ("alpha", 42, None))
    return conn.execute(sql).fetchone()


def test_row_value_returns_column_value_when_present() -> None:
    row = _one_row("SELECT a, b, c FROM t")
    assert _db.row_value(row, "a") == "alpha"
    assert _db.row_value(row, "b") == 42
    assert _db.row_value(row, "c") is None


def test_row_value_returns_default_when_column_absent() -> None:
    """The whole reason ``row_value`` exists: a partial SELECT (or a
    row from before a column was added) shouldn't raise -- callers
    can rely on ``default`` for the missing-column fallback."""
    row = _one_row("SELECT a FROM t")
    assert _db.row_value(row, "b", default=99) == 99


def test_row_value_default_none() -> None:
    row = _one_row("SELECT a FROM t")
    # No explicit default -> None.
    assert _db.row_value(row, "b") is None


def test_row_value_does_not_match_on_values() -> None:
    """Regression guard: the naive ``key in row`` (without ``.keys()``)
    would iterate the ROW's VALUES, so a query for column name
    ``"alpha"`` on a row where column ``a=="alpha"`` would falsely
    succeed. row_value must NOT do that -- ``.keys()`` is column
    names, not values."""
    row = _one_row("SELECT a FROM t")
    # ``alpha`` is a VALUE, not a column name; must return default.
    assert _db.row_value(row, "alpha", default="MISS") == "MISS"
