"""Route registration for the backup worker.

Registers ``GET /workers/backups`` + ``POST /workers/backups`` +
``DELETE /workers/backups/{backup_id}`` -- the trio driving the
Backups page + the operator-triggered backup flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status

from bty.web import _backup, _db, _models
from bty.web._auth import require_auth
from bty.web._events_log import record as _log_event
from bty.web._reqctx import client_ip as _client_ip


def register_backup_routes(
    app: FastAPI,
    *,
    backup_manager: _backup.BackupManager,
    resolved_backups_root: Path,
    state_path: Path,
) -> None:
    """Attach the backup control-plane routes to ``app``.

    Mirrors the ``/boot/releases`` shape (the only other worker-pool
    manager left after the v0.40 catalog/download + hash cleanup):
    GET lists active jobs (queued + running + recent terminal
    states); POST enqueues; DELETE cancels by backup_id.
    ``/ui/backups`` filters to queued + running only; terminal
    rows evict from the UI on completion, and history lives in
    the events log.
    """

    @app.get("/workers/backups", dependencies=[Depends(require_auth)])
    async def list_backups() -> dict[str, Any]:
        states = await backup_manager.list()
        return {
            "backups_root": str(resolved_backups_root),
            "max_parallel": backup_manager.max_parallel,
            "backups": [s.to_dict() for s in states],
        }

    @app.post(
        "/workers/backups",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def enqueue_backup(
        body: _models.BackupEnqueueRequest, request: Request
    ) -> dict[str, Any]:
        state = await backup_manager.enqueue(trigger=body.trigger)
        # Lifecycle audit event: operator-initiated backup. Scheduler-
        # driven backups go through ``_backup.scheduler_loop`` which
        # emits its own request event with actor=system; this handler
        # is operator-only (the ``trigger`` field defaults to "manual"
        # but the scheduler uses the same enqueue path with
        # "scheduled" -- so we look at body.trigger to set the actor
        # correctly even though most callers will hit "manual" here).
        is_scheduler = body.trigger == "scheduled"
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="backup.create.requested",
                summary=(
                    f"scheduler requested {state.backup_id!r}"
                    if is_scheduler
                    else f"operator requested backup {state.backup_id!r}"
                ),
                subject_kind="backup",
                subject_id=state.backup_id,
                actor="system" if is_scheduler else "operator",
                source_ip=None if is_scheduler else _client_ip(request),
                details={"backup_id": state.backup_id, "trigger": body.trigger},
            )
            conn.commit()
        return state.to_dict()

    @app.delete("/workers/backups/{backup_id}", dependencies=[Depends(require_auth)])
    async def cancel_backup(backup_id: str, request: Request) -> dict[str, Any]:
        state = await backup_manager.cancel(backup_id)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active backup for id {backup_id!r}",
            )
        # Operator-side cancel event. Unlike the catalog/release
        # workers, the backup worker does NOT emit a sibling
        # backup.create.cancelled: a running backup completes in
        # milliseconds and isn't interrupted mid-flight, so this
        # handler's event is the only cancelled record.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="backup.create.cancelled",
                summary=f"operator cancelled backup {backup_id!r}",
                subject_kind="backup",
                subject_id=backup_id,
                actor="operator",
                source_ip=_client_ip(request),
                details={"backup_id": backup_id, "source": "operator"},
            )
            conn.commit()
        return state.to_dict()
