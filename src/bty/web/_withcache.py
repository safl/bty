"""Optional withcache integration: build ``/b/<b64>/<name>`` URLs
that point at withcache's byte-serving surface.

Since withcache v0.11.0 the catalog's ``GET /catalog`` returns only
downloaded entries, so bty's own ``WithcacheCatalog.entries`` is
already the authoritative list of ready-to-flash images -- no
runtime HEAD probes needed to decide whether a URL is ready. The
old ``is_cached`` helper is gone; consumers just call
:func:`blob_url` when a withcache URL is configured.

The ``/b/<urlsafe-b64(origin)>/<basename>`` encoding MUST match
withcache's own ``_shim.blob_url`` / server decoding; that path
layout is the contract between the two.
"""

from __future__ import annotations

import base64
import os
import urllib.parse


def _base(url: str) -> str:
    return url.strip().rstrip("/")


def _basename(origin: str) -> str:
    name = os.path.basename(urllib.parse.urlsplit(origin).path)
    return name or "download"


def blob_url(withcache: str, origin: str) -> str:
    """withcache's path-encoded serve URL for ``origin``:
    ``<withcache>/b/<urlsafe-b64(origin), unpadded>/<basename>``. The
    trailing basename is cosmetic (so the live env names the file
    right); withcache keys on the decoded origin URL."""
    token = base64.urlsafe_b64encode(origin.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_base(withcache)}/b/{token}/{urllib.parse.quote(_basename(origin))}"
