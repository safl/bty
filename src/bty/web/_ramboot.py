"""Small helpers around the nbdmux control plane for ramboot.

Since v0.65.0 nbdmux owns the fetch + decompress pipeline for
ramboot-served images; bty-web only validates readiness. Three
call sites all want the same shape: ``{export_name -> status}``
from :func:`nbdmux.client.list_exports`, and ``{}`` on any
failure (blank URL, unreachable daemon, network blip) so the
enclosing page or plan can still render.

The bind-time PUT validator at :func:`bty.web._app` is
intentionally NOT routed through here: it maps a
``NbdmuxError`` to HTTP 502 so the operator sees a distinct
"nbdmux unreachable" response rather than a generic "ref not
ready", which the swallow-and-empty-dict shape here would
collapse into.
"""

from __future__ import annotations

from typing import Any

from nbdmux import client as nbdmux_client

_POLL_TIMEOUT_SECONDS = 2.0


def status_by_ref(nbdmux_url: str | None) -> dict[str, str]:
    """Return the current ``{export_name -> status}`` map.

    Returns an empty dict when ``nbdmux_url`` is unset or when
    :func:`nbdmux.client.list_exports` raises (unreachable daemon,
    HTTP error, timeout). Callers that need to distinguish
    "unreachable" from "just not warmed" catch the raw exception
    themselves (see the PUT /machines validator in :mod:`._app`).

    Never blocks longer than ~2 s per call so a slow daemon can't
    wedge a page render.
    """
    if not nbdmux_url:
        return {}
    try:
        exports = nbdmux_client.list_exports(server=nbdmux_url, timeout=_POLL_TIMEOUT_SECONDS)
    except Exception:
        return {}
    return {str(e.get("name")): str(e.get("status") or "") for e in exports if e.get("name")}


def exports_by_src(nbdmux_url: str | None) -> list[dict[str, Any]]:
    """Return the raw ``list_exports`` rows, or ``[]`` on failure.

    Since PR #33 nbdmux keys exports by their URL basename rather
    than by bty's 64-hex ref, so lookup callers walk the full row
    list and match on ``src_url``. Same swallow-and-empty-list
    behavior as :func:`status_by_ref` -- unreachable / timeout /
    HTTP error yield ``[]`` and the enclosing plan renderer
    gracefully falls back to interactive mode.
    """
    if not nbdmux_url:
        return []
    try:
        exports = nbdmux_client.list_exports(server=nbdmux_url, timeout=_POLL_TIMEOUT_SECONDS)
    except Exception:
        return []
    return [dict(e) for e in exports]
