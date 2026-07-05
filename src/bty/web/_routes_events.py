"""Route registration for the audit event log.

Registers ``GET /events`` (filterable listing) and
``POST /events/{event_id}/ack`` (mark one row acknowledged, clears
it from the dashboard tripwire without deleting the audit row).

The audit event data model lives in :mod:`bty.web._events_log`;
this module is the HTTP surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from bty.web import _db
from bty.web._auth import require_auth
from bty.web._events_log import acknowledge_event as _acknowledge_event
from bty.web._events_log import list_events as _list_events


def register_event_routes(app: FastAPI, *, state_path: Path) -> None:
    """Attach the audit-event routes to ``app``."""

    @app.get("/events", dependencies=[Depends(require_auth)])
    def list_events_endpoint(
        kind: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        source_ip: str | None = None,
        failed: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        with _db.open_db(state_path) as conn:
            events = _list_events(
                conn,
                kind=kind,
                subject_kind=subject_kind,
                subject_id=subject_id,
                actor=actor,
                source_ip=source_ip,
                failed_only=bool(failed),
                before_id=before_id,
                limit=limit,
            )
        return {"events": [e.to_dict() for e in events]}

    @app.post("/events/{event_id}/ack", dependencies=[Depends(require_auth)])
    def acknowledge_event_endpoint(event_id: int) -> dict[str, Any]:
        """Mark one event acknowledged. Clears it from the dashboard
        Health Monitoring tripwire (which counts only unacknowledged
        failures) without deleting the audit row. 404 if no such id."""
        with _db.open_db(state_path) as conn:
            changed = _acknowledge_event(conn, event_id)
            conn.commit()
        if not changed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no event with id {event_id}",
            )
        return {"id": event_id, "acknowledged": True}
