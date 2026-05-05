"""Bearer-token auth dependency for bty-web.

Single-tenant model: bty-web runs as one Linux service user (typically
``bty``); the only credential gate is *that user's OS password*,
verified via PAM at ``POST /auth/login``. A successful login issues a
short-lived opaque bearer token whose sha256 hash lives in the
``sessions`` table; subsequent requests present the token via
``Authorization: Bearer ...`` (API) or the ``bty-token`` cookie (UI).

Failure modes return 401 with ``WWW-Authenticate: Bearer`` so any
HTTP client can prompt for a re-login.
"""

from __future__ import annotations

import logging as log
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bty.web import _db

# Cookie name used by the browser UI. Set by ``POST /ui/login`` and
# read by the bearer dependency below; an attacker stealing this cookie
# has full API access (same blast radius as a leaked Bearer token), so
# the cookie is HttpOnly + SameSite=Strict + Secure (in production).
SESSION_COOKIE = "bty-token"

_bearer_scheme = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]
_SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def make_token_dep(state_path: Path) -> Callable[..., None]:
    """Return a FastAPI dependency that enforces a valid session token.

    Accepts the token via either an ``Authorization: Bearer ...`` header
    (used by API clients and PXE-flow scripts) or the ``bty-token``
    cookie (set by the browser UI's ``/ui/login`` form). 401 if the
    presented token is missing, malformed, or doesn't match an active
    row in the ``sessions`` table.
    """

    def check_token(
        request: Request,
        credentials: _BearerCredentials,
        cookie_token: _SessionCookie = None,
    ) -> None:
        token = _extract_token(credentials, cookie_token)
        if token is None:
            log.info("auth.miss reason=missing route=%s %s", request.method, request.url.path)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        with _db.open_db(state_path) as conn:
            ok = _db.find_active_session(conn, token)
        if not ok:
            log.info("auth.miss reason=invalid route=%s %s", request.method, request.url.path)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or expired bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        log.debug("auth.ok route=%s %s", request.method, request.url.path)

    return check_token


def _extract_token(
    credentials: HTTPAuthorizationCredentials | None,
    cookie_token: str | None,
) -> str | None:
    """Pick the Authorization header if present, else the cookie."""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    if cookie_token:
        return cookie_token
    return None


def authenticate_session(state_path: Path, token: str) -> bool:
    """Synchronous DB lookup for non-dependency call sites (e.g. UI)."""
    with _db.open_db(state_path) as conn:
        return _db.find_active_session(conn, token)
