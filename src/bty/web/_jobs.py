"""Shared asyncio-supervised worker-pool scaffolding.

bty-web has three managers that drive the same shape: an asyncio
worker pool over an :class:`asyncio.Queue` of keys, a per-key
``State`` dataclass with ``status`` / ``started_at`` / ``finished_at``
/ ``error`` / ``_cancel: threading.Event``, and a
:meth:`asyncio.to_thread`-wrapped blocking job body. The differences
are confined to:

  * the bind args (image-root / catalog+cache-dir / boot-root),
  * the state class and its key (filename / catalog-name / tag),
  * the per-key idempotency rules (HashManager has a sidecar-cached
    short-circuit; the others don't),
  * the body of :meth:`_run_one`.

Everything else -- ``stop()``, ``cancel()``, ``list()``, the
``_worker`` queue loop, the cancel-vs-IO-error race resolution
on terminal status -- is identical across all three. This module
extracts the identical parts to a generic base.

Subclasses MUST:

  * Inherit ``_BaseAsyncManager[StateT]``.
  * Implement ``_run_one(state)`` -- the per-job body. State
    carries everything the body needs (the manager owns the bind
    args via attributes set in ``start``).
  * Provide their own ``start(bind_args...)`` that captures bind
    args and then calls ``self._spawn_workers()``.
  * Provide their own ``enqueue(key)`` (idempotency rules
    differ).

State dataclasses MUST carry attributes:

  * ``status: str`` -- one of ``"queued" | "running" | "completed"
    | "cancelled" | "failed"``.
  * ``started_at: float | None``.
  * ``finished_at: float | None``.
  * ``_cancel: threading.Event`` -- set by the API's cancel
    handler; the worker thread polls it and the
    :meth:`_run_one` body raises a per-manager Cancelled exception
    on True.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Generic, Protocol, TypeVar


class _CancelableState(Protocol):
    """Structural type the base manager expects of state objects."""

    status: str
    started_at: float | None
    finished_at: float | None

    @property
    def _cancel(self) -> object: ...


StateT = TypeVar("StateT", bound=_CancelableState)


class _BaseAsyncManager(Generic[StateT]):
    """Shared lifecycle for the per-key worker-pool managers."""

    def __init__(self, max_parallel: int) -> None:
        self._max_parallel = max_parallel
        self._states: dict[str, StateT] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def _spawn_workers(self) -> None:
        """Spawn ``max_parallel`` workers. Subclass ``start()`` calls
        this after stashing its bind args."""
        if self._workers:
            raise RuntimeError(f"{type(self).__name__} already started")
        self._stopping = False
        for n in range(self._max_parallel):
            self._workers.append(asyncio.create_task(self._worker(n)))

    async def stop(self) -> None:
        """Drain queued jobs, signal in-flight ones to abort, await
        worker termination. Idempotent."""
        self._stopping = True
        async with self._lock:
            for st in self._states.values():
                if st.status in ("queued", "running"):
                    st._cancel.set()  # type: ignore[attr-defined]
                    if st.status == "queued":
                        st.status = "cancelled"
                        st.finished_at = time.time()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w
        self._workers.clear()

    async def cancel(self, key: str) -> StateT | None:
        """Flip the per-job cancel event. Returns the state (whatever
        its current status) or ``None`` if no state is known for
        ``key``. Permissive on already-finished states: returns the
        state with no mutation, so the API layer can treat DELETE
        as idempotent."""
        async with self._lock:
            state = self._states.get(key)
            if state is None:
                return None
            if state.status not in ("queued", "running"):
                return state
            state._cancel.set()  # type: ignore[attr-defined]
            if state.status == "queued":
                state.status = "cancelled"
                state.finished_at = time.time()
            return state

    async def list(self) -> list[StateT]:
        async with self._lock:
            return list(self._states.values())

    async def _worker(self, _idx: int) -> None:
        """Pull keys off the queue, mark running, dispatch to
        :meth:`_run_one`. Identical across managers; the body lives
        in the subclass-provided :meth:`_run_one`."""
        while not self._stopping:
            try:
                key = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                async with self._lock:
                    state = self._states.get(key)
                    if state is None or state.status != "queued":
                        continue
                    state.status = "running"
                    state.started_at = time.time()
                await self._run_one(state)
            except asyncio.CancelledError:
                return

    async def _run_one(self, state: StateT) -> None:
        """Per-job body. Subclasses must override.

        Implementations call :func:`asyncio.to_thread` against their
        blocking worker (sha-hashing / catalog-fetching / release-
        fetching), wiring up ``progress`` and ``cancel`` callbacks
        bound to ``state``. On exit, the implementation acquires
        ``self._lock`` and writes the terminal status, finished_at,
        and any per-manager metadata onto ``state``.
        """
        raise NotImplementedError
