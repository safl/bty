"""bty-web hash manager (M22 layer 5+).

Mirrors :mod:`bty.web._catalog`'s ``DownloadManager``: an asyncio-
supervised worker pool that runs SHA-256 hashing of image files in
the background so the operator can:

  * watch live progress (bytes hashed / total / percent) for every
    active hash via ``GET /catalog/hashes``,
  * cancel an in-flight hash via ``DELETE /catalog/hashes/{name}``
    -- the worker checks the cancel flag between 1 MiB chunks and
    aborts within seconds, leaving the sidecar unwritten,
  * cap parallelism so a Pi 4 / old NUC isn't saturated by N
    simultaneous hash jobs.

Default parallelism is **1**: empirically, two simultaneous SHA
runs on a small box saturate IO + CPU and both finish at half
speed; serial uses the same total wall clock without tanking
responsiveness elsewhere. Operators on fast hosts can bump via
``BTY_HASH_MAX_PARALLEL``.

Why a separate manager and not a generic JobManager: different
parallelism defaults, different state semantics (hashing has no
network failure modes; downloading has no file-not-found failure
mode), and "two managers" is a clean v1 -- premature abstraction
would lock in a shape before the second use case proves out.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bty import images as _images

# Default cap on simultaneous hashes. Tuned for small homelab
# hardware (Pi 4, old NUCs, mini-PCs); env-overridable.
DEFAULT_MAX_PARALLEL = 1


@dataclass
class HashState:
    """Live state of a single hash job.

    Mutable on purpose -- the worker updates ``status`` /
    ``bytes_hashed`` / ``bytes_total`` / timestamps as the hash
    proceeds, and the API serialises the current snapshot for
    ``GET /catalog/hashes``.
    """

    name: str  # filename (relative to image_root) -- the hash key
    path: str  # absolute path on disk
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_hashed: int = 0
    bytes_total: int = 0
    sha256: str | None = None  # populated when status="completed"
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
            "path": self.path,
            "status": self.status,
            "bytes_hashed": self.bytes_hashed,
            "bytes_total": self.bytes_total,
            "sha256": self.sha256,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class HashManager:
    """Async worker-pool scheduler for SHA-256 hash jobs.

    Lifecycle: identical to :class:`bty.web._catalog.DownloadManager`
    -- ``start(image_root)`` spawns workers, ``enqueue(filename)``
    queues a job (idempotent on already-queued / completed /
    running), ``cancel(filename)`` flips the per-job event, ``stop()``
    drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        self._max_parallel = max_parallel or _resolve_max_parallel()
        self._image_root: Path | None = None
        self._states: dict[str, HashState] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def start(self, image_root: Path) -> None:
        if self._workers:
            raise RuntimeError("HashManager already started")
        self._image_root = image_root
        self._stopping = False
        for n in range(self._max_parallel):
            self._workers.append(asyncio.create_task(self._worker(n)))

    async def stop(self) -> None:
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

    async def enqueue(self, name: str) -> HashState:
        """Queue a hash job for ``image_root / name``.

        Idempotent: returns the existing state if already
        queued / running / completed. ``cancelled`` / ``failed``
        states allow a fresh attempt.
        """
        if self._image_root is None:
            raise RuntimeError("HashManager not started")
        target = self._image_root / name
        if not target.is_file():
            raise FileNotFoundError(f"no image file at {target}")

        async with self._lock:
            existing = self._states.get(name)
            if existing is not None and existing.status in ("queued", "running", "completed"):
                return existing
            state = HashState(
                name=name,
                path=str(target),
                bytes_total=target.stat().st_size,
            )
            # If the sidecar already exists, mark complete inline
            # (ensure_sha256 would do this anyway but skipping the
            # worker round-trip means the UI sees the terminal
            # state immediately).
            cached = _images._read_sidecar_sha(target)
            if cached is not None:
                state.status = "completed"
                state.sha256 = cached
                state.bytes_hashed = state.bytes_total
                state.started_at = state.finished_at = time.time()
                self._states[name] = state
                return state
            self._states[name] = state
            await self._queue.put(name)
            return state

    async def cancel(self, name: str) -> HashState | None:
        async with self._lock:
            state = self._states.get(name)
            if state is None:
                return None
            if state.status not in ("queued", "running"):
                return state
            state._cancel.set()
            if state.status == "queued":
                state.status = "cancelled"
                state.finished_at = time.time()
            return state

    async def list(self) -> list[HashState]:
        async with self._lock:
            return list(self._states.values())

    async def _worker(self, _idx: int) -> None:
        assert self._image_root is not None
        while not self._stopping:
            try:
                name = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                async with self._lock:
                    state = self._states.get(name)
                    if state is None or state.status != "queued":
                        continue
                    state.status = "running"
                    state.started_at = time.time()
                target = self._image_root / name
                await self._run_hash(state, target)
            except asyncio.CancelledError:
                return

    async def _run_hash(self, state: HashState, target: Path) -> None:
        """Run a single hash in a worker thread, then snapshot the
        result back into ``state``. Same split-out-of-_worker
        pattern as ``DownloadManager._run_fetch`` -- the closures
        bind to non-Optional argument types, which mypy accepts."""
        cancel_event = state._cancel

        def _progress(hashed: int, total: int) -> None:
            state.bytes_hashed = hashed
            state.bytes_total = total

        def _cancel() -> bool:
            return cancel_event.is_set()

        try:
            sha = await asyncio.to_thread(
                _images.ensure_sha256,
                target,
                progress=_progress,
                cancel=_cancel,
            )
            final_status = "completed"
            error = None
        except _images.HashCancelled:
            final_status = "cancelled"
            error = None
            sha = None
        except (FileNotFoundError, OSError, Exception) as exc:
            final_status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            sha = None

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error
            if sha is not None:
                state.sha256 = sha


def _resolve_max_parallel() -> int:
    raw = os.environ.get("BTY_HASH_MAX_PARALLEL")
    if raw is None:
        return DEFAULT_MAX_PARALLEL
    try:
        n = int(raw)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        return DEFAULT_MAX_PARALLEL
