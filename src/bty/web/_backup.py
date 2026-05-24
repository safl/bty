"""Scheduled + on-demand backup of the operator-owned state.

A backup is exactly what :func:`bty.web._portability.export_bundle`
produces -- the operator's machines, catalog, and local image files
laid out as a directory under :data:`backups_root`. The manager wires
that primitive into the same per-key worker-pool model
:class:`_BaseAsyncManager` uses for downloads + hashes + release
fetches, so the worker indicator + workers page treat backups as
just another job kind.

Two entry points:

* **Manual** -- the Backup tab's "Back up now" button calls
  :meth:`enqueue` with ``trigger="manual"``. The scheduler's cadence
  is unaffected; manual runs are not recorded as the scheduler's
  ``last_run_at``.

* **Scheduled** -- the lifespan-task scheduler in :mod:`_app` calls
  :meth:`enqueue` with ``trigger="scheduled"`` when the configured
  cadence is due. Successful scheduled runs update
  ``backup.last_run_at`` in the settings store so cadence anchors on
  the most recent successful scheduled run.

Retention: after every successful run, the oldest directories under
``backups_root`` beyond :data:`backup.retention_count` are deleted.
The retention setting reads at run time so a Settings change
reflects on the next backup without restart.

History: backup outcomes land in the audit log as ``backup.created``
/ ``backup.failed`` / ``backup.pruned`` events. The manager itself
does NOT backfill its in-memory state from those events on restart
-- the workers page only renders queued + running jobs (terminal
states evict immediately from the UI), with history visible via the
events log and the on-disk ``backups/`` directory.

Cancel semantics: queued backups cancel cleanly (the job never
runs). Running backups cannot be aborted mid-export today --
``shutil.copy2`` inside :func:`export_bundle` doesn't honour a
cancel callback. A running cancel flips the cancel event but the
backup completes; the resulting directory is left in place.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bty.web import _db, _portability, _settings_store
from bty.web._events_log import record as _log_event
from bty.web._jobs import _BaseAsyncManager

log = logging.getLogger(__name__)

DEFAULT_MAX_PARALLEL = 1  # one backup at a time; concurrent exports would race on dest dirs

# Backup-id format: ISO-8601 with ``:`` -> ``-`` so the slug is a
# safe directory name on every filesystem the live env touches
# (exFAT in particular rejects ``:`` in filenames).
_BACKUP_ID_FMT = "%Y-%m-%dT%H-%M-%SZ"


@dataclass
class BackupState:
    """Live state of a single backup job.

    Mutable on purpose -- the worker updates ``status`` /
    ``finished_at`` / ``dest_path`` / counts as the export proceeds.
    """

    backup_id: str  # ISO-8601 slug; the key in :class:`BackupManager._states`
    status: str = "queued"  # queued | running | completed | cancelled | failed
    started_at: float | None = None
    finished_at: float | None = None
    machines: int = 0
    catalog_entries: int = 0
    images: int = 0
    bytes_written: int = 0
    dest_path: str | None = None  # absolute path of the bundle directory
    trigger: str = "manual"  # "manual" | "scheduled"
    error: str | None = None
    # See :class:`HashState._cancel` for the threading.Event choice.
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "machines": self.machines,
            "catalog_entries": self.catalog_entries,
            "images": self.images,
            "bytes_written": self.bytes_written,
            "dest_path": self.dest_path,
            "trigger": self.trigger,
            "error": self.error,
        }


class BackupManager(_BaseAsyncManager[BackupState]):
    """Async worker for backup jobs.

    ``start(state_path, image_root, backups_root, bty_version)`` spawns
    the worker pool, ``enqueue(trigger)`` queues a job (always queues
    a NEW row -- backups are never idempotent), ``cancel(backup_id)``
    flips the per-job cancel event.

    Unlike the other three managers there is no ``_backfill_from_events``
    here on purpose: the new workers UI renders queued + running only,
    and the events log + ``backups/`` directory are the durable history.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or _resolve_max_parallel())
        self._state_path: Path | None = None
        self._image_root: Path | None = None
        self._backups_root: Path | None = None
        self._bty_version: str | None = None

    def start(
        self,
        state_path: Path,
        image_root: Path,
        backups_root: Path,
        bty_version: str,
    ) -> None:
        """Spawn the worker pool. All four binds are required: the
        export writes to ``backups_root / <id>`` and reads operator-
        owned state from ``state_path`` + image files from
        ``image_root``; ``bty_version`` lands in the bundle's
        manifest so an operator can see which release produced it."""
        self._state_path = state_path
        self._image_root = image_root
        self._backups_root = backups_root
        self._bty_version = bty_version
        backups_root.mkdir(parents=True, exist_ok=True)
        self._spawn_workers()

    async def enqueue(self, trigger: str = "manual") -> BackupState:
        """Queue a new backup. Always creates a fresh row -- two backups
        in the same second get a numeric suffix on the id so they don't
        collide on disk."""
        if self._state_path is None:
            raise RuntimeError("BackupManager not started")
        if trigger not in ("manual", "scheduled"):
            raise ValueError(f"unknown trigger {trigger!r}")
        base = datetime.now(UTC).strftime(_BACKUP_ID_FMT)
        async with self._lock:
            backup_id = base
            n = 1
            while backup_id in self._states:
                backup_id = f"{base}-{n}"
                n += 1
            state = BackupState(backup_id=backup_id, trigger=trigger)
            self._states[backup_id] = state
            await self._queue.put(backup_id)
            return state

    async def _run_one(self, state: BackupState) -> None:
        """Run a single backup in a worker thread, write the terminal
        outcome back into ``state``, prune old backups + log events."""
        assert self._state_path is not None
        assert self._image_root is not None
        assert self._backups_root is not None
        assert self._bty_version is not None
        state_path = self._state_path
        image_root = self._image_root
        backups_root = self._backups_root
        bty_version = self._bty_version
        backup_id = state.backup_id
        dest = backups_root / backup_id
        state.dest_path = str(dest)

        now_iso = datetime.now(UTC).isoformat()
        try:
            summary = await asyncio.to_thread(
                _portability.export_bundle,
                state_path,
                image_root,
                dest,
                bty_version=bty_version,
                now=now_iso,
            )
            final_status = "completed"
            error: str | None = None
            machines = summary.machines
            catalog_entries = summary.catalog_entries
            images = summary.images
            bytes_written = _dir_size(dest)
        except Exception as exc:
            log.exception("backup %s failed", backup_id)
            final_status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            machines = 0
            catalog_entries = 0
            images = 0
            bytes_written = 0
            # Best-effort cleanup of a partial bundle; never let cleanup
            # masking the original error.
            with _suppress_oserror():
                if dest.is_dir():
                    shutil.rmtree(dest)

        # Stash export totals onto the state, but DON'T flip the
        # operator-visible status to a terminal value yet -- the job
        # isn't truly done until retention has been enforced.  Flipping
        # status here would also race the test's poll-for-completed:
        # an external observer waiting on ``status=="completed"`` would
        # exit before _prune_old_backups finishes, see a half-pruned
        # ``backups_root``, and miss the expected post-prune steady state.
        async with self._lock:
            state.error = error
            state.machines = machines
            state.catalog_entries = catalog_entries
            state.images = images
            state.bytes_written = bytes_written

        # Log + prune happen outside the lock so they don't block other
        # operations against the manager. Both run before the status flip
        # below so any observer that sees ``completed`` also sees the
        # pruned filesystem.
        if final_status == "completed":
            _log_terminal(
                state_path,
                kind="backup.created",
                state=state,
                summary_text=(
                    f"backup {backup_id!r} created ({machines} machines, "
                    f"{catalog_entries} catalog entries, {images} images, "
                    f"{bytes_written} bytes)"
                ),
            )
            # Update last_run_at only for scheduler-triggered backups;
            # manual runs don't shift the cadence anchor.
            if state.trigger == "scheduled":
                with _db.open_db(state_path) as conn:
                    _settings_store.set_backup_last_run_at(conn, now_iso)
                    conn.commit()
            await asyncio.to_thread(self._prune_old_backups)
        else:
            _log_terminal(
                state_path,
                kind="backup.failed",
                state=state,
                summary_text=f"backup {backup_id!r} failed: {error or 'unknown error'}",
            )

        # Now the job is fully done: terminal status visible to observers.
        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()

    def _prune_old_backups(self) -> None:
        """Delete oldest siblings under :data:`backups_root` to satisfy
        the retention setting. Reads the setting on every call so a
        Settings change reflects on the next backup without restart."""
        assert self._state_path is not None
        assert self._backups_root is not None
        with _db.open_db(self._state_path) as conn:
            keep = _settings_store.resolve_backup_retention(conn)
        # Only consider directories whose name matches the backup-id
        # format -- the operator may drop unrelated files (notes,
        # checksums) into ``backups_root`` and we should not touch them.
        candidates = sorted(
            (
                p
                for p in self._backups_root.iterdir()
                if p.is_dir() and _looks_like_backup_id(p.name)
            ),
            key=lambda p: p.name,  # ISO-8601 slug sorts chronologically
        )
        excess = candidates[:-keep] if keep > 0 else []
        for victim in excess:
            try:
                shutil.rmtree(victim)
            except OSError as exc:
                log.warning("could not prune backup %s: %s", victim, exc)
                continue
            with _db.open_db(self._state_path) as conn:
                _log_event(
                    conn,
                    kind="backup.pruned",
                    summary=f"pruned old backup {victim.name!r} (retention={keep})",
                    subject_kind="backup",
                    subject_id=victim.name,
                    actor="system",
                    details={"backup_id": victim.name, "retention": keep},
                )
                conn.commit()


