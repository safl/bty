"""bty-web download manager for the catalog.

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

Lifecycle plumbing lives in :class:`bty.web._jobs._BaseAsyncManager`.
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
from bty.web._jobs import _BaseAsyncManager

# Default cap on simultaneous downloads. Tuned so a typical homelab
# uplink isn't saturated by N parallel fetches; bumpable via env.
DEFAULT_MAX_PARALLEL = 2


def _reject_traversal_name(name: str) -> None:
    """Reject anything that's not a plain basename. Mirrors the
    same check on :class:`bty.web._hash.HashManager` so a
    non-API caller can't slip a path-traversal name past the
    catalog lookup."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(
            f"invalid name {name!r}: must be a basename without path separators or NUL bytes"
        )


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


class DownloadManager(_BaseAsyncManager[DownloadState]):
    """Async worker-pool scheduler for catalog fetches.

    ``start(catalog, cache_dir)`` spawns workers, ``enqueue(name)``
    queues a job (idempotent on already-queued / completed /
    running), ``cancel(name)`` flips the per-job event, ``stop()``
    drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or _resolve_max_parallel())
        self._catalog: _catalog.Catalog | None = None
        self._cache_dir: Path | None = None

    def start(self, catalog: _catalog.Catalog, cache_dir: Path) -> None:
        """Bind the manager to a manifest + cache dir and spawn workers."""
        self._catalog = catalog
        self._cache_dir = cache_dir
        self._spawn_workers()

    async def enqueue(self, name: str) -> DownloadState:
        """Look up the entry, create / re-use a state, and push
        onto the queue if a fresh download is needed.

        Returns the current ``DownloadState`` (which may already be
        ``running`` / ``completed`` / ``cancelled``). Callers expect
        idempotency on repeat enqueues for the same name.

        Raises :class:`ValueError` if ``name`` carries path-
        traversal characters. The catalog lookup at
        :meth:`bty.catalog.ParsedCatalog.by_name` would already
        return ``None`` for those (no catalog entry matches), but
        rejecting at the boundary makes the failure mode explicit
        and lines up with :class:`bty.web._hash.HashManager`.
        """
        _reject_traversal_name(name)
        if self._catalog is None or self._cache_dir is None:
            raise RuntimeError("DownloadManager not started")
        entry = self._catalog.by_name(name)
        if entry is None:
            raise KeyError(f"no catalog entry named {name!r}")

        async with self._lock:
            existing = self._states.get(name)
            if existing is not None and existing.status in ("queued", "running", "completed"):
                return existing
            # ``cancelled`` / ``failed`` (or no existing state) -- create a fresh one.
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

    async def _run_one(self, state: DownloadState) -> None:
        """Run a single fetch in a worker thread, snapshot the
        result back into ``state``."""
        assert self._catalog is not None
        assert self._cache_dir is not None
        cancel_event = state._cancel

        # Re-resolve the entry inside the worker. ``enqueue`` looked
        # it up at submit time, but the manifest could in theory
        # have been reloaded between then and now; re-resolving
        # at run time keeps us honest and is cheap.
        entry = self._catalog.by_name(state.name)
        if entry is None:
            async with self._lock:
                state.status = "failed"
                state.error = "manifest entry vanished"
                state.finished_at = time.time()
            return

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
        except (_catalog.CatalogError, Exception) as exc:
            # Cancel-vs-IO-error race: if the cancel flag fired
            # between chunks but urllib raised before the chunk
            # boundary's cancel check, treat as cancellation
            # rather than failure.
            if cancel_event.is_set():
                final_status = "cancelled"
                error = None
            else:
                final_status = "failed"
                error = (
                    str(exc)
                    if isinstance(exc, _catalog.CatalogError)
                    else f"{type(exc).__name__}: {exc}"
                )

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
        return DEFAULT_MAX_PARALLEL
