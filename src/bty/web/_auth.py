"""Cookie-based session auth dependency for bty-web.

Single-tenant model: bty-web runs as one Linux service user (typically
``bty``); the only credential gate is *that user's OS password*,
verified via PAM at ``POST /ui/login`` (browser flow). A successful
login issues an opaque session token whose sha256 hash lives in the
``sessions`` table; subsequent requests present the token via the
``bty-token`` cookie. Mutating routes use this module's
``make_token_dep`` to enforce a valid cookie.

Failure modes return 401; browsers redirect to ``/ui/login`` via the
exception-handler middleware in ``_ui.py``.
"""

from __future__ import annotations

import logging as log
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, HTTPException, Request, status

from bty.web import _db

# Cookie name used by the browser UI. Set by ``POST /ui/login`` and
# read by the auth dependency below.
SESSION_COOKIE = "bty-token"

_SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def make_token_dep(state_path: Path) -> Callable[..., None]:
    """Return a FastAPI dependency that enforces a valid session cookie.

    401 if the cookie is missing or doesn't match an active row in the
    ``sessions`` table.
    """

    def check_token(
        request: Request,
        cookie_token: _SessionCookie = None,
    ) -> None:
        if not cookie_token:
            log.info("auth.miss reason=missing route=%s %s", request.method, request.url.path)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing session cookie",
            )
        with _db.open_db(state_path) as conn:
            ok = _db.find_active_session(conn, cookie_token)
        if not ok:
            log.info("auth.miss reason=invalid route=%s %s", request.method, request.url.path)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or expired session cookie",
            )
        log.debug("auth.ok route=%s %s", request.method, request.url.path)

    return check_token


def authenticate_session(state_path: Path, token: str) -> bool:
    """Synchronous DB lookup for non-dependency call sites (e.g. UI)."""
    with _db.open_db(state_path) as conn:
        return _db.find_active_session(conn, token)
