"""Request-context helpers shared by the API (``_app``) and UI (``_ui``)
layers: client-IP normalisation and MAC canonicalisation.

These live in their own module rather than in ``_app`` because ``_app``
imports ``_ui`` (to register the UI routes), so ``_ui`` can't import
``_app`` at module load (the two would form an import cycle). A neutral
low-level module both can import breaks the cycle without the copy-paste
the two layers used to carry.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from bty.web import _config
from bty.web._events_log import normalize_ip


def client_ip(request: Request) -> str | None:
    """Return the request's client IP, normalised for storage.

    Wraps ``request.client.host`` in ``_events_log.normalize_ip``
    so a v4-mapped-v6 address (``::ffff:192.168.1.5``, the form
    Starlette returns when bty-web binds on ``::`` and a v4 client
    connects) collapses to the bare v4 form. Without this, the
    same client shows up as two distinct rows in the audit log.

    When ``[server] trusted_proxy`` is set (env override
    ``BTY_SERVER_TRUSTED_PROXY``), the leftmost ``X-Forwarded-For``
    entry takes precedence so audit rows reflect the real client
    IP rather than the reverse-proxy's loopback. Off by default
    because the header is client-spoofable: only enable it when
    bty-web is configured behind a proxy that strips inbound X-F-F.
    """
    if _config.cfg().server.trusted_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # X-F-F is a comma-separated chain (proxy-near-client
            # first); the leftmost entry is the originating client.
            first = xff.split(",", 1)[0].strip()
            if first:
                return normalize_ip(first)
    return normalize_ip(request.client.host if request.client else None)


def normalise_mac(raw: str) -> str:
    """Return a canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC, or 400."""
    cleaned = raw.lower().replace("-", ":")
    parts = cleaned.split(":")
    if len(parts) != 6 or any(
        len(p) != 2 or any(c not in "0123456789abcdef" for c in p) for p in parts
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid MAC: {raw!r}",
        )
    return cleaned
