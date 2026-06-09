"""Tests for ``bty.web._backup`` background backup queue.

Mirrors ``tests/test_web_hash_manager.py`` -- hermetic (real files
in tmp_path), async test bodies via ``asyncio.run`` rather than
pytest-asyncio. Wraps :func:`bty.web._portability.export_bundle`,
so we exercise the real export path with tiny inputs.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bty.web import _db, _events_log, _settings_store
from bty.web._backup import BackupManager


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _init_state(tmp_path: Path) -> Path:
    """Initialise a state.db + a minimal image_root + a backups_root."""
    state_path = tmp_path / "state.db"
    _db.init_db(state_path)
    (tmp_path / "images").mkdir()
    (tmp_path / "backups").mkdir()
    return state_path


def test_enqueue_creates_unique_ids(tmp_path: Path) -> None:
    """Two enqueues in the same second mint distinct ids -- the manager
    auto-suffixes ``-1`` / ``-2`` so the same-second case does not
    collide on disk."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            a = await mgr.enqueue()
            b = await mgr.enqueue()
            c = await mgr.enqueue()
            assert len({a.backup_id, b.backup_id, c.backup_id}) == 3
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_runs_to_completion(tmp_path: Path) -> None:
    """Happy path: queued -> running -> completed; bundle directory
    lands on disk under ``backups_root``; the audit log records a
    ``backup.created`` event."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        backups_root = tmp_path / "backups"
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, backups_root)
        try:
            state = await mgr.enqueue()
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.01)
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].status == "completed", states[0].error
            assert states[0].bytes_done > 0
            assert state.dest_path is not None
            assert (Path(state.dest_path) / "inventory.json").is_file()
            # v0.33.2: metadata-only bundles. No files/ subdir.
            assert not (Path(state.dest_path) / "files").exists()
        finally:
            await mgr.stop()
        # Audit-log entry recorded?
        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            events = _events_log.list_events(conn, subject_kind="backup", limit=20)
        assert any(e.kind == "backup.created" for e in events), [e.kind for e in events]

    _run(_drive())


def test_manual_trigger_does_not_update_last_run_at(tmp_path: Path) -> None:
    """``trigger=manual`` (the Backup tab button) does NOT shift the
    scheduler's cadence anchor. Only ``trigger=scheduled`` writes
    ``backup.last_run_at``."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            await mgr.enqueue(trigger="manual")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()
        with sqlite3.connect(state_path) as conn:
            assert _settings_store.get_backup_last_run_at(conn) is None

    _run(_drive())


def test_scheduled_trigger_updates_last_run_at(tmp_path: Path) -> None:
    """``trigger=scheduled`` writes ``backup.last_run_at`` on success.
    Read back via :func:`_settings_store.get_backup_last_run_at`."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            await mgr.enqueue(trigger="scheduled")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()
        with sqlite3.connect(state_path) as conn:
            ts = _settings_store.get_backup_last_run_at(conn)
        assert ts is not None and "T" in ts


def test_retention_prunes_oldest(tmp_path: Path) -> None:
    """After a successful backup the manager prunes the oldest siblings
    under ``backups_root`` to satisfy the configured retention. Stage
    three pre-existing fake-backup dirs + retention=2, expect the
    oldest deleted after one new backup."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        backups_root = tmp_path / "backups"
        # Three pre-existing fake backups. Names sort chronologically.
        for slug in ("2026-05-22T00-00-00Z", "2026-05-23T00-00-00Z", "2026-05-24T00-00-00Z"):
            (backups_root / slug).mkdir()
            (backups_root / slug / "inventory.json").write_text("{}")
        # An operator-dropped sibling that doesn't match the id pattern:
        # MUST survive the prune.
        (backups_root / "operator-notes.txt").write_text("hello")
        # Pin retention to 2.
        with sqlite3.connect(state_path) as conn:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "2")
            conn.commit()

        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, backups_root)
        try:
            await mgr.enqueue()
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()

        # After the run there are 4 backups total; retention=2 keeps the
        # 2 newest (the just-created one + 2026-05-24).
        names = sorted(p.name for p in backups_root.iterdir() if p.is_dir())
        assert len(names) == 2, names
        # Operator-dropped file survives.
        assert (backups_root / "operator-notes.txt").is_file()
        # Oldest two are gone.
        assert "2026-05-22T00-00-00Z" not in names
        assert "2026-05-23T00-00-00Z" not in names

    _run(_drive())


