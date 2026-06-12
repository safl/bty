"""Base-manager-only regression tests for ``bty.web._jobs``.

The base manager is exercised end-to-end through ReleaseFetchManager
and BackupManager (in their own test files). This file pins the
behaviours that belong to the BASE, not any one subclass:

* The worker's safety-net catch translates a leaked ``_run_one``
  exception into ``status=failed`` + ``error`` on the state and a
  state-change fire, so the operator sees a recoverable failure
  rather than a job wedged in "running" while a worker slot
  silently dies.

Production subclasses already wrap their ``_run_one`` body in a
``try/except Exception`` and write the terminal status themselves,
so this safety net only fires when a future subclass forgets, OR
when the subclass's own except clause itself raises. Test against
a minimal subclass that deliberately leaks.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from bty.web._jobs import _BaseAsyncManager


def _run(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


@dataclass
class _MinimalState:
    """Smallest state shape that satisfies the ``_CancelableState``
    protocol. Mirrors the structural-typed protocol fields."""

    key: str
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)


class _LeakingManager(_BaseAsyncManager[_MinimalState]):
    """Subclass whose ``_run_one`` raises an unhandled exception, so
    the base manager's safety net is the only thing standing between
    the operator and a forever-running job."""

    def __init__(self) -> None:
        super().__init__(max_parallel=1)

    def start(self) -> None:
        self._spawn_workers()

    async def enqueue(self, key: str) -> None:
        async with self._lock:
            self._states[key] = _MinimalState(key=key)
        await self._queue.put(key)

    async def _run_one(self, state: _MinimalState) -> None:
        raise RuntimeError(f"boom: {state.key}")


def test_worker_safety_net_marks_failed_when_run_one_leaks() -> None:
    """If ``_run_one`` raises an unhandled exception, the base
    manager's worker catches it, marks the state ``failed`` with a
    typed ``error``, fires the state-change listener, and continues
    pulling work from the queue (the worker slot survives)."""
    fires: list[tuple[str, str]] = []

    async def _drive() -> None:
        mgr = _LeakingManager()
        mgr.set_state_listener(lambda st: fires.append((st.key, st.status)))
        mgr.start()
        try:
            await mgr.enqueue("first")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("failed", "completed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "failed"
            assert terminal.finished_at is not None
            assert terminal.error is not None
            assert "RuntimeError" in terminal.error
            assert "boom: first" in terminal.error

            # Worker slot survived: enqueueing a second key still
            # gets picked up and lands as failed (vs. queued forever
            # which is what a dead worker would produce).
            await mgr.enqueue("second")
            for _ in range(200):
                states = await mgr.list()
                second = next((s for s in states if s.key == "second"), None)
                if second is not None and second.status == "failed":
                    break
                await asyncio.sleep(0.01)
            second = next(s for s in (await mgr.list()) if s.key == "second")
            assert second.status == "failed"
        finally:
            await mgr.stop()

    _run(_drive())
    # The listener fired the queued->running transition AND the
    # running->failed safety-net transition for both keys.
    assert ("first", "running") in fires
    assert ("first", "failed") in fires
    assert ("second", "running") in fires
    assert ("second", "failed") in fires


def test_worker_safety_net_does_not_overwrite_subclass_terminal_status() -> None:
    """If ``_run_one`` writes its own terminal status BEFORE raising,
    the safety net must not stomp on it. The guard is the
    ``status == "running"`` re-check inside the post-exception
    lock."""

    class _SubclassTerminal(_BaseAsyncManager[_MinimalState]):
        def __init__(self) -> None:
            super().__init__(max_parallel=1)

        def start(self) -> None:
            self._spawn_workers()

        async def enqueue(self, key: str) -> None:
            async with self._lock:
                self._states[key] = _MinimalState(key=key)
            await self._queue.put(key)

        async def _run_one(self, state: _MinimalState) -> None:
            async with self._lock:
                state.status = "cancelled"
                state.finished_at = time.time()
                state.error = "cancelled by subclass"
            raise RuntimeError("post-terminal leak should not reclassify")

    async def _drive() -> None:
        mgr = _SubclassTerminal()
        mgr.start()
        try:
            await mgr.enqueue("k")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("failed", "completed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "cancelled"
            assert terminal.error == "cancelled by subclass"
        finally:
            await mgr.stop()

    _run(_drive())
