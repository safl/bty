"""Tests for :mod:`bty.ghcr` -- the GHCR/OCI registry adapter.

Parser tests are pure; resolver tests mock ``urllib.request.urlopen``
since we want them to run on offline CI and not hit the real registry.
The mock returns either a token payload or a manifest payload based
on the requested URL substring.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from bty import ghcr

# A trimmed-down version of a real nosi manifest -- two layers (the
# .img.gz and a .sha256 sidecar), one annotation each, OCI media types.
_NOSI_MANIFEST: dict[str, Any] = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "artifactType": "application/vnd.nosi.disk-image.v1+gzip",
    "layers": [
        {
            "mediaType": "application/vnd.nosi.disk-image.layer.v1+gzip",
            "digest": "sha256:" + "aa" * 32,
            "size": 1923658046,
            "annotations": {"org.opencontainers.image.title": "nosi-debian-base-x86_64.img.gz"},
        },
        {
            "mediaType": "text/plain",
            "digest": "sha256:" + "bb" * 32,
            "size": 130,
            "annotations": {
                "org.opencontainers.image.title": "nosi-debian-base-x86_64.img.gz.sha256"
            },
        },
    ],
}


# ---------- parse_ref ---------------------------------------------------------


def test_parse_ref_tag_form() -> None:
    ref = ghcr.parse_ref("ghcr:safl/nosi/debian-base:latest")
    assert ref.repository == "safl/nosi/debian-base"
    assert ref.tag == "latest"
    assert ref.digest is None
    assert ref.manifest_locator == "latest"


def test_parse_ref_digest_form() -> None:
    digest = "sha256:" + "ab" * 32
    ref = ghcr.parse_ref(f"ghcr:safl/nosi/debian-base@{digest}")
    assert ref.repository == "safl/nosi/debian-base"
    assert ref.tag is None
    assert ref.digest == digest
    assert ref.manifest_locator == digest


def test_parse_ref_owner_repo_minimum() -> None:
    """Two-segment owner/repo is the minimum; anything shorter is rejected."""
    ref = ghcr.parse_ref("ghcr:owner/repo:v1")
    assert ref.repository == "owner/repo"


def test_parse_ref_rejects_bare_repo() -> None:
    """A path with no ``/`` is not a valid OCI repository."""
    with pytest.raises(ghcr.GhcrError, match="owner/repo"):
        ghcr.parse_ref("ghcr:nosi:latest")


def test_parse_ref_rejects_missing_scheme() -> None:
    with pytest.raises(ghcr.GhcrError, match="not a ghcr:"):
        ghcr.parse_ref("safl/nosi:latest")


def test_parse_ref_rejects_empty_body() -> None:
    with pytest.raises(ghcr.GhcrError, match="empty"):
        ghcr.parse_ref("ghcr:")


def test_parse_ref_rejects_missing_tag_and_digest() -> None:
    """Tagless / digestless refs aren't pullable; reject."""
    with pytest.raises(ghcr.GhcrError, match="malformed"):
        ghcr.parse_ref("ghcr:safl/nosi/debian-base")


def test_parse_ref_rejects_short_digest() -> None:
    """sha256 digests must be 64 hex chars; partial digests would
    silently mis-address the blob."""
    with pytest.raises(ghcr.GhcrError, match="malformed"):
        ghcr.parse_ref("ghcr:safl/nosi/debian-base@sha256:abc123")


# ---------- pick_image_layer --------------------------------------------------


def test_pick_image_layer_skips_sha256_sidecar() -> None:
    layer = ghcr.pick_image_layer(_NOSI_MANIFEST)
    title = layer["annotations"]["org.opencontainers.image.title"]
    assert title == "nosi-debian-base-x86_64.img.gz"
    assert not title.endswith(".sha256")


def test_pick_image_layer_picks_largest_when_no_sidecar() -> None:
    """Two non-sidecar layers -> pick the larger one (image bytes
    always dwarf incidental metadata)."""
    manifest: dict[str, Any] = {
        "layers": [
            {"digest": "sha256:" + "11" * 32, "size": 100, "annotations": {}},
            {"digest": "sha256:" + "22" * 32, "size": 1_000_000, "annotations": {}},
        ]
    }
    layer = ghcr.pick_image_layer(manifest)
    assert layer["size"] == 1_000_000


