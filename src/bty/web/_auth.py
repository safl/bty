"""Session-cookie auth for bty-web.

Single-tenant model (same shape as withcache): one admin password, checked at
``POST /ui/login``. A successful login flips
``request.session["bty_authed"] = True``; the session is a server-signed cookie
managed by Starlette's :class:`SessionMiddleware`, so no DB session table is
needed.

The password is sourced from ``$BTY_ADMIN_PASSWORD`` if set + non-empty,
otherwise falls back to the literal string :data:`DEFAULT_ADMIN_PASSWORD`
(``"bty"``). Auth is ALWAYS on -- there is no "open access" mode; an unset
env var just means the operator gets the well-known default until they
override it. The startup banner logs a warning when the default is in use
so an operator who's actually exposed bty-web doesn't silently ship with
``bty / bty``.

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

# Admin password env var. Overrides the well-known default below.
ADMIN_PASSWORD_ENV = "BTY_ADMIN_PASSWORD"

# Fallback password when ``$BTY_ADMIN_PASSWORD`` is unset. Auth is always
# on; this just means a fresh deploy can be opened with a known string and
# the operator should change it. Documented + warned about on startup.
DEFAULT_ADMIN_PASSWORD = "bty"


def admin_password() -> str:
    """The active admin password.

    ``$BTY_ADMIN_PASSWORD`` wins if set + non-empty; otherwise
    :data:`DEFAULT_ADMIN_PASSWORD`. Never returns ``None`` -- auth is
    always on.
    """
    env = (os.environ.get(ADMIN_PASSWORD_ENV) or "").strip()
    return env or DEFAULT_ADMIN_PASSWORD


def using_default_password() -> bool:
    """True iff the active password is the well-known fallback. The
    startup banner uses this to log a clear warning, and the Account
    page renders a "change me" callout."""
    return admin_password() == DEFAULT_ADMIN_PASSWORD


def check_password(password: str) -> bool:
    """Constant-time compare against the active admin password."""
    return hmac.compare_digest(password, admin_password())


def require_auth(request: Request) -> None:
    """Mutating routes depend on this. 401 unless ``POST /ui/login`` has
    flipped the session flag for this client."""
    if not request.session.get(SESSION_AUTHED_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )
