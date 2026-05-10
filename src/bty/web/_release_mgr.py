"""bty-web release-fetch manager.

Mirrors :class:`bty.web._hash.HashManager` and
:class:`bty.web._catalog.DownloadManager`: an asyncio-supervised
worker pool that runs :func:`bty.web._releases.fetch_release` in
the background so the operator can:

  * watch live progress (bytes done / total / percent) for the
    currently-running fetch via ``GET /boot/releases``,
  * cancel an in-flight fetch via ``DELETE /boot/releases/{tag}``
    -- the worker checks the cancel flag between 1 MiB chunks and
    aborts within seconds, leaving the boot dir's existing
    artefacts untouched (atomic-rename pattern in
    :func:`bty.web._releases.fetch_release` only commits after
    the manifest has verified),
  * see the resulting state without the browser having to hold
    a long-running connection open.

Default parallelism is **1**: fetching two GitHub releases in
parallel is operator-confusing (which one wins on rename?),
saturates link bandwidth, and the use case is "I want this one
release in BTY_BOOT_DIR" rather than "I want to fan-out N tags".
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bty.web import _releases

# Default cap on simultaneous release fetches. Tuned for "one
# release at a time" semantics; bumping is unusual.
DEFAULT_MAX_PARALLEL = 1

# Tag shape mirrors :class:`bty.web._models.ReleaseFetchRequest.tag`'s
# Pydantic regex. Manager-side enforcement catches direct
# ``manager.enqueue("../etc/passwd")`` / etc. from non-API call
# sites (tests, future internal callers) so a malformed tag can
# never reach the GitHub URL builder.
_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class ReleaseFetchState:
    """Live state of one release-fetch job.

    Mutable on purpose -- the worker updates ``status`` /
    ``bytes_done`` / ``bytes_total`` / timestamps as the fetch
    proceeds, and the API serialises the current snapshot for
    ``GET /boot/releases``.
    """

    tag: str  # the tag the operator requested ("latest", "v0.7.16", ...) -- the job key
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_done: int = 0  # cumulative bytes for the artefact currently streaming
    bytes_total: int | None = None  # Content-Length of the artefact currently streaming
    artefact: str | None = None  # filename of the artefact currently streaming
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    base_url: str | None = None  # populated on completion, for operator audit
    # Threading.Event because the actual IO happens in a worker
    # thread (via ``asyncio.to_thread``); ``asyncio.Event`` is not
    # thread-safe to query from inside the thread.
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # Build manually rather than via ``dataclasses.asdict`` --
        # that helper deep-copies every field, and threading.Event
        # contains a ``_thread.lock`` which cannot be pickled.
        return {
            "tag": self.tag,
            "status": self.status,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "artefact": self.artefact,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "base_url": self.base_url,
        }


class ReleaseFetchManager:
    """Async worker-pool scheduler for release-fetch jobs.

    Lifecycle: identical to ``HashManager``. ``start(boot_root)``
    spawns workers, ``enqueue(tag)`` queues a job (idempotent on
    already-queued / running / completed), ``cancel(tag)`` flips
    the per-job event, ``stop()`` drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        self._max_parallel = max_parallel or DEFAULT_MAX_PARALLEL
        self._boot_root: Path | None = None
        self._states: dict[str, ReleaseFetchState] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def start(self, boot_root: Path) -> None:
        if self._workers:
            raise RuntimeError("ReleaseFetchManager already started")
        self._boot_root = boot_root
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
            with contextlib.suppress(asyncio.CancelledError):
                await w
        self._workers.clear()

    async def enqueue(self, tag: str) -> ReleaseFetchState:
        """Queue a release fetch for ``tag``.

        Idempotent: returns the existing state if already
        queued / running / completed. ``cancelled`` / ``failed``
        states allow a fresh attempt.

        Raises :class:`ValueError` if ``tag`` is not a plausible
        GitHub release tag (alnum + ``.`` ``_`` ``-``). The HTTP
        layer's Pydantic model already rejects this shape; the
        check here protects non-API callers (tests, future
        internal use) from slipping a slash through to the URL
        builder.
        """
        if not _TAG_RE.match(tag):
            raise ValueError(f"invalid release tag {tag!r}: must match [A-Za-z0-9._-]+")
        if self._boot_root is None:
            raise RuntimeError("ReleaseFetchManager not started")
        async with self._lock:
            existing = self._states.get(tag)
            if existing is not None and existing.status in ("queued", "running", "completed"):
                return existing
            state = ReleaseFetchState(tag=tag)
            self._states[tag] = state
            await self._queue.put(tag)
            return state

    async def cancel(self, tag: str) -> ReleaseFetchState | None:
        async with self._lock:
            state = self._states.get(tag)
            if state is None:
                return None
            if state.status not in ("queued", "running"):
                return state
            state._cancel.set()
            if state.status == "queued":
                state.status = "cancelled"
                state.finished_at = time.time()
            return state

    async def list(self) -> list[ReleaseFetchState]:
        async with self._lock:
            return list(self._states.values())

    async def _worker(self, _idx: int) -> None:
        assert self._boot_root is not None
        while not self._stopping:
            try:
                tag = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                async with self._lock:
                    state = self._states.get(tag)
                    if state is None or state.status != "queued":
                        continue
                    state.status = "running"
                    state.started_at = time.time()
                await self._run_fetch(state)
            except asyncio.CancelledError:
                return

    async def _run_fetch(self, state: ReleaseFetchState) -> None:
        """Run one fetch in a worker thread, snapshotting the
        result back into ``state``. Same split-out pattern as
        ``HashManager._run_hash``."""
        assert self._boot_root is not None
        cancel_event = state._cancel
        boot_root = self._boot_root

        def _progress(done: int, total: int | None) -> None:
            state.bytes_done = done
            state.bytes_total = total

        def _cancel() -> bool:
            return cancel_event.is_set()

        try:
            result = await asyncio.to_thread(
                _releases.fetch_release,
                boot_root,
                tag=state.tag,
                progress=_progress,
                cancel=_cancel,
            )
            final_status = "completed"
            error = None
            base_url = result.base_url
        except _releases.FetchCancelled:
            final_status = "cancelled"
            error = None
            base_url = None
        except (_releases.FetchError, Exception) as exc:
            # If the cancel flag fired while urllib happened to be
            # mid-syscall, the worker raises ``URLError`` (wrapped
            # as ``FetchError``) before the next chunk-boundary
            # cancel check gets a chance to translate it into
            # ``FetchCancelled``. Treat that as cancellation, not
            # failure -- the operator's intent was "stop", and
            # showing a "failed: connection reset" badge for a
            # user-initiated cancel is misleading.
            if cancel_event.is_set():
                final_status = "cancelled"
                error = None
            else:
                final_status = "failed"
                error = (
                    str(exc)
                    if isinstance(exc, _releases.FetchError)
                    else (f"{type(exc).__name__}: {exc}")
                )
            base_url = None

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error
            if base_url is not None:
                state.base_url = base_url