def test_pick_image_layer_raises_on_empty_layers() -> None:
    with pytest.raises(ghcr.GhcrError, match="no layers"):
        ghcr.pick_image_layer({"layers": []})


def test_pick_image_layer_falls_back_when_all_look_like_sidecars() -> None:
    """If the picker filtered everything out (e.g. every layer is
    annotated as .sha256), fall back to picking the largest of the
    raw layer list rather than raising -- gives the resolver a chance
    to fail loudly later instead of mis-classifying."""
    manifest: dict[str, Any] = {
        "layers": [
            {
                "digest": "sha256:" + "33" * 32,
                "size": 50,
                "annotations": {"org.opencontainers.image.title": "a.sha256"},
            },
            {
                "digest": "sha256:" + "44" * 32,
                "size": 500,
                "annotations": {"org.opencontainers.image.title": "b.sha256"},
            },
        ]
    }
    layer = ghcr.pick_image_layer(manifest)
    assert layer["size"] == 500


# ---------- resolve_ref (mocked urlopen) --------------------------------------


def _make_urlopen_mock(token: str = "anon-token-xyz"):
    """Build a urlopen replacement that returns a token payload for the
    /token endpoint and the nosi manifest for any /manifests/ URL."""

    def _fake_urlopen(req, timeout=None):
        # req can be a str (for the token GET) or a urllib Request.
        url = req if isinstance(req, str) else req.full_url

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

        if "/token" in url:
            return _Resp(json.dumps({"token": token}).encode())
        if "/manifests/" in url:
            return _Resp(json.dumps(_NOSI_MANIFEST).encode())
        raise AssertionError(f"unexpected URL in test: {url}")

    return _fake_urlopen


def test_resolve_ref_tag_resolves_to_layer_digest() -> None:
    with patch("urllib.request.urlopen", _make_urlopen_mock()):
        resolved = ghcr.resolve_ref("ghcr:safl/nosi/debian-base:latest")
    assert resolved.digest == "sha256:" + "aa" * 32
    assert resolved.size == 1923658046
    assert resolved.title == "nosi-debian-base-x86_64.img.gz"
    assert resolved.blob_url == f"https://ghcr.io/v2/safl/nosi/debian-base/blobs/sha256:{'aa' * 32}"
    assert resolved.headers == {"Authorization": "Bearer anon-token-xyz"}


def test_resolve_ref_digest_skips_manifest() -> None:
    """Pre-pinned references should NOT call the manifest endpoint --
    the digest is the address. Mock raises on /manifests/ URL access."""

    def _strict_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if "/manifests/" in url:
            raise AssertionError("digest-pinned ref should not fetch manifest")

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

        return _Resp(json.dumps({"token": "pinned-token"}).encode())

    digest = "sha256:" + "cd" * 32
    with patch("urllib.request.urlopen", _strict_urlopen):
        resolved = ghcr.resolve_ref(f"ghcr:safl/nosi/debian-base@{digest}")
    assert resolved.digest == digest
    assert resolved.size is None  # unknown without the manifest
    assert resolved.title is None


def test_resolve_ref_propagates_token_failure() -> None:
    def _failing_urlopen(req, timeout=None):
        raise OSError("network unreachable")

    with (
        patch("urllib.request.urlopen", _failing_urlopen),
        pytest.raises(ghcr.GhcrError, match="token fetch failed"),
    ):
        ghcr.resolve_ref("ghcr:safl/nosi/debian-base:latest")


def test_is_ghcr_url() -> None:
    assert ghcr.is_ghcr_url("ghcr:safl/nosi/debian-base:latest")
    assert not ghcr.is_ghcr_url("https://ghcr.io/v2/safl/nosi/debian-base/blobs/x")
    assert not ghcr.is_ghcr_url("https://example.invalid/x.img.gz")
