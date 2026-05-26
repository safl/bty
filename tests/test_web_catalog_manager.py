"""Tests for ``bty.web._catalog`` download manager.

Covers the manager state machine without spinning up the full
FastAPI app:

  * enqueue creates a queued state and a worker picks it up.
  * already-cached entries skip the queue and land at completed
    with bytes_done == bytes_total immediately.
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


def test_enqueue_db_only_entry_via_lookup_callback(tmp_path: Path) -> None:
    """Operator-added rows (Add image from URL form) live in the DB
    but NOT in the parsed manifest. ``db_entry_lookup`` must resolve
    them through to the worker, otherwise the per-row Fetch button
    on /ui/images 404s silently with "no catalog entry named X"
    even though the row exists in the catalog table.
    """
    payload = b"db-only-entry"
    entry = _entry(payload, name="db-only.img.zst")

    def _lookup(name: str) -> _catalog.CatalogEntry | None:
        return entry if name == entry.name else None

    async def _drive() -> None:
        image_root = tmp_path / "images"
        image_root.mkdir()
        (image_root / entry.local_filename()).write_bytes(payload)
        # Empty manifest; the lookup callback is the only way the
        # manager learns about ``db-only.img.zst``.
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, image_root, db_entry_lookup=_lookup)
        try:
            state = await mgr.enqueue(entry.name)
            # Sha pinned + already cached -> completed without a worker
            # round-trip (the fast path).
            assert state.status == "completed"
            assert state.sha256 == entry.sha256
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_db_only_entry_runs_worker(tmp_path: Path) -> None:
    """A DB-only entry that's NOT yet cached must actually download
    through the worker -- i.e. ``_run_one`` re-resolves the entry
    via the lookup callback (``self._catalog.by_name`` won't find
    it; only the DB does), then dispatches fetch_to_cache. Without
    this end-to-end path the per-row Fetch button on /ui/images
    would enqueue successfully but the worker would fail to
    re-resolve and mark the state ``failed`` with "catalog entry
    vanished" -- exactly the "downloads never go anywhere" symptom.
    """
    payload = b"db-only-runs"
    entry = _entry(payload, name="db-only-runs.img.zst")

    def _lookup(name: str) -> _catalog.CatalogEntry | None:
        return entry if name == entry.name else None

    async def _drive() -> None:
        image_root = tmp_path / "images"
        image_root.mkdir()
        # No pre-cached file -- the worker MUST download.
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, image_root, db_entry_lookup=_lookup)
        try:
            with patch("bty.catalog.urllib.request.urlopen", _mock_urlopen(payload)):
                await mgr.enqueue(entry.name)
                for _ in range(200):
                    states = await mgr.list()
                    if states and states[0].status in ("completed", "failed"):
                        break
                    await asyncio.sleep(0.01)
            states = await mgr.list()
            assert states and states[0].status == "completed", states[0] if states else None
            assert (image_root / entry.local_filename()).is_file()
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_db_only_entry_no_lookup_raises(tmp_path: Path) -> None:
    """When ``db_entry_lookup`` is None and the manifest doesn't have
    the name, the manager raises KeyError -- the unit-test contract.
    Production wires a non-None callback in _app.py's lifespan."""

    async def _drive() -> None:
        cat = _catalog.Catalog(version=1, entries=())
        mgr = DownloadManager()
        mgr.start(cat, tmp_path / "cache", db_entry_lookup=None)
        try:
            with pytest.raises(KeyError, match="no catalog entry named"):
                await mgr.enqueue("only-in-db")
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_already_cached_shortcut(tmp_path: Path) -> None:
    """An entry whose SHA already lives in image_root lands as
    completed without being queued -- no worker round-trip."""

    async def _drive() -> None:
        payload = b"already-here"
        entry = _entry(payload)
        image_root = tmp_path / "images"
        image_root.mkdir()
        (image_root / entry.local_filename()).write_bytes(payload)

        cat = _catalog.Catalog(version=1, entries=(entry,))
        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, image_root)
        try:
            state = await mgr.enqueue(entry.name)
            assert state.status == "completed"
            assert state.bytes_done == len(payload)
            assert state.bytes_total == len(payload)
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_runs_to_completion_for_unhashed_entry(tmp_path: Path) -> None:
    """Operator-visible bug fix: a catalog entry with sha256=None
    (rolling oras tag, URL-only entry not yet hashed) used to
    crash the worker because fetch_to_cache requires a pinned sha.
    The manager now dispatches to fetch_src_to_cache for un-sha'd
    entries: download + compute sha + write to image_root/<sha>,
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
        image_root = tmp_path / "images"
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
            mgr.start(cat, image_root, state_path=state_path)
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
        # File landed at image_root/<entry.local_filename()> (URL-keyed,
        # not sha-keyed -- v0.31.0+ naming).
        cached = image_root / entry.local_filename()
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


def test_backfill_uses_src_not_name_to_resist_collision(tmp_path: Path) -> None:
    """v0.33.28+: the catalog.cache.populated backfill UPDATE must
    match by ``src`` (the immutable source URL), not ``name`` (a
    free-text display label with no UNIQUE constraint). Pre-fix,
    two catalog_entries rows that happened to share a name (e.g.,
    ``debian.iso`` from different mirrors) would BOTH have their
    disk_image_sha clobbered by a single completed download from
    only one of them. Now the WHERE clause keys on src so the
    other row stays untouched.
    """
    from bty.web import _db
    from bty.web._catalog import DownloadManager

    async def _drive() -> None:
        payload = b"src-keyed-bytes" * 100
        computed_sha = hashlib.sha256(payload).hexdigest()
        # Two entries with the same display name but DIFFERENT sources.
        entry_target = _catalog.CatalogEntry(
            name="image.iso",
            src="https://mirror-A.invalid/image.iso",
            sha256=None,
        )
        entry_sibling = _catalog.CatalogEntry(
            name="image.iso",  # same name
            src="https://mirror-B.invalid/image.iso",  # different src
            sha256=None,
        )
        cat = _catalog.Catalog(version=1, entries=(entry_target, entry_sibling))
        image_root = tmp_path / "images"
        state_path = tmp_path / "state.db"
        _db.init_db(state_path)
        import sqlite3

        # Seed both rows so the backfill UPDATE has to choose between them.
        with sqlite3.connect(state_path) as conn:
            for ref_seed, ent in (("1" * 64, entry_target), ("2" * 64, entry_sibling)):
                conn.execute(
                    "INSERT INTO catalog_entries "
                    "(bty_image_ref, src, name, disk_image_sha, added_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (ref_seed, ent.src, ent.name, "2026-05-26T00:00:00+00:00"),
                )
            conn.commit()

        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload)):
            mgr.start(cat, image_root, state_path=state_path)
            try:
                # Fetch only the TARGET entry's name. (Both rows have
                # the same name; the DownloadManager picks the one
                # matching the catalog list -- which we know is the
                # target because the catalog only carried entry_target
                # first... actually, both. Enqueue by name should
                # pick the one whose enqueue resolves first.)
                await mgr.enqueue(entry_target.name)
                for _ in range(100):
                    states = await mgr.list()
                    if states and states[0].status == "completed":
                        break
                    await asyncio.sleep(0.01)
            finally:
                await mgr.stop()

        # The TARGET row got the sha; the SIBLING row stays NULL.
        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = {
                r["src"]: r["disk_image_sha"]
                for r in conn.execute("SELECT src, disk_image_sha FROM catalog_entries").fetchall()
            }
        # At least one row must carry the sha; the matching row's src
        # is whichever entry the DownloadManager's enqueue picked.
        sha_rows = {src: sha for src, sha in rows.items() if sha == computed_sha}
        assert len(sha_rows) == 1, (
            f"exactly ONE row should have been backfilled (src-keyed), "
            f"not multiple (name-keyed); got {rows!r}"
        )
        null_rows = {src for src, sha in rows.items() if sha is None}
        assert len(null_rows) == 1, (
            f"the sibling row (different src, same name) must stay NULL; got {rows!r}"
        )


def test_download_manager_backfills_from_events(tmp_path: Path) -> None:
    """``DownloadManager.start(state_path=...)`` repopulates
    ``_states`` from recent catalog.cache.populated events so the
    /ui/images Downloads table shows operator-driven fetch history
    across bty-web restarts."""
    from bty.web import _db, _events_log

    async def _drive() -> None:
        state_path = tmp_path / "state.db"
        image_root = tmp_path / "images"
        image_root.mkdir()
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
        mgr.start(cat, image_root, state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].name == "rolling.img.gz"
            assert states[0].status == "completed"
            assert states[0].sha256 == "a" * 64
            assert states[0].bytes_done == 12345
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
        image_root = tmp_path / "images"
        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload)):
            mgr.start(cat, image_root)
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
                assert states[0].bytes_done == len(payload)
            finally:
                await mgr.stop()
        cached = image_root / entry.local_filename()
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
        image_root = tmp_path / "images"
        block = threading.Event()
        mgr = DownloadManager(max_parallel=1)
        with patch("urllib.request.urlopen", _mock_urlopen(payload, hold=block)):
            mgr.start(cat, image_root)
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


def test_run_handles_catalog_entry_vanished_mid_download(tmp_path: Path) -> None:
    """REGRESSION: an operator can DELETE a catalog entry while a
    fetch is queued / running. By the time the worker picks the
    job up, the entry may have vanished -- _lookup_entry returns
    None inside _run_one. The manager must mark the state failed
    with a clear message rather than crash the worker thread on
    AttributeError("'NoneType' has no attribute 'sha256'").
    """

    async def _drive() -> None:
        entry = _catalog.CatalogEntry(
            name="ghost.img.gz",
            src="https://example.com/ghost.img.gz",
            sha256=None,
        )
        cat = _catalog.Catalog(version=1, entries=(entry,))
        image_root = tmp_path / "images"
        image_root.mkdir()

        mgr = DownloadManager(max_parallel=1)
        mgr.start(cat, image_root)

        # Patch _lookup_entry to return the entry on enqueue (so the
        # job queues normally) then None on the worker's second
        # lookup inside _run_one (simulating mid-flight delete --
        # operator hit DELETE while the fetch was queued).
        call_count = {"n": 0}
        real_lookup = mgr._lookup_entry

        def flaky_lookup(name: str) -> _catalog.CatalogEntry | None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return None
            return real_lookup(name)

        mgr._lookup_entry = flaky_lookup  # type: ignore[method-assign]
        try:
            await mgr.enqueue(entry.name)
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "failed", (
                f"vanished-entry must land as failed (not crashed); got {terminal.status!r}"
            )
            assert terminal.error == "catalog entry vanished"
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
