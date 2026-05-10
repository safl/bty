"""Tests for ``bty.web._catalog`` (M22 download manager).

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
