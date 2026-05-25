"""Tests for ``bty.web._db`` schema initialisation.

Pre-1.0: ``init_db`` is a one-liner over ``CREATE TABLE IF NOT
EXISTS`` plus a strict ``bty_version`` match check -- no migration
apparatus, no per-column stale-schema detection. Every release that
bumps ``bty.__version__`` is a release that requires a state.db
wipe (or export+wipe+import). These tests pin both halves of that
contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def test_init_db_stamps_current_version_on_fresh_db(tmp_path: Path) -> None:
    """A freshly-created state.db has the running ``bty.__version__``
    in the ``bty_version`` table -- this is what later starts check
    against to refuse stale DBs."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        row = conn.execute("SELECT version FROM bty_version").fetchone()
    assert row is not None
    assert row[0] == bty.__version__


def test_init_db_idempotent(tmp_path: Path) -> None:
    """``init_db`` is called on every ``open_db``; double-call against
    a DB that already has the current version row must be a no-op."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    _db.init_db(state)  # second call must not raise
    with sqlite3.connect(state) as conn:
        rows = conn.execute("SELECT version FROM bty_version").fetchall()
    # Still exactly one version row (PRIMARY KEY means inserting a
    # second of the same value would raise; the implementation must
    # skip the INSERT when the marker already exists).
    assert len(rows) == 1
    assert rows[0][0] == bty.__version__


def test_init_db_raises_on_pre_versioning_db(tmp_path: Path) -> None:
    """A state.db with data tables but no ``bty_version`` row is a
    pre-versioning DB from an older bty release. Pre-1.0 policy has
    no migration apparatus -- refuse with operator-actionable
    instructions instead of silently mixing schemas."""
    state = tmp_path / "state.db"
    # Create a minimal pre-versioning DB shape (one data table, no
    # bty_version table). Mirrors the real-world scenario where the
    # operator's state.db survived a reflash via bty-state-migrate but
    # was created by a bty release that predates this check.
    with sqlite3.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE machines (
                mac          TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        )
        conn.commit()
    with pytest.raises(_db.VersionMismatchError, match="pre-versioning DB"):
        _db.init_db(state)


def test_init_db_raises_on_version_mismatch(tmp_path: Path) -> None:
    """A state.db whose ``bty_version`` row doesn't match the running
    code must be refused. Pre-1.0 policy: every release wipes state
    (or migrates via export+wipe+import)."""
    state = tmp_path / "state.db"
    # Stand up a valid v0.X DB with a different version stamped.
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-fake-old-release",))
        conn.commit()
    with pytest.raises(_db.VersionMismatchError, match=r"0\.0\.1-fake-old-release"):
        _db.init_db(state)


def test_init_db_refuses_pre_versioning_db_across_restart_retries(tmp_path: Path) -> None:
    """REGRESSION (v0.31.0 -> v0.31.1): init_db must refuse a pre-
    versioning DB on EVERY call, not just the first. The earlier
    implementation ran ``conn.executescript(SCHEMA)`` BEFORE checking,
    and ``sqlite3.executescript`` issues an implicit COMMIT, so the
    very act of refusing left ``CREATE TABLE IF NOT EXISTS
    bty_version`` committed to disk (empty table). systemd's
    ``Restart=on-failure`` retried 5s later; the second call saw the
    marker table existed, treated the empty-row case as "fresh DB,
    stamp it", and silently accepted the franken-state.

    Surfaced in production: an operator upgraded an appliance with
    its state.db on a separate disk (bty-state-migrate setup); the
    v0.31.0 hard check fired once, then systemd restarted bty-web
    and the second start succeeded. Old machine inventory + audit
    log carried into v0.31.0 with a stamped bty_version=0.31.0 row.

    Three consecutive init_db calls against the same pre-versioning
    state.db must all raise. No DB mutation may slip through across
    the failed attempts.
    """
    import sqlite3 as _sqlite

    state = tmp_path / "state.db"
    # Stand up a pre-versioning DB (data table present, no
    # bty_version table at all).
    with _sqlite.connect(state) as conn:
        conn.execute(
            """
            CREATE TABLE machines (
                mac          TEXT PRIMARY KEY,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        )
        conn.commit()

    # First call: refused.
    with pytest.raises(_db.VersionMismatchError):
        _db.init_db(state)

    # CRITICAL: the failed first call must NOT have created the
    # ``bty_version`` table (which is what the v0.31.0 bug did via
    # the implicit-commit-before-executescript on the SCHEMA run).
    with _sqlite.connect(state) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bty_version" not in tables, (
        "init_db's refuse path must not leave a partial bty_version table; "
        "otherwise the next systemd-Restart=on-failure attempt would see "
        f"the table exist and accept the DB. Found tables: {tables!r}"
    )

    # Second + third call (modelling systemd retries): still refused,
    # with the same error class. Same shape every time.
    with pytest.raises(_db.VersionMismatchError):
        _db.init_db(state)
    with pytest.raises(_db.VersionMismatchError):
        _db.init_db(state)


