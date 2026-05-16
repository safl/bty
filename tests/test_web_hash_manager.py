"""Tests for ``bty.web._hash`` background hash queue.

Mirrors ``tests/test_web_catalog_manager.py`` -- same lifecycle
shape, hermetic (real files in tmp_path), async test bodies via
``asyncio.run`` rather than pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from bty.web._hash import HashManager


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_enqueue_unknown_filename_raises(tmp_path: Path) -> None:
    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(tmp_path)
        try:
            with pytest.raises(FileNotFoundError):
                await mgr.enqueue("nope.img")
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_rejects_traversal_names(tmp_path: Path) -> None:
    """``HashManager.enqueue`` must reject names with path
    separators or traversal segments before touching the
    filesystem. The HTTP layer's ``_safe_path`` already covers
    public routes; this defends non-API callers (auto-import,
    tests, future internal use)."""

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(tmp_path)
        try:
            for bad in ("../etc/passwd", "foo/bar", "..", ".", "", "a\\b", "a\0b"):
                with pytest.raises(ValueError, match=r"invalid name"):
                    await mgr.enqueue(bad)
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_already_hashed_shortcut(tmp_path: Path) -> None:
    """An image whose .sha256 sidecar already exists lands as
    ``completed`` immediately, no worker round-trip."""

    async def _drive() -> None:
        payload = b"already-hashed"
        sha = hashlib.sha256(payload).hexdigest()
        (tmp_path / "demo.img").write_bytes(payload)
        (tmp_path / "demo.img.sha256").write_text(f"{sha}  demo.img\n")

        mgr = HashManager(max_parallel=1)
        mgr.start(tmp_path)
        try:
            state = await mgr.enqueue("demo.img")
            assert state.status == "completed"
            assert state.sha256 == sha
            assert state.bytes_hashed == len(payload)
            assert state.bytes_total == len(payload)
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_runs_to_completion(tmp_path: Path) -> None:
    """Happy path: queued -> running -> completed; sidecar gets
    written; sha256 field is populated."""

    async def _drive() -> None:
        payload = b"a" * (1 << 12)  # 4 KiB so the test is fast
        (tmp_path / "demo.img").write_bytes(payload)

        mgr = HashManager(max_parallel=1)
        mgr.start(tmp_path)
        try:
            await mgr.enqueue("demo.img")
            for _ in range(50):
                states = await mgr.list()
                if states and states[0].status == "completed":
                    break
                await asyncio.sleep(0.01)
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].status == "completed"
            assert states[0].sha256 == hashlib.sha256(payload).hexdigest()
            # Sidecar exists.
            assert (tmp_path / "demo.img.sha256").is_file()
        finally:
            await mgr.stop()

    _run(_drive())


def test_default_parallelism_is_one(tmp_path: Path) -> None:
    """Default is 1 -- documented Pi/NUC-friendly behaviour."""
    mgr = HashManager()
    assert mgr.max_parallel == 1


def test_cancel_unknown_returns_none(tmp_path: Path) -> None:
    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(tmp_path)
        try:
            assert await mgr.cancel("nothing.img") is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_run_hash_cancel_with_concurrent_oserror_marks_cancelled(tmp_path: Path) -> None:
    """If the cancel flag fires while the worker thread happens
    to be mid-syscall, ``ensure_sha256`` may raise ``OSError``
    before its own chunk-boundary cancel-check translates it
    into ``HashCancelled``. The manager must treat that as
    operator cancellation, not a failed hash. Without the
    override, the UI shows "failed: <syscall>" for what was
    actually the operator's stop request."""
    import unittest.mock

    from bty import images as _images
    from bty.web._hash import HashManager, HashState

    target = tmp_path / "demo.img"
    target.write_bytes(b"x" * 64)

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(tmp_path)
        try:
            state = HashState(name="demo.img", path=str(target), bytes_total=64)
            state._cancel.set()  # simulate operator cancel before the IO error

            def boom(*_a: object, **_kw: object) -> str:
                raise OSError("transient IO error")

            with unittest.mock.patch.object(_images, "ensure_sha256", boom):
                await mgr._run_one(state)

            assert state.status == "cancelled"
            assert state.error is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_hash_manager_backfills_from_events(tmp_path: Path) -> None:
    """``HashManager.start(state_path=...)`` repopulates ``_states``
    from recent image.hashed / image.hash_failed events so the
    /ui/images Hashes table survives a bty-web restart. Mirrors
    the DownloadManager + ReleaseFetchManager backfill."""
    from bty.web import _db, _events_log
    from bty.web._hash import HashManager

    state_path = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    _db.init_db(state_path)
    with _db.open_db(state_path) as conn:
        _events_log.record(
            conn,
            kind="image.hashed",
            summary="hashed demo.img.zst",
            subject_kind="image",
            subject_id="demo.img.zst",
            actor="system",
            details={"name": "demo.img.zst", "sha256": "c" * 64, "bytes": 1234},
        )
        _events_log.record(
            conn,
            kind="image.hash_failed",
            summary="broken.img.gz failed",
            subject_kind="image",
            subject_id="broken.img.gz",
            actor="system",
            details={"name": "broken.img.gz", "error": "OSError: disk on fire"},
        )
        conn.commit()

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(image_root, state_path=state_path)
        try:
            states = await mgr.list()
            by_name = {s.name: s for s in states}
            assert by_name["demo.img.zst"].status == "completed"
            assert by_name["demo.img.zst"].sha256 == "c" * 64
            assert by_name["demo.img.zst"].bytes_hashed == 1234
            assert by_name["broken.img.gz"].status == "failed"
            assert "disk on fire" in (by_name["broken.img.gz"].error or "")
        finally:
            await mgr.stop()

    _run(_drive())