def test_backup_state_to_dict_omits_unpicklable_event() -> None:
    """``BackupState.to_dict`` excludes ``_cancel`` (a
    ``threading.Event`` wraps a non-picklable lock)."""
    from bty.web._backup import BackupState

    s = BackupState(backup_id="2026-05-24T00-00-00Z")
    d = s.to_dict()
    assert "_cancel" not in d
    assert d["backup_id"] == "2026-05-24T00-00-00Z"
    assert d["status"] == "queued"


def test_unknown_trigger_rejected(tmp_path: Path) -> None:
    """Defensive check: only ``manual`` and ``scheduled`` are valid
    triggers; anything else surfaces a ValueError so a buggy caller
    can't write garbage into the events log."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            with pytest.raises(ValueError, match=r"unknown trigger"):
                await mgr.enqueue(trigger="wat")
        finally:
            await mgr.stop()

    _run(_drive())


# ----- scheduler logic --------------------------------------------------


def test_is_due_manual_never_fires() -> None:
    """``cadence=manual`` never fires regardless of state."""
    from datetime import UTC, datetime

    from bty.web._backup import _is_due

    now = datetime(2026, 5, 24, tzinfo=UTC)
    assert _is_due(None, "manual", now) is False
    assert _is_due("2020-01-01T00:00:00+00:00", "manual", now) is False


def test_is_due_no_last_run_fires_immediately() -> None:
    """First-time opt-in fires on the next tick so the operator can
    confirm the schedule works without waiting a full cadence."""
    from datetime import UTC, datetime

    from bty.web._backup import _is_due

    now = datetime(2026, 5, 24, tzinfo=UTC)
    assert _is_due(None, "daily", now) is True
    assert _is_due(None, "weekly", now) is True


def test_is_due_daily_interval() -> None:
    """Daily cadence fires when 24h have elapsed since the last run."""
    from datetime import UTC, datetime, timedelta

    from bty.web._backup import _is_due

    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    just_now = (now - timedelta(hours=1)).isoformat()
    yesterday = (now - timedelta(hours=23, minutes=59)).isoformat()
    a_day_ago = (now - timedelta(hours=24, minutes=1)).isoformat()
    assert _is_due(just_now, "daily", now) is False
    assert _is_due(yesterday, "daily", now) is False
    assert _is_due(a_day_ago, "daily", now) is True


def test_is_due_weekly_interval() -> None:
    """Weekly cadence fires when 7d have elapsed since the last run."""
    from datetime import UTC, datetime, timedelta

    from bty.web._backup import _is_due

    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    three_days_ago = (now - timedelta(days=3)).isoformat()
    a_week_ago = (now - timedelta(days=7, minutes=1)).isoformat()
    assert _is_due(three_days_ago, "weekly", now) is False
    assert _is_due(a_week_ago, "weekly", now) is True


def test_is_due_unparseable_timestamp_fires() -> None:
    """A garbage ``last_run_at`` (hand-edited state.db) is treated as
    "no prior run" rather than silently never firing."""
    from datetime import UTC, datetime

    from bty.web._backup import _is_due

    now = datetime(2026, 5, 24, tzinfo=UTC)
    assert _is_due("not-a-timestamp", "daily", now) is True


def test_scheduler_tick_disabled_does_nothing(tmp_path: Path) -> None:
    """``backup.enabled=False`` -> the scheduler enqueues nothing."""

    async def _drive() -> None:
        from bty.web._backup import _scheduler_tick

        state_path = _init_state(tmp_path)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            await _scheduler_tick(state_path, mgr)
            assert await mgr.list() == []
        finally:
            await mgr.stop()

    _run(_drive())


def test_scheduler_tick_enqueues_when_due(tmp_path: Path) -> None:
    """``enabled=True`` + ``cadence=daily`` + no prior run -> a
    scheduled backup gets enqueued on the next tick."""

    async def _drive() -> None:
        from bty.web._backup import _scheduler_tick

        state_path = _init_state(tmp_path)
        with sqlite3.connect(state_path) as conn:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, "1")
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, "daily")
            conn.commit()
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            await _scheduler_tick(state_path, mgr)
            rows = await mgr.list()
            assert len(rows) == 1
            assert rows[0].trigger == "scheduled"
            # The scheduler-initiated request lands an audit event with
            # actor=system -- the symmetric counterpart to the operator
            # HTTP handler's backup.create.requested (actor=operator).
            # Without this, a regression that drops the scheduler-side
            # event would be invisible (the enqueue still happens).
            with _db.open_db(state_path) as conn:
                reqs = _events_log.list_events(conn, kind="backup.create.requested")
            assert len(reqs) == 1, reqs
            assert reqs[0].actor == "system"
        finally:
            await mgr.stop()

    _run(_drive())


def test_scheduler_tick_skips_when_already_running(tmp_path: Path) -> None:
    """When a scheduled backup is already queued or running, the tick
    must NOT enqueue another one -- otherwise a slow backup gets piled
    on top of itself every tick."""

    async def _drive() -> None:
        from bty.web._backup import _scheduler_tick

        state_path = _init_state(tmp_path)
        with sqlite3.connect(state_path) as conn:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_ENABLED, "1")
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, "daily")
            conn.commit()
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "backups")
        try:
            # Pre-seed a scheduled backup; tick shouldn't add a second.
            await mgr.enqueue(trigger="scheduled")
            await _scheduler_tick(state_path, mgr)
            rows = await mgr.list()
            # 1 from our pre-seed; tick added nothing.
            assert len(rows) == 1, [r.backup_id for r in rows]
        finally:
            await mgr.stop()

    _run(_drive())


def test_list_backups_on_disk_enumerates_and_reads_inventory(tmp_path: Path) -> None:
    """A directory under ``backups_root`` whose name is an ISO-8601 slug
    shows up with inventory-derived counts + a bytes-on-disk total."""
    import json

    from bty.web._backup import list_backups_on_disk

    backups_root = tmp_path / "backups"
    backups_root.mkdir()

    # Bundle #1: older, two machines. v0.33.2 metadata-only format.
    older = backups_root / "2026-05-23T10-00-00Z"
    older.mkdir()
    (older / "inventory.json").write_text(
        json.dumps(
            {
                "bty_export_version": 3,
                "exported_at": "2026-05-23T10:00:00+00:00",
                "exported_by_bty_version": "0.33.2",
                "machines": [{"mac": "aa:bb:cc:dd:ee:01"}, {"mac": "aa:bb:cc:dd:ee:02"}],
            }
        )
    )
    # Bundle #2: newer, empty inventory.
    newer = backups_root / "2026-05-24T09-00-00Z"
    newer.mkdir()
    (newer / "inventory.json").write_text(
        json.dumps(
            {
                "bty_export_version": 3,
                "exported_at": "2026-05-24T09:00:00+00:00",
                "exported_by_bty_version": "0.33.2",
                "machines": [],
            }
        )
    )

    # Non-bundle siblings: a notes file + a directory not matching the
    # ID format must NOT show up in the listing.
    (backups_root / "README.txt").write_text("operator notes")
    (backups_root / "not-a-backup").mkdir()

    out = list_backups_on_disk(backups_root)
    assert [b.backup_id for b in out] == [
        "2026-05-24T09-00-00Z",  # newest first
        "2026-05-23T10-00-00Z",
    ]
    older_row = out[1]
    assert older_row.machines == 2
    assert older_row.bty_version == "0.33.2"
    assert older_row.bytes_on_disk > 0  # at least the inventory


def test_list_backups_on_disk_handles_missing_inventory(tmp_path: Path) -> None:
    """A bundle dir without an inventory.json still lists -- the
    operator can see the orphan and clean it up rather than the UI
    hiding it."""
    from bty.web._backup import list_backups_on_disk

    backups_root = tmp_path / "backups"
    orphan = backups_root / "2026-05-22T08-00-00Z"
    orphan.mkdir(parents=True)
    (orphan / "stray.bin").write_bytes(b"x")

    out = list_backups_on_disk(backups_root)
    assert len(out) == 1
    assert out[0].backup_id == "2026-05-22T08-00-00Z"
    assert out[0].machines == 0
    assert out[0].bty_version is None
    assert out[0].exported_at is None


def test_list_backups_on_disk_missing_root_returns_empty(tmp_path: Path) -> None:
    """No ``backups_root`` -> empty list (no crash). Covers a fresh
    install where the operator hasn't run any backups yet."""
    from bty.web._backup import list_backups_on_disk

    assert list_backups_on_disk(tmp_path / "does-not-exist") == []


