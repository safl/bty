"""bty-web download manager for the catalog (M22).

Routes catalog fetches through an asyncio-supervised worker pool
so the operator can:

  * watch live progress (bytes downloaded / total / percent) for
    every active fetch via ``GET /catalog/downloads``,
  * cancel an in-flight fetch via ``DELETE /catalog/downloads/{name}``
    -- the worker checks the cancel flag between 1 MiB chunks and
    aborts within seconds, leaving no half-written cache,
  * cap parallelism so a typo on the catalog page doesn't trigger
    five simultaneous multi-GiB downloads (limit via
    ``BTY_CATALOG_MAX_PARALLEL``, default 2).

State is in-memory; on server restart, in-flight downloads die with
the worker and the cache directory is the source of truth for "what
is cached" (the no-half-written invariant from
``bty.catalog.fetch_to_cache`` survives restart -- a partial download
leaves no file).

Module is layered on top of ``bty.catalog``: this file holds the
async + state machinery, ``bty.catalog`` holds the byte-pumping +
SHA verification. Keeps the CLI path (``bty catalog fetch``) free
of any asyncio dependency.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bty import catalog as _catalog

# Default cap on simultaneous downloads. Tuned so a typical homelab
# uplink isn't saturated by N parallel fetches; bumpable via env.
DEFAULT_MAX_PARALLEL = 2


@dataclass
class DownloadState:
    """Live state of a single catalog fetch.

    Mutable on purpose -- the worker updates ``status`` /
    ``bytes_downloaded`` / ``bytes_total`` / timestamps as the
    download proceeds, and the API serialises the current snapshot
    for ``GET /catalog/downloads``.
    """

    name: str
    sha256: str
    src: str
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_downloaded: int = 0
    bytes_total: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # Threading.Event because the actual IO happens in a worker
    # thread (via ``asyncio.to_thread``); ``asyncio.Event`` is not
    # thread-safe to query from inside the thread.
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # Build manually rather than via ``dataclasses.asdict`` --
        # that helper deep-copies every field, and threading.Event
        # contains a ``_thread.lock`` which cannot be pickled.
        return {
            "name": self.name,
            "sha256": self.sha256,
            "src": self.src,
            "status": self.status,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class DownloadManager:
    """Async worker-pool scheduler for catalog fetches.

    Lifecycle:

      1. ``start(catalog, cache_dir)`` spawns ``max_parallel``
         worker coroutines, each pulling names off the queue.
      2. ``enqueue(name)`` validates the name against the catalog
         and either returns the existing state (if already
         queued / running / completed) or registers a new
         ``DownloadState`` and pushes the name onto the queue.
      3. ``cancel(name)`` flips the per-download cancel event;
         the worker thread sees it on the next chunk boundary
         and raises ``CatalogCancelled``, which we translate to
         ``status="cancelled"``.
      4. ``stop()`` cancels every queued download and signals the
         workers to drain. Called from the FastAPI shutdown hook.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        self._max_parallel = max_parallel or _resolve_max_parallel()
        self._catalog: _catalog.Catalog | None = None
        self._cache_dir: Path | None = None
        self._states: dict[str, DownloadState] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        # Lock for read-modify-write of ``_states`` from API
        # handlers (which run in the event-loop thread alongside
        # workers). asyncio.Lock is sufficient since all mutation
        # happens in the event loop.
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def start(self, catalog: _catalog.Catalog, cache_dir: Path) -> None:
        """Bind the manager to a manifest + cache dir and spawn workers.

        Idempotent within a process: a second ``start`` after
        ``stop`` would need to reset ``_stopping`` -- we don't
        support hot manifest reload here in v1.
        """
        if self._workers:
            raise RuntimeError("DownloadManager already started")
        self._catalog = catalog
        self._cache_dir = cache_dir
        self._stopping = False
        for n in range(self._max_parallel):
            self._workers.append(asyncio.create_task(self._worker(n)))

    async def stop(self) -> None:
        """Cancel queued downloads, signal in-flight ones to abort,
        and wait for workers to drain. Idempotent."""
        self._stopping = True
        async with self._lock:
            for st in self._states.values():
                if st.status in ("queued", "running"):
                    st._cancel.set()
                    if st.status == "queued":
                        st.status = "cancelled"
                        st.finished_at = time.time()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    async def enqueue(self, name: str) -> DownloadState:
        """Look up the entry, create / re-use a state, and push
        onto the queue if a fresh download is needed.

        Returns the current ``DownloadState`` (which may already be
        ``running`` / ``completed`` / ``cancelled``). Callers expect
        idempotency on repeat enqueues for the same name.
        """
        if self._catalog is None or self._cache_dir is None:
            raise RuntimeError("DownloadManager not started")
        entry = self._catalog.by_name(name)
        if entry is None:
            raise KeyError(f"no catalog entry named {name!r}")

        async with self._lock:
            existing = self._states.get(name)
            if existing is not None:
                if existing.status in ("queued", "running"):
                    return existing
                if existing.status == "completed":
                    return existing
                # ``cancelled`` / ``failed`` -- allow a fresh attempt.
            state = DownloadState(
                name=entry.name,
                sha256=entry.sha256,
                src=entry.src,
            )
            # If already cached on disk, mark complete immediately
            # without enqueueing -- saves a worker round-trip.
            if _catalog.is_cached(entry, self._cache_dir):
                size = entry.cached_path(self._cache_dir).stat().st_size
                state.status = "completed"
                state.bytes_downloaded = size
                state.bytes_total = size
                state.started_at = state.finished_at = time.time()
                self._states[name] = state
                return state
            self._states[name] = state
            await self._queue.put(name)
            return state

    async def cancel(self, name: str) -> DownloadState | None:
        """Flip the cancel flag for an active download.

        Returns the (now-updated) state on success, ``None`` if no
        such download exists or it is already finished.
        """
        async with self._lock:
            state = self._states.get(name)
            if state is None:
                return None
            if state.status not in ("queued", "running"):
                return state
            state._cancel.set()
            # Queued downloads never reached the worker; mark them
            # cancelled inline so the operator sees the state flip
            # immediately. Running downloads transition in the
            # worker after the next chunk-boundary cancel poll.
            if state.status == "queued":
                state.status = "cancelled"
                state.finished_at = time.time()
            return state

    async def list(self) -> list[DownloadState]:
        """Snapshot of every download the manager knows about.

        Returns a list copy so callers can iterate without holding
        the lock; the underlying ``DownloadState`` objects are
        still mutated by workers, but the list itself is stable.
        """
        async with self._lock:
            return list(self._states.values())

    async def _worker(self, _idx: int) -> None:
        """Worker coroutine. Loops pulling names off the queue.

        Each iteration:

          1. ``await self._queue.get()``.
          2. Look up the state + entry. If state is no longer
             ``queued`` (operator cancelled before pickup), skip.
          3. Mark ``running``, delegate to ``_run_fetch`` which
             dispatches ``fetch_to_cache`` on a worker thread.
          4. Update final status based on outcome (completed /
             cancelled / failed). Save error message on failure.
        """
        assert self._catalog is not None
        assert self._cache_dir is not None
        while not self._stopping:
            try:
                name = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                async with self._lock:
                    state = self._states.get(name)
                    if state is None or state.status != "queued":
                        # Cancelled before pickup; nothing to do.
                        continue
                    entry = self._catalog.by_name(name)
                    if entry is None:
                        state.status = "failed"
                        state.error = "manifest entry vanished"
                        state.finished_at = time.time()
                        continue
                    state.status = "running"
                    state.started_at = time.time()
                # Past the lock, ``state`` and ``entry`` are
                # concrete; pass them by argument so the helper's
                # closures bind cleanly without mypy gymnastics.
                await self._run_fetch(state, entry)
            except asyncio.CancelledError:
                return

    async def _run_fetch(self, state: DownloadState, entry: _catalog.CatalogEntry) -> None:
        """Run a single fetch in a worker thread, then snapshot
        the result back into ``state``. Split out of ``_worker``
        so the progress / cancel closures bind to non-Optional
        argument types -- otherwise mypy refuses the default-arg
        binding trick that B023 forces us to use.
        """
        assert self._cache_dir is not None
        cancel_event = state._cancel

        def _progress(downloaded: int, total: int | None) -> None:
            state.bytes_downloaded = downloaded
            if total is not None:
                state.bytes_total = total

        def _cancel() -> bool:
            return cancel_event.is_set()

        try:
            await asyncio.to_thread(
                _catalog.fetch_to_cache,
                entry,
                self._cache_dir,
                progress=_progress,
                cancel=_cancel,
            )
            final_status = "completed"
            error = None
        except _catalog.CatalogCancelled:
            final_status = "cancelled"
            error = None
        except _catalog.CatalogError as exc:
            final_status = "failed"
            error = str(exc)
        except (OSError, Exception) as exc:  # network, IO, anything else
            final_status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error


def _resolve_max_parallel() -> int:
    raw = os.environ.get("BTY_CATALOG_MAX_PARALLEL")
    if raw is None:
        return DEFAULT_MAX_PARALLEL
    try:
        n = int(raw)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        # Bad value silently falls back to the default rather than
        # blocking server startup; logged elsewhere.
        return DEFAULT_MAX_PARALLEL
