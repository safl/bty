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
            assert state.bytes_done == len(payload)
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
    from recent image.hashed / image.hash.failed events so the
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
            kind="image.hash.failed",
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
            assert by_name["demo.img.zst"].bytes_done == 1234
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
            kind="image.hash.failed",
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
    must land an ``image.hash.failed`` event in the audit log
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
        rows = list_events(conn, kind="image.hash.failed")
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_kind == "image"
    assert row.subject_id == "demo.img"
    assert row.actor == "system"
    assert row.details is not None
    assert "disk on fire" in row.details["error"]


def test_hash_completion_backfills_catalog_entries_by_file_src(tmp_path: Path) -> None:
    """Happy path: an operator-typed dir-scan file's catalog_entries
    row has src=``file://<name>``. When the HashManager finishes
    hashing it, the disk_image_sha column gets backfilled (the
    PXE flash flow needs disk_image_sha to be non-NULL to serve
    /images/<sha>). v0.33.x has the matching UPDATE in
    HashManager._run_one terminal callback."""
    from bty.web import _db
    from bty.web._hash import HashManager

    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    payload = b"x" * 256
    target = tmp_path / "operator.img"
    target.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()
    ref = "abc1234567890def1234567890abc1234567890abc1234567890abc1234567890"[:64]
    with _db.open_db(state_db) as conn:
        conn.execute(
            "INSERT INTO catalog_entries (bty_image_ref, src, name, format, "
            "size_bytes, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ref, "file://operator.img", "operator.img", "img", 256, "2026-05-26T00:00:00+00:00"),
        )
        conn.commit()

    async def _drive() -> None:
        mgr = HashManager(max_parallel=1)
        mgr.start(tmp_path, state_path=state_db)
        try:
            await mgr.enqueue("operator.img")
            for _ in range(100):
                states = await mgr.list()
                if states and states[0].status == "completed":
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()

    _run(_drive())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT disk_image_sha FROM catalog_entries WHERE src = ?",
            ("file://operator.img",),
        ).fetchone()
    assert row["disk_image_sha"] == expected_sha


def test_hash_completion_backfills_catalog_entries_by_ref_prefix(tmp_path: Path) -> None:
    """v0.33.28+: a catalog-fetched cache file's catalog_entries row
    has src=<upstream URL>, NOT ``file://catalog-<ref:12>-...``. A
    manual ``POST /catalog/hashes/<catalog-name>`` would compute the
    sha but the src-keyed UPDATE wouldn't find the row. The
    ref-prefix LIKE clause does -- the 12-hex prefix in the cache
    filename matches the catalog_entries.bty_image_ref column.
    Without this backfill, a manual re-hash leaves the catalog
    row's disk_image_sha NULL even though the file is fully hashed
    and ready to serve."""
    from bty.web import _db
    from bty.web._hash import HashManager

    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    payload = b"y" * 256
    expected_sha = hashlib.sha256(payload).hexdigest()
    # Construct a catalog row whose 12-hex bty_image_ref prefix
    # matches the filename we'll write under image_root.
    ref = "0123456789ab" + "f" * 52  # 64 hex chars, prefix '0123456789ab'
    cache_name = "catalog-0123456789ab-someimage.img"
    (tmp_path / cache_name).write_bytes(payload)
    with _db.open_db(state_db) as conn:
        conn.execute(
            "INSERT INTO catalog_entries (bty_image_ref, src, name, format, "
            "size_bytes, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ref,
                "https://example.invalid/someimage.img",
                "someimage",
                "img",
                256,
                "2026-05-26T00:00:00+00:00",
            ),
        )
        conn.commit()

    async def _drive() -> None:
        mgr = HashManager(max_parallel=1)
        mgr.start(tmp_path, state_path=state_db)
        try:
            await mgr.enqueue(cache_name)
            for _ in range(100):
                states = await mgr.list()
                if states and states[0].status == "completed":
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()

    _run(_drive())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT disk_image_sha FROM catalog_entries WHERE bty_image_ref = ?",
            (ref,),
        ).fetchone()
    assert row["disk_image_sha"] == expected_sha


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


