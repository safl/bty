"""Bearer-token auth dependency for bty-web.

Single-token model: there is one ``BTY_WEB_TOKEN`` configured at
server startup. Requests to protected routes must carry
``Authorization: Bearer <token>``. Comparison is constant-time via
:func:`secrets.compare_digest`.

If the token is unset at startup, ``main()`` refuses to start the
server - it fails closed by default.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Cookie name used by the browser UI. Set by ``POST /ui/login`` and
# read by the bearer dependency below; an attacker stealing this cookie
# has full API access (same blast radius as a leaked Bearer token), so
# the cookie is HttpOnly + SameSite=Strict + Secure (in production).
SESSION_COOKIE = "bty-token"

_bearer_scheme = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]
_SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def make_token_dep(expected_token: str) -> Callable[..., None]:
    """Return a FastAPI dependency that enforces ``Bearer <expected_token>``.

    Accepts the token via either an ``Authorization: Bearer ...`` header
    (used by API clients and PXE-flow scripts) or the ``bty-token``
    cookie (set by the browser UI's ``/ui/login`` form). Raises 401 if
    neither carries the right value. ``expected_token`` is captured by
    closure so tests can wire up an isolated token without touching the
    environment.
    """
    if not expected_token:
        raise ValueError("expected_token must be a non-empty string")

    def check_token(
        credentials: _BearerCredentials,
        cookie_token: _SessionCookie = None,
    ) -> None:
        token = _extract_token(credentials, cookie_token)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

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


def token_matches(expected: str, candidate: str) -> bool:
    """Constant-time check for use outside the dependency (e.g. /ui/login)."""
    return secrets.compare_digest(expected, candidate)
