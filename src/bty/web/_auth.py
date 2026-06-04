"""Session-cookie auth for bty-web.

Single-tenant model (same shape as withcache): one admin password, supplied via
``$BTY_ADMIN_PASSWORD``, checked at ``POST /ui/login``. A successful login flips
``request.session["bty_authed"] = True``; the session is a server-signed cookie
managed by Starlette's :class:`SessionMiddleware`, so no DB session table is
needed.

When ``$BTY_ADMIN_PASSWORD`` is unset the operator UI is left open (a startup
warning is logged) -- the single-box lab default. Set the password to gate it.

Failure modes return 401; ``/ui/*`` routes catch the exception in a middleware
and redirect to ``/ui/login``.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

# Session-cookie name. Set explicitly so the PXE chain test and operator
# scripts can grep for a stable token in Set-Cookie.
SESSION_COOKIE = "bty-token"

# Session key the auth dep checks. Set on successful /ui/login.
SESSION_AUTHED_KEY = "bty_authed"

# Admin password env var. Unset => the operator UI is open (with a warning).
ADMIN_PASSWORD_ENV = "BTY_ADMIN_PASSWORD"


def admin_password() -> str | None:
    """The configured admin password, or ``None`` if unset/empty."""
    return os.environ.get(ADMIN_PASSWORD_ENV) or None


def auth_enabled() -> bool:
    """True when a password gates the operator UI; False leaves it open."""
    return admin_password() is not None


def check_password(password: str) -> bool:
    """Constant-time compare of ``password`` against ``$BTY_ADMIN_PASSWORD``."""
    pw = admin_password()
    return pw is not None and hmac.compare_digest(password, pw)


def require_auth(request: Request) -> None:
    """Mutating routes depend on this. Open when no password is configured;
    otherwise 401 unless ``POST /ui/login`` has flipped the session flag."""
    if not auth_enabled():
        return
    if not request.session.get(SESSION_AUTHED_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )
