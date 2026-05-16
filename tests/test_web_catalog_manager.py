"""Tests for ``bty.web._catalog`` download manager.

Covers the manager state machine without spinning up the full
FastAPI app:

  * enqueue creates a queued state and a worker picks it up.
  * already-cached entries skip the queue and land at completed
    with bytes_downloaded == bytes_total immediately.
  * cancel flips state to cancelled.
  * unknown names produce KeyError / None as appropriate.

Network is mocked at the underlying ``urlopen`` so tests are
hermetic. Async test bodies run via ``asyncio.run`` so we don't
need a pytest-asyncio dependency (matching ``tests/test_tui.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bty import catalog as _catalog
from bty.web._catalog import DownloadManager


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _entry(payload: bytes, name: str = "demo.img.zst") -> _catalog.CatalogEntry:
    return _catalog.CatalogEntry(
        name=name,
        src="https://example.com/" + name,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _mock_urlopen(payload: bytes, *, hold: threading.Event | None = None):
    """Mock urlopen. Optional ``hold`` event blocks the read so a
    test can observe the running state before the worker finishes."""

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._buf.close()
            return False

        def read(self, n: int = -1) -> bytes:
            if hold is not None:
                hold.wait()
            return self._buf.read(n)

    return lambda *_a, **_kw: _Resp(payload)


# -----------------------------------------------------------------------
# enqueue / completed / cached-shortcut
# -----------------------------------------------------------------------


def test_enqueue_unknown_name_raises(tmp_path: Path) -> None:
    async def _drive() -> None:
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager()
        mgr.start(cat, tmp_path / "cache")
        try:
            with pytest.raises(KeyError):
                await mgr.enqueue("nope")
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_already_cached_shortcut(tmp_path: Path) -> None:
    """An entry whose SHA already lives in cache_dir lands as
    completed without being queued -- no worker round-trip."""

    async def _drive() -> None:
        payload = b"already-here"
        entry = _entry(payload)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / entry.sha256).write_bytes(payload)

        cat = _catalog.Catalog(version=1, entries=(entry,))
        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, cache_dir)
        try:
            state = await mgr.enqueue(entry.name)
            assert state.status == "completed"
            assert state.bytes_downloaded == len(payload)
            assert state.bytes_total == len(payload)
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_runs_to_completion_for_unhashed_entry(tmp_path: Path) -> None:
    """Operator-visible bug fix: a catalog entry with sha256=None
    (rolling oras tag, URL-only entry not yet hashed) used to
    crash the worker because fetch_to_cache requires a pinned sha.
    The manager now dispatches to fetch_src_to_cache for un-sha'd
    entries: download + compute sha + write to cache_dir/<sha>,
    set state.sha256 to the computed value, and back-fill
    catalog_entries.disk_image_sha when state_path is given.
    """
    from bty.web import _db, _events_log

    async def _drive() -> None:
        payload = b"un-sha'd-bytes" * 100
        computed_sha = hashlib.sha256(payload).hexdigest()
        entry = _catalog.CatalogEntry(
            name="rolling.img.gz",
            src="https://example.com/rolling.img.gz",
            sha256=None,
        )
        cat = _catalog.Catalog(version=1, entries=(entry,))
        cache_dir = tmp_path / "cache"
        state_path = tmp_path / "state.db"
        # Seed an existing catalog_entries row so the back-fill
        # update has something to land on.
        _db.init_db(state_path)
        import sqlite3

        with sqlite3.connect(state_path) as conn:
            conn.execute(
                "INSERT INTO catalog_entries "
                "(bty_image_ref, src, name, disk_image_sha, added_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (
                    "1" * 64,
                    entry.src,
                    entry.name,
                    "2026-05-16T00:00:00+00:00",
                ),
            )
            conn.commit()
        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload)):
            mgr.start(cat, cache_dir, state_path=state_path)
            try:
                await mgr.enqueue(entry.name)
                for _ in range(100):
                    states = await mgr.list()
                    if states and states[0].status == "completed":
                        break
                    await asyncio.sleep(0.01)
                states = await mgr.list()
                assert len(states) == 1
                assert states[0].status == "completed"
                # Computed sha lands on the state.
                assert states[0].sha256 == computed_sha
            finally:
                await mgr.stop()
        # File landed at cache_dir/<computed_sha>.
        cached = cache_dir / computed_sha
        assert cached.is_file()
        assert cached.read_bytes() == payload
        # Back-fill: catalog_entries row now carries the sha.
        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT disk_image_sha FROM catalog_entries WHERE name = ?",
                (entry.name,),
            ).fetchone()
            assert row["disk_image_sha"] == computed_sha
        # And a catalog.cache.populated event was recorded.
        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            events = _events_log.list_events(conn, kind="catalog.cache.populated", limit=10)
        assert any(e.subject_id == entry.name for e in events)

    _run(_drive())


def test_download_manager_backfills_from_events(tmp_path: Path) -> None:
    """``DownloadManager.start(state_path=...)`` repopulates
    ``_states`` from recent catalog.cache.populated events so the
    /ui/images Downloads table shows operator-driven fetch history
    across bty-web restarts."""
    from bty.web import _db, _events_log

    async def _drive() -> None:
        state_path = tmp_path / "state.db"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _db.init_db(state_path)
        import sqlite3

        with sqlite3.connect(state_path) as conn:
            _events_log.record(
                conn,
                kind="catalog.cache.populated",
                summary="fetched rolling.img.gz",
                subject_kind="catalog",
                subject_id="rolling.img.gz",
                actor="operator",
                details={
                    "name": "rolling.img.gz",
                    "src": "https://example.com/rolling.img.gz",
                    "disk_image_sha": "a" * 64,
                    "size_bytes": 12345,
                },
            )
            # Cache-through event (no ``name`` key) -- should be
            # filtered out by the backfill.
            _events_log.record(
                conn,
                kind="catalog.cache.populated",
                summary="cache-through for ref=...",
                subject_kind="catalog",
                subject_id="b" * 64,
                actor="pxe-client",
                details={"src": "oras://...", "disk_image_sha": "b" * 64},
            )
            conn.commit()
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, cache_dir, state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].name == "rolling.img.gz"
            assert states[0].status == "completed"
            assert states[0].sha256 == "a" * 64
            assert states[0].bytes_downloaded == 12345
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_runs_to_completion(tmp_path: Path) -> None:
    """The default happy path: enqueue, worker runs, status flips
    queued -> running -> completed, bytes match."""

    async def _drive() -> None:
        payload = b"a" * (1 << 12)  # 4 KiB so the test is fast
        entry = _entry(payload)
        cat = _catalog.Catalog(version=1, entries=(entry,))
        cache_dir = tmp_path / "cache"
        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload)):
            mgr.start(cat, cache_dir)
            try:
                await mgr.enqueue(entry.name)
                # Poll briefly for completion.
                for _ in range(50):
                    states = await mgr.list()
                    if states and states[0].status == "completed":
                        break
                    await asyncio.sleep(0.01)
                states = await mgr.list()
                assert len(states) == 1
                assert states[0].status == "completed"
                assert states[0].bytes_downloaded == len(payload)
            finally:
                await mgr.stop()
        cached = cache_dir / entry.sha256
        assert cached.is_file()
        assert cached.read_bytes() == payload

    _run(_drive())


# -----------------------------------------------------------------------
# cancel
# -----------------------------------------------------------------------


def test_cancel_queued_immediate_state_flip(tmp_path: Path) -> None:
    """Cancelling a download that's still in the queue (workers
    busy with other items) flips the state synchronously to
    'cancelled' without waiting for a worker pickup."""

    async def _drive() -> None:
        payload = b"x" * 64
        entries = tuple(_entry(payload, name=f"e{i}.img.zst") for i in range(3))
        cat = _catalog.Catalog(version=1, entries=entries)
        cache_dir = tmp_path / "cache"
        block = threading.Event()
        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload, hold=block)):
            mgr.start(cat, cache_dir)
            try:
                await mgr.enqueue("e0.img.zst")  # picked up immediately
                queued_state = await mgr.enqueue("e1.img.zst")
                assert queued_state.status == "queued"
                cancelled = await mgr.cancel("e1.img.zst")
                assert cancelled is not None
                assert cancelled.status == "cancelled"
            finally:
                block.set()
                await mgr.stop()

    _run(_drive())


def test_cancel_unknown_returns_none(tmp_path: Path) -> None:
    async def _drive() -> None:
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager()
        mgr.start(cat, tmp_path / "cache")
        try:
            assert await mgr.cancel("nothing") is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_run_fetch_cancel_with_concurrent_catalog_error_marks_cancelled(
    tmp_path: Path,
) -> None:
    """If the operator cancels mid-fetch and urllib happens to
    raise (e.g. server tore the connection on cancel), the
    DownloadManager must report cancellation, not failure --
    surface UX matters: an error badge for what was actually
    a cancel button is a lie."""
    import unittest.mock

    from bty.web._catalog import DownloadManager, DownloadState

    payload = b"unused"
    entry = _entry(payload, name="demo.img.zst")

    async def _drive() -> None:
        cat = _catalog.Catalog(version=1, entries=(entry,))
        mgr = DownloadManager()
        mgr.start(cat, tmp_path / "cache")
        try:
            state = DownloadState(name=entry.name, sha256=entry.sha256, src=entry.src)
            state._cancel.set()  # simulate operator cancel before the error

            def boom(*_a: object, **_kw: object) -> None:
                raise _catalog.CatalogError("connection reset")

            with unittest.mock.patch.object(_catalog, "fetch_to_cache", boom):
                await mgr._run_one(state)

            assert state.status == "cancelled"
            assert state.error is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_rejects_traversal_names(tmp_path: Path) -> None:
    """``DownloadManager.enqueue`` must reject names with path
    separators or traversal segments at the manager boundary,
    matching the same defence on ``HashManager``."""

    async def _drive() -> None:
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager()
        mgr.start(cat, tmp_path / "cache")
        try:
            for bad in ("../etc/passwd", "foo/bar", "..", ".", "", "a\\b", "a\0b"):
                with pytest.raises(ValueError, match=r"invalid name"):
                    await mgr.enqueue(bad)
        finally:
            await mgr.stop()

    _run(_drive())


def test_download_state_to_dict_omits_unpicklable_event() -> None:
    """``DownloadState.to_dict`` must not emit the
    ``threading.Event`` (which contains an unpicklable
    ``_thread.lock``) -- otherwise FastAPI's response serialiser
    raises ``TypeError: cannot pickle '_thread.lock' object`` the
    moment the manager has any state. The fix uses an explicit
    field list rather than ``dataclasses.asdict``; this test
    pins it so a future field addition that re-introduces the
    bug fails fast."""
    from bty.web._catalog import DownloadState

    state = DownloadState(name="x", sha256="a" * 64, src="http://x")
    d = state.to_dict()
    assert "_cancel" not in d
    # Round-trip through json to mirror what FastAPI does.
    import json

    encoded = json.dumps(d)
    assert "name" in json.loads(encoded)
