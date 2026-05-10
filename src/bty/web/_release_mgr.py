"""bty-web release-fetch manager.

Asyncio-supervised worker pool that runs
:func:`bty.web._releases.fetch_release` in the background so the
operator can:

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

Lifecycle plumbing lives in :class:`bty.web._jobs._BaseAsyncManager`.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bty.web import _db, _releases
from bty.web._events_log import record as _log_event
from bty.web._jobs import ENQUEUE_DEDUP_STATES, _BaseAsyncManager

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

    tag: str  # the tag the operator requested ("latest" or e.g. "v1.2.3") -- the job key
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


class ReleaseFetchManager(_BaseAsyncManager[ReleaseFetchState]):
    """Async worker-pool scheduler for release-fetch jobs.

    ``start(boot_root)`` spawns workers, ``enqueue(tag)`` queues a
    job (idempotent on already-queued / running / completed),
    ``cancel(tag)`` flips the per-job event, ``stop()`` drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or DEFAULT_MAX_PARALLEL)
        self._boot_root: Path | None = None
        self._state_path: Path | None = None

    def start(self, boot_root: Path, state_path: Path | None = None) -> None:
        """Spawn the worker pool. ``state_path`` is optional: when
        given, terminal status transitions (completed / failed)
        log a ``boot.release.fetched`` event to the audit table
        so async fetches surface in /ui/events alongside the
        synchronous /ui/boot/fetch-release path. Tests omit it."""
        self._boot_root = boot_root
        self._state_path = state_path
        self._spawn_workers()

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
            if existing is not None and existing.status in ENQUEUE_DEDUP_STATES:
                return existing
            state = ReleaseFetchState(tag=tag)
            self._states[tag] = state
            await self._queue.put(tag)
            return state

    async def _run_one(self, state: ReleaseFetchState) -> None:
        """Run one fetch in a worker thread, snapshot the result
        back into ``state``."""
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
            # Cancel-vs-IO-error race: if the cancel flag fired
            # while urllib happened to be mid-syscall, the worker
            # raises ``URLError`` (wrapped as ``FetchError``)
            # before the next chunk-boundary cancel check gets a
            # chance to translate it into ``FetchCancelled``.
            # Treat that as cancellation, not failure.
            if cancel_event.is_set():
                final_status = "cancelled"
                error = None
            else:
                final_status = "failed"
                error = (
                    str(exc)
                    if isinstance(exc, _releases.FetchError)
                    else f"{type(exc).__name__}: {exc}"
                )
            base_url = None

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error
            if base_url is not None:
                state.base_url = base_url

        # Log terminal outcomes so the audit trail is symmetric:
        # successful fetches land ``boot.release.fetched``, failures
        # land ``boot.release.fetch_failed`` with the error so the
        # operator can see "this fetch tried + crashed" in
        # /ui/events without polling /boot/releases. Cancelled
        # fetches are operator-initiated and not logged.
        # ``state_path`` is optional so unit tests of the manager
        # can stay db-free.
        if self._state_path is None:
            return
        if final_status == "completed":
            with _db.open_db(self._state_path) as conn:
                _log_event(
                    conn,
                    kind="boot.release.fetched",
                    summary=f"boot release {state.tag!r} fetched from {state.base_url}",
                    subject_kind="boot",
                    subject_id=state.tag,
                    actor="system",
                    details={
                        "tag": state.tag,
                        "base_url": state.base_url,
                    },
                )
                conn.commit()
        elif final_status == "failed":
            with _db.open_db(self._state_path) as conn:
                _log_event(
                    conn,
                    kind="boot.release.fetch_failed",
                    summary=f"boot release {state.tag!r} fetch failed: {error or 'unknown error'}",
                    subject_kind="boot",
                    subject_id=state.tag,
                    actor="system",
                    details={
                        "tag": state.tag,
                        "error": error,
                    },
                )
                conn.commit()
