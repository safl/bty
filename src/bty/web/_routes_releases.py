"""Route registration for the release-fetch worker.

Registers ``GET /boot/releases`` + ``POST /boot/releases`` +
``DELETE /boot/releases/{tag}`` -- the trio powering the trackable
"Fetch from GitHub releases" action on ``/ui/netboot``. Routes
close over the :class:`ReleaseFetchManager` instance created in
``bty.web._app.create_app`` + the state_path used for audit-event
logging.

Kept in its own module (rather than nested inside ``create_app``)
so a future ``trio-common`` extraction can lift the pattern
without unpacking a 3000-line file's closure graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status

from bty.web import _db, _models, _release_mgr
from bty.web._auth import require_auth
from bty.web._events_log import record as _log_event
from bty.web._reqctx import client_ip as _client_ip


def register_release_routes(
    app: FastAPI,
    *,
    release_fetch_manager: _release_mgr.ReleaseFetchManager,
    resolved_boot_root: Path,
    state_path: Path,
) -> None:
    """Attach the release-fetch control-plane routes to ``app``.

    Registered BEFORE the ``GET /boot/{name}`` catch-all so
    ``/boot/releases`` doesn't get eaten as a missing artifact name.
    Powers the trackable "Fetch from GitHub releases" action on
    ``/ui/netboot``: ``POST /boot/releases`` enqueues,
    ``GET /boot/releases`` polls, ``DELETE /boot/releases/{tag}``
    cancels.
    """

    @app.get("/boot/releases", dependencies=[Depends(require_auth)])
    async def list_release_fetches() -> dict[str, Any]:
        states = await release_fetch_manager.list()
        return {
            "boot_root": str(resolved_boot_root),
            "max_parallel": release_fetch_manager.max_parallel,
            "fetches": [s.to_dict() for s in states],
        }

    @app.post(
        "/boot/releases",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def enqueue_release_fetch(
        body: _models.ReleaseFetchRequest, request: Request
    ) -> dict[str, Any]:
        state = await release_fetch_manager.enqueue(body.tag)
        # Lifecycle audit event: operator-initiated release fetch.
        # Pairs with the worker-side ``netboot.artifacts.fetch.started``
        # and the eventual terminal event.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="netboot.artifacts.fetch.requested",
                summary=f"operator requested release fetch for tag {body.tag!r}",
                subject_kind="netboot",
                subject_id=body.tag,
                actor="operator",
                source_ip=_client_ip(request),
                details={"tag": body.tag},
            )
            conn.commit()
        return state.to_dict()

    @app.delete("/boot/releases/{tag}", dependencies=[Depends(require_auth)])
    async def cancel_release_fetch(tag: str, request: Request) -> dict[str, Any]:
        try:
            state = await release_fetch_manager.cancel(tag)
        except ValueError as exc:
            # Symmetric with enqueue: a malformed tag (path traversal,
            # whitespace, escaped slashes) hits the same _TAG_RE guard
            # and surfaces as 422 here rather than the previous 404
            # plus an audit-log row carrying the attacker-controlled
            # text in subject_id.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active release fetch for tag {tag!r}",
            )
        # Operator-side cancel event. The worker writes its own
        # ``netboot.artifacts.fetch.cancelled`` when it observes the
        # flag; this row carries source_ip + actor=operator so the
        # /ui/events filter on actor=operator picks up the intent.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="netboot.artifacts.fetch.cancelled",
                summary=f"operator cancelled release fetch for tag {tag!r}",
                subject_kind="netboot",
                subject_id=tag,
                actor="operator",
                source_ip=_client_ip(request),
                details={"tag": tag, "source": "operator"},
            )
            conn.commit()
        return state.to_dict()
