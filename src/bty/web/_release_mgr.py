"""bty-web release-fetch manager.

Asyncio-supervised worker pool that runs
:func:`bty.web._releases.fetch_release` in the background so the
operator can:

  * watch live progress (bytes done / total / percent) for the
    currently-running fetch via ``GET /boot/releases``,
  * cancel an in-flight fetch via ``DELETE /boot/releases/{tag}``
    -- the worker checks the cancel flag between 1 MiB chunks and
    aborts within seconds, leaving the boot dir's existing
    artifacts untouched (atomic-rename pattern in
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
from datetime import datetime
from pathlib import Path
from typing import Any

from bty.web import _db, _releases, _settings_store
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
class ReleaseArtifactState:
    """Live state of one artifact within a release-fetch job.

    A release fetch grabs the netboot trio + the sha256 manifest --
    four files total. Each file gets its own ``ReleaseArtifactState``
    so the workers UI can render per-file rows + the navbar's
    Downloads icon can count per-file (clicking "Fetch artifacts"
    increments the counter by 4, finishing one drops it to 3).
    """

    name: str  # filename, e.g. "bty-netboot-x86_64-v0.26.0.vmlinuz"
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_done: int = 0
    bytes_total: int | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class ReleaseFetchState:
    """Live state of one release-fetch job.

    Mutable on purpose -- the worker updates ``status`` /
    ``bytes_done`` / ``bytes_total`` / timestamps as the fetch
    proceeds, and the API serialises the current snapshot for
    ``GET /boot/releases``.

    ``bytes_done`` / ``bytes_total`` / ``artifact`` reflect the
    artifact CURRENTLY streaming (existing behaviour, used by
    /ui/netboot). The newer ``artifacts`` dict carries one
    :class:`ReleaseArtifactState` per file in the trio + manifest,
    populated at enqueue time so the Downloads page (``/ui/downloads``)
    can render queued rows immediately and the navbar Downloads
    counter is per-file.
    """

    tag: str  # the tag the operator requested ("latest" or e.g. "v1.2.3") -- the job key
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_done: int = 0  # cumulative bytes for the artifact currently streaming
    bytes_total: int | None = None  # Content-Length of the artifact currently streaming
    artifact: str | None = None  # filename of the artifact currently streaming
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    base_url: str | None = None  # populated on completion, for operator audit
    # Per-artifact states, keyed by filename. Empty for backfilled
    # states (events log doesn't carry per-artifact detail) and
    # populated at enqueue time for live jobs.
    artifacts: dict[str, ReleaseArtifactState] = field(default_factory=dict)
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
            "artifact": self.artifact,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "base_url": self.base_url,
            "artifacts": [a.to_dict() for a in self.artifacts.values()],
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
        log a ``netboot.artifacts.fetched`` event to the audit table
        so async fetches surface in /ui/events alongside the
        synchronous /ui/netboot/fetch-release path. Tests omit it.

        Also backfills ``_states`` from recent
        ``netboot.artifacts.fetched`` / ``netboot.artifacts.fetch_failed``
        events when ``state_path`` is given. The manager's
        ``_states`` dict is otherwise lost on restart, which made
        the /ui/netboot "Active + recent fetches" table show
        "No fetches yet." even when artifacts were clearly
        present on disk. Backfill gives the operator a durable
        history per-tag without persisting the manager's queue.
        """
        self._boot_root = boot_root
        self._state_path = state_path
        if state_path is not None:
            self._backfill_from_events(state_path)
        self._spawn_workers()

    def _backfill_from_events(self, state_path: Path) -> None:
        """Repopulate ``_states`` with recent fetch outcomes.

        Reads the latest ``netboot.artifacts.fetched`` /
        ``netboot.artifacts.fetch_failed`` rows from the events table
        (per-tag dedupe: the most recent terminal event for each
        tag wins). The reconstructed states show ``status=completed``
        or ``status=failed`` with the original ts as
        ``finished_at`` -- enough for the UI to render the
        "Last fetched" / "Error" cells. Bytes counters stay at 0;
        the live progress is gone, but the terminal outcome is
        what the operator wants to see across restarts.

        Soft-fail on any exception: a corrupt events row or a DB
        that hasn't been created yet must not crash bty-web
        startup. The backfill is a UX nicety, not a correctness
        guarantee.
        """
        # ``_events_log.list_events`` is the canonical reader.
        from bty.web import _events_log

        try:
            with _db.open_db(state_path) as conn:
                rows = _events_log.list_events(
                    conn,
                    subject_kind="netboot",
                    limit=200,
                )
        except Exception:
            return

        # Walk newest-first; first terminal event per tag wins.
        # ``rows`` is already ordered by id DESC from
        # ``list_events``.
        seen: set[str] = set()
        for ev in rows:
            if ev.kind not in ("netboot.artifacts.fetched", "netboot.artifacts.fetch_failed"):
                continue
            tag = ev.subject_id
            if not tag or tag in seen:
                continue
            seen.add(tag)
            details = ev.details or {}
            try:
                started = datetime.fromisoformat(ev.ts).timestamp() if ev.ts else None
            except (TypeError, ValueError):
                started = None
            self._states[tag] = ReleaseFetchState(
                tag=tag,
                status="completed" if ev.kind == "netboot.artifacts.fetched" else "failed",
                bytes_done=int(details.get("total_bytes") or 0),
                bytes_total=int(details.get("total_bytes") or 0) or None,
                started_at=started,
                finished_at=started,
                error=(
                    str(details.get("error"))
                    if ev.kind == "netboot.artifacts.fetch_failed"
                    else None
                ),
                base_url=(
                    details.get("base_url") if isinstance(details.get("base_url"), str) else None
                ),
            )

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
            # Pre-populate the per-artifact dict at enqueue time. Lets
            # the workers UI render all four queued rows the moment the
            # operator presses "Fetch artifacts" -- no waiting for the
            # worker to call ``on_artifact_start`` to discover the file
            # list lazily.
            for name in _releases.ALL_NAMES:
                state.artifacts[name] = ReleaseArtifactState(name=name)
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
            # Mirror onto the current per-artifact row so the workers
            # UI can render per-file progress without translating from
            # the parent state's "current artifact" field.
            cur = state.artifacts.get(state.artifact or "")
            if cur is not None:
                cur.bytes_done = done
                cur.bytes_total = total

        def _cancel() -> bool:
            return cancel_event.is_set()

        def _on_artifact_start(name: str) -> None:
            # Reset bytes counters at each new artifact so the
            # /ui/netboot live UI ticks per-file rather than carrying
            # the previous file's terminal value into the next
            # file's "0 / total" initial render.
            now = time.time()
            # Mark the previously-running artifact (if any) as
            # completed -- ``fetch_release`` only advances to the
            # next artifact once the prior one has finished
            # streaming successfully.
            prev = state.artifacts.get(state.artifact or "")
            if prev is not None and prev.status == "running":
                prev.status = "completed"
                prev.finished_at = now
                if prev.bytes_total is not None:
                    prev.bytes_done = prev.bytes_total
            state.artifact = name
            state.bytes_done = 0
            state.bytes_total = None
            cur = state.artifacts.get(name)
            if cur is None:
                # Defensive: file the worker is starting on isn't in
                # the pre-populated set (release script picked up an
                # extra artifact). Add it so the row appears in the UI.
                cur = ReleaseArtifactState(name=name)
                state.artifacts[name] = cur
            cur.status = "running"
            cur.started_at = now
            cur.bytes_done = 0
            cur.bytes_total = None

        # Resolve the release repo from the operator override (if any)
        # at fetch time, so a Settings change takes effect without a
        # restart. ``None`` lets ``fetch_release`` fall back to env /
        # default itself.
        repo: str | None = None
        if self._state_path is not None:
            with _db.open_db(self._state_path) as conn:
                repo = _settings_store.get(conn, _settings_store.KEY_RELEASE_REPO)

        try:
            result = await asyncio.to_thread(
                _releases.fetch_release,
                boot_root,
                repo=repo,
                tag=state.tag,
                progress=_progress,
                cancel=_cancel,
                on_artifact_start=_on_artifact_start,
            )
            final_status = "completed"
            error = None
            base_url = result.base_url
        except _releases.FetchCancelled:
            final_status = "cancelled"
            error = None
            base_url = None
        except Exception as exc:
            # Catch-all (``FetchError`` is a subclass): the
            # ``isinstance`` check below keeps a tidy ``FetchError``
            # message distinct from an unexpected error's typed prefix.
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
            # Propagate the terminal verdict onto the per-artifact rows.
            # On success every artifact landed -> mark each completed.
            # On failure the artifact currently streaming (if any) is
            # the one that failed; queued siblings never started so
            # they're cancelled.  On cancellation everything not yet
            # completed is cancelled.
            now_t = state.finished_at
            for a in state.artifacts.values():
                if final_status == "completed":
                    if a.status != "completed":
                        a.status = "completed"
                        a.finished_at = now_t
                        if a.bytes_total is not None:
                            a.bytes_done = a.bytes_total
                elif final_status == "cancelled":
                    if a.status not in ("completed", "failed"):
                        a.status = "cancelled"
                        a.finished_at = now_t
                else:  # failed
                    if a.status == "running":
                        a.status = "failed"
                        a.error = error
                        a.finished_at = now_t
                    elif a.status == "queued":
                        a.status = "cancelled"
                        a.finished_at = now_t
        # SSE: terminal transition outside the lock so the Netboot +
        # Downloads pages don't wait for their safety poll.
        self._fire_state_change(state)

        # Log terminal outcomes so the audit trail is symmetric:
        # successful fetches land ``netboot.artifacts.fetched``, failures
        # land ``netboot.artifacts.fetch_failed`` with the error so the
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
                    kind="netboot.artifacts.fetched",
                    summary=f"boot release {state.tag!r} fetched from {state.base_url}",
                    subject_kind="netboot",
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
                    kind="netboot.artifacts.fetch_failed",
                    summary=f"boot release {state.tag!r} fetch failed: {error or 'unknown error'}",
                    subject_kind="netboot",
                    subject_id=state.tag,
                    actor="system",
                    details={
                        "tag": state.tag,
                        "error": error,
                    },
                )
                conn.commit()
