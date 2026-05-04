"""Bearer-token auth dependency for bty-web.

Single-token model: there is one ``BTY_WEB_TOKEN`` configured at
server startup. Requests to protected routes must carry
``Authorization: Bearer <token>``. Comparison is constant-time via
:func:`secrets.compare_digest`.

If the token is unset at startup, ``main()`` refuses to start the
server — it fails closed by default.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]


def make_token_dep(expected_token: str) -> Callable[..., None]:
    """Return a FastAPI dependency that enforces ``Bearer <expected_token>``.

    Raises 401 on missing or mismatched token. ``expected_token`` is
    captured by closure so tests can wire up an isolated token without
    touching the environment.
    """
    if not expected_token:
        raise ValueError("expected_token must be a non-empty string")

    def check_token(credentials: _BearerCredentials) -> None:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(credentials.credentials, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check_token