def test_is_valid_backup_id_accepts_iso_slug_only() -> None:
    """The id validator is the path-traversal guard for the download
    route: only ISO-8601 slugs (with optional ``-N`` suffix) pass."""
    from bty.web._backup import is_valid_backup_id

    assert is_valid_backup_id("2026-05-23T10-00-00Z") is True
    assert is_valid_backup_id("2026-05-23T10-00-00Z-1") is True
    # Traversal attempts + garbage all reject.
    assert is_valid_backup_id("..") is False
    assert is_valid_backup_id("../etc") is False
    assert is_valid_backup_id("") is False
    assert is_valid_backup_id("not-a-date") is False
    assert is_valid_backup_id("2026/05/23") is False


def test_delete_bundle_rmtree_logs_event(tmp_path: Path) -> None:
    """``delete_bundle`` removes the directory + emits a
    ``backup.deleted`` event with the snapshotted counts."""
    import json

    from bty.web._backup import delete_bundle

    state_path = _init_state(tmp_path)
    backups_root = tmp_path / "backups"
    bundle = backups_root / "2026-05-23T10-00-00Z"
    bundle.mkdir(parents=True)
    (bundle / "inventory.json").write_text(
        json.dumps(
            {
                "bty_export_version": 3,
                "machines": [{"mac": "aa:bb:cc:dd:ee:01"}],
            }
        )
    )

    snapshot = delete_bundle(state_path, backups_root, "2026-05-23T10-00-00Z")
    assert not bundle.exists()
    assert snapshot.machines == 1

    # Audit log carries the new event with the snapshot counts.
    with sqlite3.connect(state_path) as conn:
        conn.row_factory = sqlite3.Row
        events = _events_log.list_events(conn, subject_kind="backup", limit=10)
    kinds = [e.kind for e in events]
    assert "backup.deleted" in kinds
    deleted = next(e for e in events if e.kind == "backup.deleted")
    assert deleted.subject_id == "2026-05-23T10-00-00Z"
    assert deleted.details is not None
    assert deleted.details["machines"] == 1