# ----- helpers ----------------------------------------------------------


def _looks_like_backup_id(name: str) -> bool:
    """Is ``name`` a directory name this manager would create? Used by
    :meth:`_prune_old_backups` to avoid touching operator-dropped
    siblings under ``backups_root``."""
    if not name or len(name) < len("YYYY-MM-DDTHH-MM-SSZ"):
        return False
    head = name[: len("YYYY-MM-DDTHH-MM-SSZ")]
    try:
        datetime.strptime(head, _BACKUP_ID_FMT)
    except ValueError:
        return False
    return True


def _dir_size(path: Path) -> int:
    """Sum of file sizes under ``path``. Best-effort: a stat failure on
    one file falls through; the result is informational (operators see
    "Backup is ~3 GiB" in the UI), not load-bearing."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _log_terminal(
    state_path: Path,
    *,
    kind: str,
    state: BackupState,
    summary_text: str,
) -> None:
    """Single audit-log entry for a backup's terminal outcome."""
    with _db.open_db(state_path) as conn:
        _log_event(
            conn,
            kind=kind,
            summary=summary_text,
            subject_kind="backup",
            subject_id=state.backup_id,
            actor="system",
            details={
                "backup_id": state.backup_id,
                "trigger": state.trigger,
                "dest_path": state.dest_path,
                "machines": state.machines,
                "catalog_entries": state.catalog_entries,
                "images": state.images,
                "bytes_written": state.bytes_written,
                "error": state.error,
            },
        )
        conn.commit()


