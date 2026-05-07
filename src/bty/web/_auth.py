"""Session-cookie auth dependency for bty-web.

Single-tenant model: bty-web runs as one Linux service user (typically
``bty``); the only credential gate is *that user's OS password*,
verified via PAM at ``POST /ui/login``. A successful login flips
``request.session["bty_authed"] = True``; the session is a server-
signed cookie managed by Starlette's :class:`SessionMiddleware`, so
no DB session table is needed.

Failure modes return 401; ``/ui/*`` routes catch the exception in a
middleware and redirect to ``/ui/login``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

# Session-cookie name. Set explicitly so the existing PXE chain test
# (which captures Set-Cookie from /ui/login) and any operator scripts
# don't break across the SessionMiddleware swap.
SESSION_COOKIE = "bty-token"

# Session key the auth dep checks. Set on successful /ui/login.
SESSION_AUTHED_KEY = "bty_authed"


def require_auth(request: Request) -> None:
    """Mutating routes depend on this. 401 if the session cookie is
    missing, malformed (SessionMiddleware drops it), or hasn't been
    flipped to authenticated by ``POST /ui/login``."""
    if not request.session.get(SESSION_AUTHED_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )
