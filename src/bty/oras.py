"""ORAS / OCI registry adapter for fetching disk images.

Lets ``.bri`` descriptors point at OCI artifacts -- disk images
published via ORAS_ (OCI Registry As Storage), *not* container
images -- via a tiny URL scheme prefix. Operators write::

    url = "oras://ghcr.io/safl/nosi/debian-sysdev:latest"

and bty resolves the tag to a manifest, picks the disk-image layer,
and streams the blob to disk through the same flash pipeline used
for plain HTTPS URLs. Digest-pinned references look like::

    url = "oras://ghcr.io/safl/nosi/debian-sysdev@sha256:94e6..."

and skip the manifest fetch entirely -- the digest IS the address.

Why ``oras://`` and not ``ghcr:``
---------------------------------

The ORAS spelling disambiguates from container references. A reader
who sees ``ghcr.io/safl/nosi/debian-sysdev:latest`` in a docs example
might reach for ``docker pull`` or ``podman run`` -- which would
fail and leave them confused, because nosi publishes disk-image
artifacts, not runnable container images. ``oras://`` is the
ecosystem term for OCI-Registry-As-Storage; an operator googling it
lands at oras.land which explicitly explains "store arbitrary
artifacts, not just containers". The ``://`` form also composes
with other registries -- ``oras://quay.io/...``,
``oras://registry.example.com:5000/...`` -- without per-registry
schemes.

.. _ORAS: https://oras.land/

Auth
----

Spec-compliant OCI v2 registries (GHCR included) return 401 on every
request even for public packages. Their ``/token`` endpoint
mints anonymous tokens on a plain credential-less GET. So the flow
is: hit ``https://<host>/token``, take the returned bearer, set
``Authorization: Bearer`` on the manifest + blob requests. No
registry login, no PAT, no secrets shipped.

The token endpoint is built from the URL's host (``ghcr.io`` ->
``https://ghcr.io/token``), which works for GHCR and any registry
that follows the same convention. Registries with non-standard auth
flows (private registries with custom realms, e.g.) would need the
proper ``WWW-Authenticate`` challenge dance instead -- noted as
future work; not needed for the homelab / nosi use case this module
ships for.

Layer picker
------------

A nosi manifest carries two layers: the ``.img.gz`` disk image and a
``.sha256`` sidecar. The picker drops layers whose
``org.opencontainers.image.title`` annotation ends in a known sidecar
suffix (``.sha256``, ``.sha512``, ``.sig``, ``.asc``, ``.pem``,
``.cert``, ``.sbom``, ``.att``, ``.json``), then takes the largest
remaining layer by declared size. Manifests with no useful
annotations fall through to the largest layer overall -- a reasonable
bet that the image bytes dwarf any metadata sidecar.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

ORAS_SCHEME = "oras://"

# Accept type covers OCI v1 + Docker v2 manifest media types so the
# registry doesn't bounce us with a 406 if the package was originally
# pushed as a Docker manifest.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)

# Layer titles ending in any of these are non-image sidecars (sha
# sums, signatures, attestations); skip them when picking the image
# layer so a future ``oras attach`` on the same artifact doesn't
# silently start being flashed.
_SIDECAR_SUFFIXES = (
    ".sha256",
    ".sha512",
    ".sig",
    ".asc",
    ".pem",
    ".cert",
    ".sbom",
    ".att",
    ".json",
)


class OrasError(OSError):
    """Raised on parse / resolution / fetch errors against an OCI registry.

    Inherits from :class:`OSError` so it's caught by callers that
    handle remote-I/O failures generically (``bty``, the catalog
    boundary) -- semantically the same family as
    :class:`urllib.error.URLError`, which also subclasses
    :class:`OSError`. Code paths that need to distinguish ORAS-
    specific failures from arbitrary network errors still can:
    :class:`OrasError` is a strict subclass."""


@dataclass(frozen=True)
class OrasRef:
    """Parsed ``oras://`` reference.

    Exactly one of ``tag`` / ``digest`` is set. ``digest`` references
    skip the manifest fetch (the digest is content-addressed, so the
    blob URL is fully determined). ``tag`` references go through the
    manifest to resolve a layer digest first.
    """

    host: str  # e.g. "ghcr.io" or "registry.example.com:5000"
    repository: str  # e.g. "safl/nosi/debian-sysdev"
    tag: str | None = None
    digest: str | None = None

    @property
    def manifest_locator(self) -> str:
        """Value used in the ``/manifests/<X>`` URL path."""
        if self.digest is not None:
            return self.digest
        assert self.tag is not None, "OrasRef must have either tag or digest set"
        return self.tag


# Host: DNS hostname (or registry.example.com:5000 with optional port).
# Repository: lowercase alnum + ``/_.-``, must contain at least one
# ``/`` after the host (owner + repo). Tag: OCI tag charset (alnum +
# ``._-``). Digest: only sha256 today; future algorithms would need
# extending. Layout overall::
#
#     <host>[:port]/<repo>(:<tag>|@sha256:<hex>)
#
# applied to the body after stripping the ``oras://`` scheme.
_REF_RE = re.compile(
    r"^"
    r"(?P<host>[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]+)?)"
    r"/"
    r"(?P<repo>[a-z0-9][a-z0-9/_.-]*)"
    r"(?:(?:@(?P<digest>sha256:[0-9a-f]{64}))"
    r"|(?::(?P<tag>[A-Za-z0-9._-]+)))"
    r"$"
)


def parse_ref(ref: str) -> OrasRef:
    """Parse an ``oras://`` reference into a :class:`OrasRef`.

    Accepts the two canonical forms::

        oras://<host>/<owner>/<repo>[/<extra>]:<tag>
        oras://<host>/<owner>/<repo>[/<extra>]@sha256:<64-hex>

    Raises :class:`OrasError` on any malformed input. The repository
    component must contain at least one ``/`` -- a bare top-level
    path like ``oras://ghcr.io/nosi:latest`` is rejected because OCI's
    URL scheme requires owner+repo under the host.
    """
    if not ref.startswith(ORAS_SCHEME):
        raise OrasError(f"not an oras:// reference: {ref!r}")
    body = ref[len(ORAS_SCHEME) :]
    if not body:
        raise OrasError(f"empty oras:// reference: {ref!r}")
    match = _REF_RE.match(body)
    if match is None:
        raise OrasError(
            f"malformed oras:// reference {ref!r}: "
            f"expected oras://<host>/<owner>/<repo>:<tag> or "
            f"oras://<host>/<owner>/<repo>@sha256:<hex>"
        )
    repo = match.group("repo")
    if "/" not in repo:
        raise OrasError(
            f"oras:// reference must include <host>/<owner>/<repo>: "
            f"{ref!r} (got bare repo {repo!r} after host)"
        )
    return OrasRef(
        host=match.group("host"),
        repository=repo,
        tag=match.group("tag"),
        digest=match.group("digest"),
    )


def fetch_anonymous_token(host: str, repository: str, *, timeout: float = 30.0) -> str:
    """Grab an anonymous bearer token for ``repository:pull``.

    Spec-compliant OCI v2 registries (GHCR included) expose a
    ``/token`` endpoint that accepts unauthenticated GETs for public
    packages and returns a short-lived bearer in the response body.
    No credentials, no PAT, no signup. The token scope is read-only
    and repository-specific.
    """
    url = (
        f"https://{host}/token?service={host}"
        f"&scope=repository:{urllib.parse.quote(repository, safe='/')}:pull"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # ``OSError`` covers ``URLError`` (its subclass) plus the raw
        # connection failures urllib re-raises through it on some
        # platforms; tests also patch in a plain ``OSError`` to
        # simulate "registry unreachable" without constructing a
        # full URLError.
        raise OrasError(f"oras token fetch failed for {host}/{repository}: {exc}") from exc
    # OCI registries return ``token``; some spell it ``access_token``.
    token = payload.get("token") or payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise OrasError(
            f"oras token response for {host}/{repository} did not contain "
            f"a token (keys: {sorted(payload.keys())})"
        )
    return token


def fetch_manifest(ref: OrasRef, token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch the OCI manifest for ``ref`` using a previously-acquired token.

    Returns the parsed JSON. Raises :class:`OrasError` on network or
    parse failure. The caller is responsible for layer selection.
    """
    locator = urllib.parse.quote(ref.manifest_locator, safe=":")
    url = f"https://{ref.host}/v2/{ref.repository}/manifests/{locator}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": _MANIFEST_ACCEPT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise OrasError(
            f"oras manifest fetch failed for "
            f"{ref.host}/{ref.repository}:{ref.manifest_locator}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise OrasError(
            f"oras manifest for "
            f"{ref.host}/{ref.repository}:{ref.manifest_locator} "
            f"is not a JSON object"
        )
    return payload


def _layer_title(layer: dict[str, Any]) -> str:
    annotations = layer.get("annotations")
    if not isinstance(annotations, dict):
        return ""
    title = annotations.get("org.opencontainers.image.title", "")
    return title if isinstance(title, str) else ""


def pick_image_layer(manifest: dict[str, Any]) -> dict[str, Any]:
    """Pick the disk-image layer from a (possibly multi-layer) manifest.

    Drops sidecar-looking layers by title-annotation suffix, then takes
    the largest by declared ``size``. A manifest with no usable
    annotations falls through to the largest layer overall -- the image
    bytes will dwarf any metadata blob in practice.

    Raises :class:`OrasError` if the manifest has no layers at all.
    """
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise OrasError("manifest has no layers")

    image_like: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        title = _layer_title(layer)
        if title and any(title.endswith(suffix) for suffix in _SIDECAR_SUFFIXES):
            continue
        image_like.append(layer)
    # If every layer looked like a sidecar (shouldn't happen for a real
    # disk-image artifact but tolerate it), fall back to all dict layers
    # so the size-pick still has something to choose from.
    candidates = image_like or [layer for layer in layers if isinstance(layer, dict)]
    if not candidates:
        raise OrasError("manifest has no usable layers")
    return max(candidates, key=lambda layer: layer.get("size") or 0)


@dataclass(frozen=True)
class ResolvedBlob:
    """Everything a fetcher needs to stream the image bytes.

    ``blob_url`` is the final ``/v2/<repo>/blobs/sha256:<digest>``
    endpoint. ``headers`` carries the bearer the registry expects. The
    ``digest`` is what the fetcher should verify the downloaded bytes
    against -- when the caller started from a tag, this is the digest
    the registry resolved to right now, frozen for the rest of the
    flash.
    """

    blob_url: str
    headers: dict[str, str]
    digest: str
    size: int | None
    title: str | None


def resolve_ref(ref: str | OrasRef, *, timeout: float = 30.0) -> ResolvedBlob:
    """Resolve an ``oras://`` reference (or pre-parsed :class:`OrasRef`)
    to a :class:`ResolvedBlob`.

    For tag references: anonymous token -> manifest -> layer pick ->
    ``ResolvedBlob`` with the layer's content-addressed digest. The
    digest is frozen at resolve time so a tag that moves under us
    between resolve and fetch still produces the bytes we committed to.

    For digest-pinned references: anonymous token only; the blob URL is
    fully determined by the digest, the manifest fetch is skipped, and
    size / title come back as ``None`` (the descriptor's optional
    ``size_bytes`` field can carry that info instead if known).
    """
    if isinstance(ref, str):
        ref = parse_ref(ref)
    token = fetch_anonymous_token(ref.host, ref.repository, timeout=timeout)
    headers = {"Authorization": f"Bearer {token}"}

    if ref.digest is not None:
        digest = ref.digest
        size: int | None = None
        title: str | None = None
    else:
        manifest = fetch_manifest(ref, token, timeout=timeout)
        layer = pick_image_layer(manifest)
        raw_digest = layer.get("digest")
        if not isinstance(raw_digest, str) or not raw_digest.startswith("sha256:"):
            raise OrasError(
                f"picked layer for "
                f"{ref.host}/{ref.repository}:{ref.manifest_locator} "
                f"has unusable digest {raw_digest!r}"
            )
        digest = raw_digest
        layer_size = layer.get("size")
        size = layer_size if isinstance(layer_size, int) else None
        title = _layer_title(layer) or None

    blob_url = f"https://{ref.host}/v2/{ref.repository}/blobs/{digest}"
    return ResolvedBlob(
        blob_url=blob_url,
        headers=headers,
        digest=digest,
        size=size,
        title=title,
    )


def is_oras_url(url: str) -> bool:
    """True iff ``url`` is an ``oras://`` reference rather than http(s)://."""
    return url.startswith(ORAS_SCHEME)
