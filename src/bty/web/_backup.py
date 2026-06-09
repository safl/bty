"""Scheduled + on-demand backup of the operator-owned state.

A backup is exactly what :func:`bty.web._portability.export_bundle`
produces -- v0.33.2+: a metadata-only bundle (just
``inventory.json``) carrying the per-machine hardware identity
(mac + lshw + known_disks). No image bytes; image files live in
``BTY_IMAGE_ROOT`` and are either still on disk or re-fetchable
from the catalog. The bundle lands as a directory under
:data:`backups_root`. The manager wires this primitive into the
same per-key worker-pool model :class:`_BaseAsyncManager` uses for
downloads + hashes + release fetches, so the worker indicator +
the Backups page (``/ui/backups``) treat backups as just another
job kind.

Two entry points:

* **Manual** -- the "Back up now" button on ``/ui/backups`` calls
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
/ ``backup.failed`` / ``backup.pruned`` / ``backup.deleted`` events.
The manager itself
does NOT backfill its in-memory state from those events on restart
-- ``/ui/backups`` only renders queued + running jobs (terminal
states evict immediately from the UI), with history visible via the
events log and the on-disk ``backups/`` directory.

Cancel semantics: queued backups cancel cleanly (the job never
runs). Running backups are effectively un-cancellable because
the metadata-only export finishes in milliseconds -- the cancel
event flips while the write is already done.
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

# Valid trigger values for :meth:`BackupManager.enqueue`. ``manual``
# is the operator-pressed "Back up now"; ``scheduled`` is the
# scheduler loop's cadence-driven enqueue. Only ``scheduled`` runs
# update the ``backup.last_run_at`` settings key (the cadence anchor).
BACKUP_TRIGGERS: tuple[str, ...] = ("manual", "scheduled")


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
    bytes_done: int = 0
    dest_path: str | None = None  # absolute path of the bundle directory
    trigger: str = "manual"  # one of :data:`BACKUP_TRIGGERS`
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
            "bytes_done": self.bytes_done,
            "dest_path": self.dest_path,
            "trigger": self.trigger,
            "error": self.error,
        }


class BackupManager(_BaseAsyncManager[BackupState]):
    """Async worker for backup jobs.

    ``start(state_path, backups_root)`` spawns the worker pool,
    ``enqueue(trigger)`` queues a job (always queues a NEW row --
    backups are never idempotent), ``cancel(backup_id)`` flips the
    per-job cancel event.

    Unlike the other three managers there is no ``_backfill_from_events``
    here on purpose: the new workers UI renders queued + running only,
    and the events log + ``backups/`` directory are the durable history.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or _resolve_max_parallel())
        self._state_path: Path | None = None
        self._backups_root: Path | None = None

    def start(
        self,
        state_path: Path,
        backups_root: Path,
    ) -> None:
        """Spawn the worker pool. The export writes a metadata-only
        bundle to ``backups_root / <id>``. The running
        ``bty.__version__`` is stamped into the bundle's manifest by
        ``export_bundle`` -- no separate plumbing needed."""
        self._state_path = state_path
        self._backups_root = backups_root
        backups_root.mkdir(parents=True, exist_ok=True)
        self._spawn_workers()

    async def enqueue(self, trigger: str = "manual") -> BackupState:
        """Queue a new backup. Always creates a fresh row -- two backups
        in the same second get a numeric suffix on the id so they don't
        collide on disk."""
        if self._state_path is None:
            raise RuntimeError("BackupManager not started")
        if trigger not in BACKUP_TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}; expected one of {BACKUP_TRIGGERS}")
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
        outcome back into ``state``, prune old backups + log events.

        Unlike ReleaseFetchManager, this method does NOT poll
        ``state._cancel`` in the worker loop. A v3 metadata-only
        export is a single ``json.dumps`` + write, finishing in
        milliseconds; there is no window where a cancel signal could
        land between "started" and "completed". The ``_cancel`` field
        still lives on the dataclass because the
        ``_BaseAsyncManager`` Protocol requires it and the queued-
        backup cancel path (job dropped before it runs) sets it.
        """
        assert self._state_path is not None
        assert self._backups_root is not None
        state_path = self._state_path
        backups_root = self._backups_root
        backup_id = state.backup_id
        dest = backups_root / backup_id
        state.dest_path = str(dest)

        # Lifecycle audit event: worker started the backup. Pairs
        # with the request-side ``backup.create.requested`` (logged
        # by either the POST /workers/backups handler or the
        # scheduler_loop's enqueue site).
        try:
            with _db.open_db(state_path) as conn:
                _log_event(
                    conn,
                    kind="backup.create.started",
                    summary=f"worker started backup {backup_id!r}",
                    subject_kind="backup",
                    subject_id=backup_id,
                    actor="system",
                    details={"backup_id": backup_id, "trigger": state.trigger},
                )
                conn.commit()
        except Exception:
            log.exception("backup.create.started event-log write failed for %s", backup_id)

        now_iso = datetime.now(UTC).isoformat()
        try:
            summary = await asyncio.to_thread(
                _portability.export_bundle,
                state_path,
                dest,
                now=now_iso,
            )
            final_status = "completed"
            error: str | None = None
            machines = summary.machines
            bytes_done = _bundle_size(dest)
        except Exception as exc:
            log.exception("backup %s failed", backup_id)
            final_status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            machines = 0
            bytes_done = 0
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
            state.bytes_done = bytes_done

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
                    f"backup {backup_id!r} created ({machines} machines, {bytes_done} bytes)"
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
        # SSE: notify subscribers of the terminal transition so the
        # Backups page picks it up without waiting for its safety poll.
        self._fire_state_change(state)

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


def _bundle_size(path: Path) -> int:
    """Size of the v3 ``inventory.json`` inside ``path``, or 0 if it's
    missing. Best-effort: a stat failure returns 0 -- the value is
    informational (operators see "Backup is 4 KiB" in the UI), not
    load-bearing.

    v3 bundles are one file, so this is a single stat rather than a
    ``os.walk`` summation. Existing on-disk v2 bundles with image
    bytes show as the size of just their inventory file (which is
    what's actually still readable on the new release)."""
    try:
        return (path / "inventory.json").stat().st_size
    except OSError:
        return 0


# ----- on-disk listing --------------------------------------------------
#
# The Backups page enumerates the directories under ``backups_root`` so
# the operator can see which bundles actually exist on disk -- the active
# / activity cards above only show in-flight + recent jobs, which is not
# enough to answer "what backups do I have right now?" after a few
# scheduled runs have come and gone from the events table.


@dataclass(frozen=True)
class BackupOnDisk:
    """A bundle the BackupManager (or an offline ``bty-web export``) wrote
    under :data:`backups_root`.

    Fields read from the bundle's ``inventory.json``; the machine
    count comes from the inventory. ``bytes_on_disk`` is the size of
    the bundle directory (just ``inventory.json`` in v3), surfaced so
    the operator can sanity-check that "backup of 12 KiB" actually
    looks like a metadata bundle.

    A bundle whose ``inventory.json`` is missing or malformed still
    appears in the list (``exported_at`` / ``bty_version`` are
    ``None``, machines is 0) so the operator can see it and clean it
    up rather than silently hiding it.
    """

    backup_id: str
    path: Path
    exported_at: str | None
    bty_version: str | None
    machines: int
    bytes_on_disk: int


def list_backups_on_disk(backups_root: Path) -> list[BackupOnDisk]:
    """Enumerate bundles under ``backups_root``, newest first.

    Only directories whose name matches the backup-id format are
    listed; the operator may drop unrelated files (notes, checksums)
    into the backups dir without confusing this view. Sort is by
    backup-id (ISO-8601 slug), reversed -- so the most recent run
    is at the top.
    """
    if not backups_root.is_dir():
        return []
    out: list[BackupOnDisk] = []
    for entry in sorted(backups_root.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir() or not _looks_like_backup_id(entry.name):
            continue
        out.append(_read_bundle(entry))
    return out


def _read_bundle(path: Path) -> BackupOnDisk:
    """Build a :class:`BackupOnDisk` from a bundle directory.

    Robust to a missing or malformed ``inventory.json`` -- pre-1.0
    strictness applies to settings the UI controls; this is a passive
    survey of operator-owned files and we want to SHOW garbage rather
    than hide it.
    """
    import json

    inventory_path = path / "inventory.json"
    exported_at: str | None = None
    bty_version: str | None = None
    machines = 0
    if inventory_path.is_file():
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            if isinstance(inventory, dict):
                ea = inventory.get("exported_at")
                bv = inventory.get("exported_by_bty_version")
                exported_at = ea if isinstance(ea, str) else None
                bty_version = bv if isinstance(bv, str) else None
                ms = inventory.get("machines")
                machines = len(ms) if isinstance(ms, list) else 0
        except (OSError, ValueError):
            # Unparseable inventory -- the bundle still lists with
            # blank metadata so the operator can find + delete it.
            pass
    return BackupOnDisk(
        backup_id=path.name,
        path=path,
        exported_at=exported_at,
        bty_version=bty_version,
        machines=machines,
        bytes_on_disk=_bundle_size(path),
    )


def is_valid_backup_id(name: str) -> bool:
    """Whether ``name`` matches the backup-id format the BackupManager
    mints.

    Public alias for the module-private :func:`_looks_like_backup_id`,
    so route handlers can validate path segments coming off the URL
    without reaching into ``_``-prefixed internals. Used as a path-
    traversal guard: a request for
    ``/ui/backups/{backup_id}/download`` will only resolve when the
    segment shape itself matches an ISO-8601 slug, so the route can
    never look at sibling directories of ``backups_root``.
    """
    return _looks_like_backup_id(name)


def delete_bundle(state_path: Path, backups_root: Path, backup_id: str) -> BackupOnDisk:
    """Remove one bundle directory from disk + log an audit event.

    Caller MUST have validated ``backup_id`` against
    :func:`is_valid_backup_id` already -- this helper trusts the
    shape and joins ``backups_root / backup_id`` without further
    sanitising. Returns the :class:`BackupOnDisk` snapshot captured
    just before the rmtree, so the event log entry + UI feedback
    can name the counts the operator just lost.

    Raises :class:`FileNotFoundError` if no such bundle exists --
    the route handler translates that to a 404.
    """
    bundle = backups_root / backup_id
    if not bundle.is_dir():
        raise FileNotFoundError(f"no such backup: {backup_id}")
    snapshot = _read_bundle(bundle)
    shutil.rmtree(bundle)
    with _db.open_db(state_path) as conn:
        _log_event(
            conn,
            kind="backup.deleted",
            summary=(
                f"backup {backup_id!r} deleted by operator "
                f"({snapshot.machines} machines, "
                f"{snapshot.bytes_on_disk} bytes)"
            ),
            subject_kind="backup",
            subject_id=backup_id,
            actor="operator",
            details={
                "backup_id": backup_id,
                "machines": snapshot.machines,
                "bytes_on_disk": snapshot.bytes_on_disk,
            },
        )
        conn.commit()
    return snapshot


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
                "bytes_done": state.bytes_done,
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
    state = await manager.enqueue(trigger="scheduled")
    # Lifecycle audit event: the scheduler is the "operator" here
    # (actor=system). The HTTP POST /workers/backups handler emits
    # the matching event for operator-driven runs; this one keeps
    # the audit log symmetric across both initiation paths.
    try:
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="backup.create.requested",
                summary=f"scheduler requested backup {state.backup_id!r}",
                subject_kind="backup",
                subject_id=state.backup_id,
                actor="system",
                details={"backup_id": state.backup_id, "trigger": "scheduled"},
            )
            conn.commit()
    except Exception:
        log.exception(
            "backup.create.requested event-log write failed for %s (scheduler tick)",
            state.backup_id,
        )


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
    """Read ``[tuning] backup_max_parallel`` (env override
    ``BTY_TUNING_BACKUP_MAX_PARALLEL``) from the active config;
    clamp non-positive values to :data:`DEFAULT_MAX_PARALLEL`."""
    from bty.web._config import cfg as _cfg

    try:
        n = _cfg().tuning.backup_max_parallel
        return n if n >= 1 else DEFAULT_MAX_PARALLEL
    except RuntimeError:
        # No active config -- direct-call test / import. Fall back
        # to the legacy env name so existing fixtures still work.
        raw = os.environ.get("BTY_TUNING_BACKUP_MAX_PARALLEL") or os.environ.get(
            "BTY_BACKUP_MAX_PARALLEL"
        )
        if raw is None:
            return DEFAULT_MAX_PARALLEL
        try:
            n = int(raw)
            return n if n >= 1 else DEFAULT_MAX_PARALLEL
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