def test_delete_bundle_missing_raises_filenotfound(tmp_path: Path) -> None:
    """A delete against a non-existent backup_id is a
    FileNotFoundError -- callers translate to 404."""
    from bty.web._backup import delete_bundle

    state_path = _init_state(tmp_path)
    with pytest.raises(FileNotFoundError):
        delete_bundle(state_path, tmp_path / "backups", "2026-05-23T10-00-00Z")


def test_state_listener_fires_on_terminal_transition(tmp_path: Path) -> None:
    """Backup completes -> the registered state listener gets called
    with the terminal state. Wires the SSE push-driven refresh."""

    async def _drive() -> list[str]:
        state_path = _init_state(tmp_path)
        seen: list[str] = []
        mgr = BackupManager(max_parallel=1)
        mgr.set_state_listener(lambda s: seen.append(s.status))
        mgr.start(state_path, tmp_path / "backups")
        try:
            await mgr.enqueue(trigger="manual")
            # Wait for the worker to flip terminal. The listener fires
            # for queued -> running and running -> completed, so we
            # poll for the terminal transition.
            for _ in range(50):
                states = await mgr.list()
                if states and states[0].status == "completed":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("backup did not complete in time")
            return seen
        finally:
            await mgr.stop()

    seen = _run(_drive())
    # At minimum: a running event and a completed event. Order matters.
    assert "running" in seen
    assert "completed" in seen
    assert seen.index("running") < seen.index("completed")