def test_enqueue_before_start_raises(tmp_path: Path) -> None:
    """``enqueue`` without ``start`` is a programmer error -- mirrors
    the ReleaseFetchManager / DownloadManager safety check. The
    image_root isn't bound yet so the hash worker would have no
    file to hash. Surface as RuntimeError rather than crashing in
    the worker thread."""
    from bty.web._hash import HashManager

    async def _drive() -> None:
        mgr = HashManager(max_parallel=1)
        with pytest.raises(RuntimeError, match="not started"):
            await mgr.enqueue("anything.img")

    _run(_drive())


def test_resolve_max_parallel_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BTY_HASH_MAX_PARALLEL`` overrides the default. Out-of-range
    or non-numeric values fall back to the default rather than
    raising at startup."""
    from bty.web._hash import DEFAULT_MAX_PARALLEL, _resolve_max_parallel

    monkeypatch.setenv("BTY_HASH_MAX_PARALLEL", "4")
    assert _resolve_max_parallel() == 4

    monkeypatch.setenv("BTY_HASH_MAX_PARALLEL", "0")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL

    monkeypatch.setenv("BTY_HASH_MAX_PARALLEL", "-1")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL

    monkeypatch.setenv("BTY_HASH_MAX_PARALLEL", "abc")
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL

    monkeypatch.delenv("BTY_HASH_MAX_PARALLEL", raising=False)
    assert _resolve_max_parallel() == DEFAULT_MAX_PARALLEL


def test_enqueue_explicit_hash_cancelled_lands_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``_images.ensure_sha256`` raises ``HashCancelled``
    explicitly (the cancel check between chunks fires before any
    other exception), the state lands at ``cancelled`` with no
    error message. Distinct from the cancel-during-IO race covered
    by ``test_run_hash_cancel_with_concurrent_oserror_marks_cancelled``."""
    from bty import images as _images
    from bty.web import _db, _hash
    from bty.web._hash import HashManager

    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.img").write_bytes(b"x" * 1024)
    state_path = tmp_path / "state.db"
    _db.init_db(state_path)

    def fake_ensure_sha256(*_a: object, **_kw: object) -> str:
        raise _images.HashCancelled("operator cancelled")

    monkeypatch.setattr(_hash._images, "ensure_sha256", fake_ensure_sha256)

    async def _drive() -> None:
        mgr = HashManager(max_parallel=1)
        mgr.start(image_root, state_path=state_path)
        try:
            await mgr.enqueue("demo.img")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "cancelled"
            assert terminal.error is None
        finally:
            await mgr.stop()

    _run(_drive())

    # v0.33.29+: the cancelled terminal lands an audit event too,
    # closing the lifecycle loop. Pre-fix the manager flipped
    # _states but wrote nothing to /ui/events; an operator
    # scrolling the timeline saw started -> nothing.
    from bty.web._events_log import list_events as _list_events

    with _db.open_db(state_path) as conn:
        events = _list_events(conn, subject_kind="image", limit=20)
    kinds = [e.kind for e in reversed(events)]  # oldest first
    assert "image.hash.started" in kinds, kinds
    assert "image.hash.cancelled" in kinds, kinds
    assert "image.hashed" not in kinds
    started_idx = kinds.index("image.hash.started")
    cancelled_idx = kinds.index("image.hash.cancelled")
    assert started_idx < cancelled_idx


def test_hash_lifecycle_emits_started_before_terminal(tmp_path: Path) -> None:
    """v0.33.29+: every hash now emits ``image.hash.started`` when
    the worker picks it up + ``image.hashed`` (success) /
    ``image.hash.failed`` / ``image.hash.cancelled`` as terminal.
    Operators get queue-vs-running visibility on /ui/events instead
    of inferring from absence."""
    from bty.web import _db
    from bty.web._events_log import list_events as _list_events
    from bty.web._hash import HashManager

    image_root = tmp_path / "images"
    image_root.mkdir()
    payload = b"happy-path-bytes" * 64
    (image_root / "happy.img").write_bytes(payload)
    state_path = tmp_path / "state.db"
    _db.init_db(state_path)

    async def _drive() -> None:
        mgr = HashManager(max_parallel=1)
        mgr.start(image_root, state_path=state_path)
        try:
            await mgr.enqueue("happy.img")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status == "completed":
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()

    _run(_drive())

    with _db.open_db(state_path) as conn:
        events = _list_events(conn, subject_kind="image", limit=20)
    kinds = [e.kind for e in reversed(events)]  # oldest first
    assert "image.hash.started" in kinds, kinds
    assert "image.hashed" in kinds, kinds
    assert kinds.index("image.hash.started") < kinds.index("image.hashed")