def test_hash_manager_backfill_newest_per_name_wins(tmp_path: Path) -> None:
    """Two image.hashed events for the same name -- the newer one
    wins after backfill. Guards the newest-first / seen-set
    invariant shared with the other manager backfills."""
    from bty.web import _db, _events_log
    from bty.web._hash import HashManager

    state_path = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    _db.init_db(state_path)
    with _db.open_db(state_path) as conn:
        # Older event (e.g. failed import attempt).
        _events_log.record(
            conn,
            kind="image.hash_failed",
            summary="early failure",
            subject_kind="image",
            subject_id="demo.img.gz",
            actor="system",
            details={"name": "demo.img.gz", "error": "old transient"},
        )
        # Newer event (operator re-triggered after fix).
        _events_log.record(
            conn,
            kind="image.hashed",
            summary="hashed successfully",
            subject_kind="image",
            subject_id="demo.img.gz",
            actor="system",
            details={"name": "demo.img.gz", "sha256": "d" * 64, "bytes": 8},
        )
        conn.commit()

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(image_root, state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].status == "completed"
            assert states[0].sha256 == "d" * 64
        finally:
            await mgr.stop()

    _run(_drive())


def test_hash_manager_backfill_tolerates_corrupted_ts(tmp_path: Path) -> None:
    """A row with a malformed ``ts`` (manually corrupted state.db,
    or a future ts format we don't recognise) shouldn't crash the
    backfill. The event still seeds the manager state; only
    ``started_at`` / ``finished_at`` end up None."""
    from bty.web import _db
    from bty.web._hash import HashManager

    state_path = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    _db.init_db(state_path)
    # Hand-write a row with a clearly-broken ts so we can be sure
    # the backfill doesn't bail on the whole loop.
    with _db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO events (ts, kind, subject_kind, subject_id, actor, summary, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "not an iso timestamp",
                "image.hashed",
                "image",
                "broken_ts.img",
                "system",
                "ok despite bad ts",
                '{"name": "broken_ts.img", "sha256": "ff", "bytes": 1}',
            ),
        )
        conn.commit()

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(image_root, state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].name == "broken_ts.img"
            assert states[0].status == "completed"
            assert states[0].started_at is None
            assert states[0].finished_at is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_hash_failed_event_is_recorded(tmp_path: Path) -> None:
    """A genuinely-failed hash (IO error, not operator cancel)
    must land an ``image.hash_failed`` event in the audit log
    so /ui/events shows the operator "this file tried to import
    and crashed" without polling /catalog/hashes. The matching
    success path emits ``image.hashed``; symmetric coverage."""
    import unittest.mock

    from bty import images as _images
    from bty.web import _db
    from bty.web._events_log import list_events
    from bty.web._hash import HashManager, HashState

    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    target = tmp_path / "demo.img"
    target.write_bytes(b"x" * 64)

    async def _drive() -> None:
        mgr = HashManager()
        mgr.start(tmp_path, state_path=state_db)
        try:
            state = HashState(name="demo.img", path=str(target), bytes_total=64)

            def boom(*_a: object, **_kw: object) -> str:
                raise OSError("disk on fire")

            with unittest.mock.patch.object(_images, "ensure_sha256", boom):
                await mgr._run_one(state)
            assert state.status == "failed"
        finally:
            await mgr.stop()

    _run(_drive())

    with _db.open_db(state_db) as conn:
        rows = list_events(conn, kind="image.hash_failed")
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_kind == "image"
    assert row.subject_id == "demo.img"
    assert row.actor == "system"
    assert row.details is not None
    assert "disk on fire" in row.details["error"]


def test_hash_state_to_dict_omits_unpicklable_event() -> None:
    """``HashState.to_dict`` must JSON-serialise without the
    ``threading.Event`` slipping through (which would explode
    on FastAPI's response serialiser; see the matching test in
    test_web_catalog_manager)."""
    from bty.web._hash import HashState

    state = HashState(name="x.img", path="/var/lib/bty/images/x.img")
    d = state.to_dict()
    assert "_cancel" not in d
    import json

    encoded = json.dumps(d)
    assert json.loads(encoded)["name"] == "x.img"
