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
        mgr.start(state_path, tmp_path / "images", tmp_path / "backups", bty_version="x")
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
        image_root = tmp_path / "images"
        backups_root = tmp_path / "backups"
        (image_root / "demo.img").write_bytes(b"x" * 1024)
        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, image_root, backups_root, bty_version="test")
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
            assert states[0].images == 1
            assert states[0].bytes_written > 0
            assert state.dest_path is not None
            assert (Path(state.dest_path) / "manifest.json").is_file()
            assert (Path(state.dest_path) / "images" / "demo.img").is_file()
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
        mgr.start(state_path, tmp_path / "images", tmp_path / "backups", bty_version="x")
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
        mgr.start(state_path, tmp_path / "images", tmp_path / "backups", bty_version="x")
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
            (backups_root / slug / "manifest.json").write_text("{}")
        # An operator-dropped sibling that doesn't match the id pattern:
        # MUST survive the prune.
        (backups_root / "operator-notes.txt").write_text("hello")
        # Pin retention to 2.
        with sqlite3.connect(state_path) as conn:
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, "2")
            conn.commit()

        mgr = BackupManager(max_parallel=1)
        mgr.start(state_path, tmp_path / "images", backups_root, bty_version="x")
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
        mgr.start(state_path, tmp_path / "images", tmp_path / "backups", bty_version="x")
        try:
            with pytest.raises(ValueError, match=r"unknown trigger"):
                await mgr.enqueue(trigger="wat")
        finally:
            await mgr.stop()

    _run(_drive())
