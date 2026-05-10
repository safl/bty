"""Tests for ``bty.web._hash`` (M22 layer 5+: background hash queue).

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