def test_state_listener_fires_on_cancel(tmp_path: Path) -> None:
    """A queued backup that gets cancelled before running still
    fires a listener event with status="cancelled"."""

    async def _drive() -> list[str]:
        state_path = _init_state(tmp_path)
        seen: list[str] = []
        # max_parallel=0 would leave the queue starved; using 1 but
        # cancelling immediately after enqueue gets us the queued ->
        # cancelled path through stop() in some timings; the more
        # reliable path is to enqueue then explicitly cancel.
        mgr = BackupManager(max_parallel=1)
        mgr.set_state_listener(lambda s: seen.append((s.backup_id, s.status)))
        mgr.start(state_path, tmp_path / "backups")
        try:
            st = await mgr.enqueue(trigger="manual")
            # Cancel may race the worker picking it up; in either case
            # we expect a cancelled event in `seen` either via the
            # explicit cancel path or via stop() draining the queue.
            await mgr.cancel(st.backup_id)
            await asyncio.sleep(0.05)
            return [s for _bid, s in seen]
        finally:
            await mgr.stop()

    seen = _run(_drive())
    # Either via cancel() OR via the running -> completed path if the
    # worker beat the cancel. We assert the listener saw SOME terminal
    # state, not a specific status -- the timing isn't deterministic.
    assert any(s in ("cancelled", "completed", "failed") for s in seen)


def test_resolve_max_parallel_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BTY_BACKUP_MAX_PARALLEL`` overrides the default; out-of-range
    or non-numeric values fall back to the default rather than
    raising at startup. Same shape as the sibling
    ``test_resolve_max_parallel_env_var`` in test_web_hash_manager.py
    -- the three managers all read their own env var the same way,
    and operators who set one might typo a similar one."""
    from bty.web import _config
    from bty.web._backup import DEFAULT_MAX_PARALLEL, _resolve_max_parallel

    def _set_and_reload(value: str) -> None:
        if value == "":
            monkeypatch.delenv("BTY_BACKUP_MAX_PARALLEL", raising=False)
        else:
            monkeypatch.setenv("BTY_BACKUP_MAX_PARALLEL", value)
        _config.set_active_config(_config.load_config(None))

    _set_and_reload("3")
    assert _resolve_max_parallel() == 3

    _set_and_reload("0")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL

    # Negative ints coerce cleanly (int("-2") == -2) but the
    # resolver clamps to the default since concurrency < 1 is
    # nonsense.
    _set_and_reload("-2")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL

    # Non-numeric value fails the env -> int coerce at load time.
    # v0.42 surfaces that as a startup ValueError (typo-loud)
    # rather than the silent-fallback the pre-v0.42 resolver did.
    import pytest as _pytest

    monkeypatch.setenv("BTY_BACKUP_MAX_PARALLEL", "not-a-number")
    with _pytest.raises(ValueError):
        _config.set_active_config(_config.load_config(None))

    _set_and_reload("")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL


def test_suppress_oserror_swallows_oserror_only() -> None:
    """``_suppress_oserror`` is used around best-effort filesystem
    cleanup (rmtree of a partial bundle dir). It must:

    - swallow OSError + subclasses (FileNotFoundError, PermissionError),
      so cleanup failure doesn't mask the original error
    - propagate non-OSError exceptions unchanged (so an unrelated
      bug in cleanup code still surfaces, not silently disappears)
    - log the swallowed error at warning level
    """
    from bty.web._backup import _suppress_oserror

    # OSError + subclasses swallowed.
    with _suppress_oserror():
        raise FileNotFoundError("simulated")
    with _suppress_oserror():
        raise PermissionError("simulated")
    with _suppress_oserror():
        raise OSError("simulated")

    # Non-OSError exceptions propagate.
    with pytest.raises(ValueError, match="not swallowed"), _suppress_oserror():
        raise ValueError("not swallowed")
    with pytest.raises(KeyError, match="not swallowed"), _suppress_oserror():
        raise KeyError("not swallowed")

    # Normal exit (no exception) passes through.
    with _suppress_oserror():
        pass