def test_init_db_refuses_mismatched_version_without_mutating(tmp_path: Path) -> None:
    """The version-mismatch refuse path must also leave the DB
    untouched (so the operator's ``bty-web export`` on the OLD
    release reads consistent state). Mirror of the pre-versioning
    test for the "different version stamped" case."""
    import sqlite3 as _sqlite

    state = tmp_path / "state.db"
    _db.init_db(state)  # stamp current version
    with _sqlite.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-fake-old",))
        conn.commit()
        # Snapshot the schema for comparison.
        before = sorted(
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )

    with pytest.raises(_db.VersionMismatchError, match=r"0\.0\.1-fake-old"):
        _db.init_db(state)

    with _sqlite.connect(state) as conn:
        after = sorted(
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        # Marker still says the OLD version (refuse path didn't
        # touch it).
        stored = conn.execute("SELECT version FROM bty_version").fetchone()[0]
    assert before == after, "refuse path must not add/drop tables"
    assert stored == "0.0.1-fake-old", "refuse path must not update the marker"


def test_check_db_fresh_db(tmp_path: Path) -> None:
    """A non-existent state.db reports FRESH so the recovery flow
    knows ``init_db`` will create + stamp on first start."""
    state = tmp_path / "state.db"
    r = _db.check_db(state)
    assert r.state == _db.DbState.FRESH
    assert r.stored_version is None
    assert r.has_data_tables is False
    assert r.running_version == bty.__version__
    assert r.needs_recovery is False


def test_check_db_ok_after_init(tmp_path: Path) -> None:
    """A freshly-init'd DB reports OK with the running version stamped.
    ``has_data_tables`` is True because SCHEMA creates the (empty)
    machines/catalog_entries/events/settings tables on init -- the
    flag flips on any non-marker table, populated or not."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    r = _db.check_db(state)
    assert r.state == _db.DbState.OK
    assert r.stored_version == bty.__version__
    assert r.needs_recovery is False


def test_check_db_pre_versioning(tmp_path: Path) -> None:
    """A DB with data tables but no ``bty_version`` row reports
    PRE_VERSIONING -- the recovery UI uses this to render the
    "old release; wipe + import" wizard."""
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE machines (mac TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO machines VALUES (?)", ("aa:bb:cc:dd:ee:ff",))
        conn.commit()
    r = _db.check_db(state)
    assert r.state == _db.DbState.PRE_VERSIONING
    assert r.stored_version is None
    assert r.has_data_tables is True
    assert r.needs_recovery is True


def test_check_db_mismatch(tmp_path: Path) -> None:
    """A DB with a stamped version that doesn't match the running
    code reports MISMATCH + the stored value so the recovery UI
    can say "this DB was created by bty v0.27.4; you are running
    v0.32.0."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.27.4",))
        conn.commit()
    r = _db.check_db(state)
    assert r.state == _db.DbState.MISMATCH
    assert r.stored_version == "0.27.4"
    assert r.needs_recovery is True


def test_check_db_does_not_mutate(tmp_path: Path) -> None:
    """``check_db`` is a non-mutating probe (opens read-only); the
    recovery UI calls it repeatedly while the operator decides what
    to do, and any DB write would either (a) create the file when
    it shouldn't or (b) leak the marker table across systemd
    retries like the v0.31.0 bug did."""
    state = tmp_path / "state.db"
    # Pre-versioning DB: data table without marker.
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE machines (mac TEXT)")
        conn.commit()
    before = state.stat().st_size

    _db.check_db(state)
    _db.check_db(state)
    _db.check_db(state)

    after = state.stat().st_size
    assert before == after, "check_db must not mutate the file"
    with sqlite3.connect(state) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bty_version" not in tables, (
        "check_db must not create the marker table on a pre-versioning DB"
    )


def test_check_db_missing_file_is_fresh(tmp_path: Path) -> None:
    """A check against a path that doesn't exist returns FRESH without
    creating the file -- the recovery flow uses this to decide which
    UI to mount before bty-web has ever stamped anything."""
    nonexistent = tmp_path / "nope" / "state.db"
    r = _db.check_db(nonexistent)
    assert r.state == _db.DbState.FRESH
    assert not nonexistent.exists()
    assert not nonexistent.parent.exists()


def test_version_mismatch_error_carries_running_version(tmp_path: Path) -> None:
    """The error message names BOTH the stored version (so the operator
    knows which release the DB came from) AND the running version
    (so they know which release they're upgrading TO). Together with
    the ``rm state.db`` recovery line, that's all the operator needs."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    with sqlite3.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.99.99",))
        conn.commit()
    with pytest.raises(_db.VersionMismatchError) as exc:
        _db.init_db(state)
    msg = str(exc.value)
    assert "0.99.99" in msg, "error must name the stored (old) version"
    assert bty.__version__ in msg, "error must name the running (new) version"
    assert "rm" in msg, "error must include the wipe-recovery command"
    assert "export" in msg, "error must mention the export/import preservation path"
