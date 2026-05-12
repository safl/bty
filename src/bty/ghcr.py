"""GHCR / OCI registry adapter for fetching disk images.

Lets ``.bri`` descriptors point at GitHub Container Registry artefacts
via a tiny URL scheme prefix. Operators write::

    url = "ghcr:safl/nosi/debian-base:latest"

and bty resolves the tag to a manifest, picks the disk-image layer,
and streams the blob to disk through the same flash pipeline used
for plain HTTPS URLs. Digest-pinned references look like::

    url = "ghcr:safl/nosi/debian-base@sha256:94e6..."

and skip the manifest fetch entirely -- the digest IS the address.

Auth
----

GHCR returns 401 on every request even for public packages, but its
``/token`` endpoint mints anonymous tokens on a plain credential-less
GET. So the flow is: hit ``/token``, take the returned bearer, set
``Authorization: Bearer`` on the manifest + blob requests. No
registry login, no PAT, no secrets shipped.

Layer picker
------------

A nosi manifest carries two layers: the ``.img.gz`` disk image and a
``.sha256`` sidecar. The picker drops layers whose
``org.opencontainers.image.title`` annotation ends in a known sidecar
suffix (``.sha256``, ``.sig``, ``.asc``, ``.pem``, ``.cert``,
``.sbom``, ``.att``), then takes the largest remaining layer by
declared size. Manifests with no useful annotations fall through to
the largest layer overall -- a reasonable bet that the image bytes
dwarf any metadata sidecar.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

GHCR_SCHEME = "ghcr:"
GHCR_HOST = "ghcr.io"
GHCR_TOKEN_URL = f"https://{GHCR_HOST}/token"

# Accept type covers OCI v1 + Docker v2 manifest media types so the
# registry doesn't bounce us with a 406 if the package was originally
# pushed as a Docker manifest.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)

# Layer titles ending in any of these are non-image sidecars (sha
# sums, signatures, attestations); skip them when picking the image
# layer so a future ``oras attach`` on the same artefact doesn't
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


class GhcrError(Exception):
    """Raised on parse / resolution / fetch errors against GHCR.

    Distinct from generic exceptions so callers can surface a friendly
    per-reference error without conflating it with unrelated network
    failures."""


@dataclass(frozen=True)
class GhcrRef:
    """Parsed ``ghcr:`` reference.

    Exactly one of ``tag`` / ``digest`` is set. ``digest`` references
    skip the manifest fetch (the digest is content-addressed, so the
    blob URL is fully determined). ``tag`` references go through the
    manifest to resolve a layer digest first.
    """

    repository: str  # e.g. "safl/nosi/debian-base"
    tag: str | None = None
    digest: str | None = None

    @property
    def manifest_locator(self) -> str:
        """Value used in the ``/manifests/<X>`` URL path."""
        if self.digest is not None:
            return self.digest
        assert self.tag is not None, "GhcrRef must have either tag or digest set"
        return self.tag


# Repository: lowercase alnum + ``/_.-``, must contain at least one ``/``
# (owner + repo). Tag: GitHub uses the OCI tag charset (alnum + ``._-``).
# Digest: only sha256 today; future algorithms would need extending.
_REF_RE = re.compile(
    r"^(?P<repo>[a-z0-9][a-z0-9/_.-]*)"
    r"(?:(?:@(?P<digest>sha256:[0-9a-f]{64}))"
    r"|(?::(?P<tag>[A-Za-z0-9._-]+)))$"
)


def parse_ref(ref: str) -> GhcrRef:
    """Parse a ``ghcr:`` reference string into a :class:`GhcrRef`.

    Accepts the two canonical forms::

        ghcr:owner/repo[/extra]:tag
        ghcr:owner/repo[/extra]@sha256:<64-hex>

    Raises :class:`GhcrError` on any malformed input. The repository
    component must contain at least one ``/`` -- a bare top-level path
    like ``ghcr:nosi:latest`` is rejected because GHCR's URL scheme
    requires owner+repo.
    """
    if not ref.startswith(GHCR_SCHEME):
        raise GhcrError(f"not a ghcr: reference: {ref!r}")
    body = ref[len(GHCR_SCHEME) :]
    if not body:
        raise GhcrError(f"empty ghcr: reference: {ref!r}")
    match = _REF_RE.match(body)
    if match is None:
        raise GhcrError(
            f"malformed ghcr: reference {ref!r}: "
            f"expected ghcr:owner/repo:tag or ghcr:owner/repo@sha256:<hex>"
        )
    repo = match.group("repo")
    if "/" not in repo:
        raise GhcrError(f"ghcr: reference must include owner/repo: {ref!r} (got bare {repo!r})")
    return GhcrRef(repository=repo, tag=match.group("tag"), digest=match.group("digest"))


def fetch_anonymous_token(repository: str, *, timeout: float = 30.0) -> str:
    """Grab an anonymous bearer token for ``repository:pull``.

    GHCR's ``/token`` endpoint accepts unauthenticated GETs for public
    packages and returns a short-lived bearer in the response body. No
    credentials, no PAT, no signup. The token scope is read-only and
    repository-specific.
    """
    url = (
        f"{GHCR_TOKEN_URL}?service={GHCR_HOST}"
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
        raise GhcrError(f"ghcr token fetch failed for {repository}: {exc}") from exc
    # GHCR returns ``token``; some registries spell it ``access_token``.
    token = payload.get("token") or payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise GhcrError(
            f"ghcr token response for {repository} did not contain a token "
            f"(keys: {sorted(payload.keys())})"
        )
    return token


def fetch_manifest(ref: GhcrRef, token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch the OCI manifest for ``ref`` using a previously-acquired token.

    Returns the parsed JSON. Raises :class:`GhcrError` on network or
    parse failure. The caller is responsible for layer selection.
    """
    locator = urllib.parse.quote(ref.manifest_locator, safe=":")
    url = f"https://{GHCR_HOST}/v2/{ref.repository}/manifests/{locator}"
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
        raise GhcrError(
            f"ghcr manifest fetch failed for {ref.repository}:{ref.manifest_locator}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GhcrError(
            f"ghcr manifest for {ref.repository}:{ref.manifest_locator} is not a JSON object"
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

    Raises :class:`GhcrError` if the manifest has no layers at all.
    """
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise GhcrError("manifest has no layers")

    image_like: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        title = _layer_title(layer)
        if title and any(title.endswith(suffix) for suffix in _SIDECAR_SUFFIXES):
            continue
        image_like.append(layer)
    # If every layer looked like a sidecar (shouldn't happen for a real
    # disk-image artefact but tolerate it), fall back to all dict layers
    # so the size-pick still has something to choose from.
    candidates = image_like or [layer for layer in layers if isinstance(layer, dict)]
    if not candidates:
        raise GhcrError("manifest has no usable layers")
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


def resolve_ref(ref: str | GhcrRef, *, timeout: float = 30.0) -> ResolvedBlob:
    """Resolve a ``ghcr:`` reference (or pre-parsed :class:`GhcrRef`) to a
    :class:`ResolvedBlob`.

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
    token = fetch_anonymous_token(ref.repository, timeout=timeout)
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
            raise GhcrError(
                f"picked layer for {ref.repository}:{ref.manifest_locator} "
                f"has unusable digest {raw_digest!r}"
            )
        digest = raw_digest
        layer_size = layer.get("size")
        size = layer_size if isinstance(layer_size, int) else None
        title = _layer_title(layer) or None

    blob_url = f"https://{GHCR_HOST}/v2/{ref.repository}/blobs/{digest}"
    return ResolvedBlob(
        blob_url=blob_url,
        headers=headers,
        digest=digest,
        size=size,
        title=title,
    )


def is_ghcr_url(url: str) -> bool:
    """True iff ``url`` is a ``ghcr:`` reference rather than http(s)://."""
    return url.startswith(GHCR_SCHEME)