# ----- scheduler loop ----------------------------------------------------
#
# Long-running asyncio task that ticks every ``tick_interval`` seconds
# and enqueues a ``trigger="scheduled"`` backup when the configured
# cadence is due. Reads cadence + last_run_at + enabled on EVERY tick
# so a Settings change reflects within one tick window without
# bty-web restart.


_CADENCE_SECONDS: dict[str, float] = {
    "daily": 24 * 3600.0,
    "weekly": 7 * 24 * 3600.0,
}


def _is_due(last_iso: str | None, cadence: str, now: datetime) -> bool:
    """Decide whether a scheduled backup should fire.

    ``last_iso`` is the ISO-8601 timestamp from ``backup.last_run_at``
    (``None`` if no scheduled run has ever succeeded). ``cadence`` is
    one of :data:`BACKUP_CADENCES`. ``now`` is the current time;
    factored as an argument so tests can drive deterministic clocks.

    Returns ``True`` if a fresh scheduled run is due:

    * ``manual`` cadence -> never due (only "Back up now" runs).
    * No prior run -> due immediately (lets the operator confirm the
      schedule works without waiting a full cadence interval).
    * Otherwise -> due iff ``now - last_iso`` >= cadence interval.
    """
    if cadence not in _CADENCE_SECONDS:
        return False
    if last_iso is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    elapsed = (now - last_dt).total_seconds()
    return elapsed >= _CADENCE_SECONDS[cadence]


async def _scheduler_tick(state_path: Path, manager: BackupManager) -> None:
    """One scheduler iteration: read settings, decide if a scheduled
    backup is due, enqueue if so. Skips when a scheduled backup is
    already queued or running so a long-running backup doesn't get
    re-enqueued every tick."""
    with _db.open_db(state_path) as conn:
        enabled = _settings_store.resolve_backup_enabled(conn)
        cadence = _settings_store.resolve_backup_cadence(conn)
        last = _settings_store.get_backup_last_run_at(conn)
    if not enabled:
        return
    if not _is_due(last, cadence, datetime.now(UTC)):
        return
    # Don't pile on if a scheduled backup is already in flight.
    active = await manager.list()
    if any(s.trigger == "scheduled" and s.status in ("queued", "running") for s in active):
        return
    await manager.enqueue(trigger="scheduled")


async def scheduler_loop(
    state_path: Path,
    manager: BackupManager,
    stop_event: asyncio.Event,
    *,
    tick_interval: float = 60.0,
) -> None:
    """Run :func:`_scheduler_tick` every ``tick_interval`` seconds
    until ``stop_event`` is set. Exceptions in a tick are caught +
    logged so a transient DB error doesn't kill the scheduler.

    Shutdown wakes immediately on ``stop_event`` rather than waiting
    out the current sleep -- ``asyncio.wait_for(stop_event.wait(),
    timeout=tick_interval)`` returns the moment stop is signalled,
    times out (and loops) when the interval elapses normally.
    """
    while not stop_event.is_set():
        try:
            await _scheduler_tick(state_path, manager)
        except Exception:
            log.exception("backup scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_interval)
            return
        except TimeoutError:
            continue


def _resolve_max_parallel() -> int:
    """Read ``BTY_BACKUP_MAX_PARALLEL`` env override; default 1.
    Mirrors the pattern in :mod:`_hash` + :mod:`_release_mgr`."""
    raw = os.environ.get("BTY_BACKUP_MAX_PARALLEL")
    if raw is None:
        return DEFAULT_MAX_PARALLEL
    try:
        n = int(raw)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        return DEFAULT_MAX_PARALLEL


class _suppress_oserror:
    """Context manager: swallow ``OSError`` and log at warning level.
    Used around best-effort filesystem cleanup so the original
    exception isn't masked by a cleanup failure."""

    def __enter__(self) -> _suppress_oserror:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> bool:
        if exc_type is None:
            return False
        if not issubclass(exc_type, OSError):
            return False
        log.warning("cleanup OSError suppressed: %s", exc)
        return True
