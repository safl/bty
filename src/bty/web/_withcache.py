"""Optional withcache integration: when a withcache cache-host is configured,
bty prefers it as the *source* for image artifacts it has already cached, and
otherwise serves the artifact itself exactly as before.

bty stays the policy/PXE layer; withcache (https://github.com/safl/withcache)
owns the bytes. For an origin URL, :func:`blob_url` builds withcache's
path-encoded serve URL and :func:`is_cached` asks withcache via a cheap ``HEAD``
on its open read path whether it already holds that artifact. The HEAD also
*warms* withcache: a cold miss is recorded and, in withcache's auto-fetch mode,
enqueues the background fill, so the next check flips to cached.

Everything degrades gracefully: an unreachable withcache, a timeout, or any
error means "not cached", and bty serves the artifact itself as before. So
turning withcache on can only add caching, never break a boot.

The ``/b/<urlsafe-b64(origin)>/<basename>`` encoding MUST match withcache's
own ``_shim.blob_url`` / server decoding; that path layout is the contract
between the two.
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger(__name__)

# Never block a boot plan on a slow/unreachable cache.
PROBE_TIMEOUT = 3  # seconds


def _base(url: str) -> str:
    return url.strip().rstrip("/")


def _basename(origin: str) -> str:
    name = os.path.basename(urllib.parse.urlsplit(origin).path)
    return name or "download"


def blob_url(withcache: str, origin: str) -> str:
    """withcache's path-encoded serve URL for ``origin``:
    ``<withcache>/b/<urlsafe-b64(origin), unpadded>/<basename>``. The trailing
    basename is cosmetic (so the live env names the file right); withcache keys
    on the decoded origin URL."""
    token = base64.urlsafe_b64encode(origin.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_base(withcache)}/b/{token}/{urllib.parse.quote(_basename(origin))}"


def is_cached(
    withcache: str,
    origin: str,
    timeout: float = PROBE_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> bool:
    """True if withcache already holds ``origin`` (HEAD -> 200). A miss (404),
    an unreachable cache, or any error returns False, the caller then serves
    the artifact itself. The HEAD also warms an auto-fetch withcache.

    ``headers`` (optional) attaches request headers to the HEAD. From v0.4.0
    withcache forwards the client-supplied ``Authorization`` header into the
    background-fetch worker, so a consumer that has just minted an OCI
    bearer (the bty oras case: a fresh anon token against ghcr.io for the
    catalog entry's resolved blob URL) can warm a token-gated origin in
    one probe. Cache hits are still served bearer-free (cached bytes never
    revisit the origin).
    """
    url = blob_url(withcache, origin)
    req = urllib.request.Request(url, method="HEAD")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hit = bool(resp.status == 200)
            _log.info("withcache HEAD %s -> %d (%s)", url, resp.status, "hit" if hit else "miss")
            return hit
    except urllib.error.HTTPError as exc:
        # 404 miss (now recorded + enqueued by an auto-fetch withcache).
        _log.info("withcache HEAD %s -> %d (miss)", url, exc.code)
        return False
    except (urllib.error.URLError, OSError) as exc:
        # Unreachable / timeout: serve it ourselves. A misconfig signal worth
        # surfacing, not silently swallowing.
        _log.warning("withcache HEAD %s unreachable: %s; serving origin", url, exc)
        return False
