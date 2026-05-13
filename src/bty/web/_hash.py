"""bty-web hash manager.

Asyncio-supervised worker pool that runs SHA-256 hashing of image
files in the background so the operator can:

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

The lifecycle plumbing (``stop``, ``cancel``, ``list``, the
``_worker`` queue loop) lives in :class:`bty.web._jobs._BaseAsyncManager`;
this module owns the hash-specific state shape, the ``enqueue``
sidecar-cached short-circuit, and the ``_run_one`` body.
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
from bty.web import _db
from bty.web._events_log import record as _log_event
from bty.web._jobs import ENQUEUE_DEDUP_STATES, _BaseAsyncManager

# Default cap on simultaneous hashes. Tuned for small homelab
# hardware (Pi 4, old NUCs, mini-PCs); env-overridable.
DEFAULT_MAX_PARALLEL = 1


def _reject_traversal_name(name: str) -> None:
    """Reject anything that's not a plain basename.

    The FastAPI layer's ``_safe_path`` rejects these on public
    routes; mirroring the check at the manager boundary means a
    direct call from a non-API caller (auto-import, tests, future
    internal use) can never resolve outside ``image_root``.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(
            f"invalid name {name!r}: must be a basename without path separators or NUL bytes"
        )


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


class HashManager(_BaseAsyncManager[HashState]):
    """Async worker-pool scheduler for SHA-256 hash jobs.

    ``start(image_root)`` spawns workers, ``enqueue(filename)`` queues
    a job (idempotent on already-queued / completed / running),
    ``cancel(filename)`` flips the per-job event, ``stop()`` drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or _resolve_max_parallel())
        self._image_root: Path | None = None
        self._state_path: Path | None = None

    def start(self, image_root: Path, state_path: Path | None = None) -> None:
        """Spawn the worker pool. ``state_path`` is optional: when
        given, successful hash completions log an ``image.hashed``
        event to the audit table so the operator can see SHA
        availability roll forward in /ui/events. Tests omit it."""
        self._image_root = image_root
        self._state_path = state_path
        self._spawn_workers()

    async def enqueue(self, name: str) -> HashState:
        """Queue a hash job for ``image_root / name``.

        Idempotent: returns the existing state if already
        queued / running / completed. ``cancelled`` / ``failed``
        states allow a fresh attempt.

        Raises :class:`ValueError` if ``name`` carries path-
        traversal characters (``/``, ``\\``, ``..``, NUL). The
        FastAPI layer's ``_safe_path`` already rejects these on
        the public PUT route; the check here defends non-API
        callers (auto-import lifespan, tests, future internal
        use) so a malformed name can never reach the
        filesystem.
        """
        _reject_traversal_name(name)
        if self._image_root is None:
            raise RuntimeError("HashManager not started")
        target = self._image_root / name
        if not target.is_file():
            raise FileNotFoundError(f"no image file at {target}")

        async with self._lock:
            existing = self._states.get(name)
            if existing is not None and existing.status in ENQUEUE_DEDUP_STATES:
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

    async def _run_one(self, state: HashState) -> None:
        """Run a single hash in a worker thread, snapshot the result
        back into ``state``."""
        target = Path(state.path)
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
        except Exception as exc:
            # Cancel-vs-IO-error race: if the cancel flag fired
            # between chunks but the ``ensure_sha256`` worker hit
            # a transient OSError before reaching its cancel
            # check, the operator-initiated stop should not
            # surface as "failed".
            sha = None
            if cancel_event.is_set():
                final_status = "cancelled"
                error = None
            else:
                final_status = "failed"
                error = f"{type(exc).__name__}: {exc}"

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error
            if sha is not None:
                state.sha256 = sha

        # Log terminal outcomes so the audit trail is symmetric:
        # successful hashes land ``image.hashed`` with the sha;
        # failures land ``image.hash_failed`` with the error so an
        # operator scanning /ui/events can see "this file was
        # supposed to import but couldn't" without having to
        # poll /catalog/hashes. Cancelled hashes are operator-
        # initiated and not logged. ``state_path`` is optional so
        # unit tests of the manager can stay db-free.
        if self._state_path is None:
            return
        if final_status == "completed" and sha is not None:
            with _db.open_db(self._state_path) as conn:
                # Propagate the computed sha into the catalog row
                # that's already keyed by ``file://<name>``. The auto-
                # import sweep on bty-web startup inserts the row
                # with ``disk_image_sha = NULL`` so the catalog has
                # entries the operator can bind to; this UPDATE makes
                # those rows bindable in the flash flow (PXE handler
                # resolves ref -> disk_image_sha -> /images/<sha>).
                # Match by src rather than ref so that this code path
                # stays decoupled from the canonicalisation helper.
                conn.execute(
                    "UPDATE catalog_entries SET disk_image_sha = ? WHERE src = ?",
                    (sha, f"file://{state.name}"),
                )
                _log_event(
                    conn,
                    kind="image.hashed",
                    summary=f"image {state.name!r} hashed (sha256={sha[:12]}...)",
                    subject_kind="image",
                    subject_id=state.name,
                    actor="system",
                    details={
                        "name": state.name,
                        "sha256": sha,
                        "bytes": state.bytes_total,
                    },
                )
                conn.commit()
        elif final_status == "failed":
            with _db.open_db(self._state_path) as conn:
                _log_event(
                    conn,
                    kind="image.hash_failed",
                    summary=f"image {state.name!r} hash failed: {error or 'unknown error'}",
                    subject_kind="image",
                    subject_id=state.name,
                    actor="system",
                    details={
                        "name": state.name,
                        "error": error,
                    },
                )
                conn.commit()


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
