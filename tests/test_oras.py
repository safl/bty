"""Tests for :mod:`bty.oras` -- the ORAS / OCI registry adapter.

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

from bty import oras

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
            "annotations": {"org.opencontainers.image.title": "nosi-debian-sysdev-x86_64.img.gz"},
        },
        {
            "mediaType": "text/plain",
            "digest": "sha256:" + "bb" * 32,
            "size": 130,
            "annotations": {
                "org.opencontainers.image.title": "nosi-debian-sysdev-x86_64.img.gz.sha256"
            },
        },
    ],
}


# ---------- parse_ref ---------------------------------------------------------


def test_parse_ref_tag_form() -> None:
    ref = oras.parse_ref("oras://ghcr.io/safl/nosi/debian-sysdev:latest")
    assert ref.host == "ghcr.io"
    assert ref.repository == "safl/nosi/debian-sysdev"
    assert ref.tag == "latest"
    assert ref.digest is None
    assert ref.manifest_locator == "latest"


def test_parse_ref_digest_form() -> None:
    digest = "sha256:" + "ab" * 32
    ref = oras.parse_ref(f"oras://ghcr.io/safl/nosi/debian-sysdev@{digest}")
    assert ref.host == "ghcr.io"
    assert ref.repository == "safl/nosi/debian-sysdev"
    assert ref.tag is None
    assert ref.digest == digest
    assert ref.manifest_locator == digest


def test_parse_ref_owner_repo_minimum() -> None:
    """Two-segment owner/repo under the host is the minimum; anything
    shorter (e.g. ``oras://ghcr.io/nosi:latest``) is rejected."""
    ref = oras.parse_ref("oras://ghcr.io/owner/repo:v1")
    assert ref.host == "ghcr.io"
    assert ref.repository == "owner/repo"


def test_parse_ref_accepts_host_with_port() -> None:
    """Private / on-prem registries often run on non-443 ports;
    the host parser should preserve ``host:port`` verbatim."""
    ref = oras.parse_ref("oras://registry.example.com:5000/foo/bar:v1")
    assert ref.host == "registry.example.com:5000"
    assert ref.repository == "foo/bar"


def test_parse_ref_rejects_bare_repo_after_host() -> None:
    """A path with no ``/`` after the host is not a valid OCI repository."""
    with pytest.raises(oras.OrasError, match=r"host.*owner.*repo"):
        oras.parse_ref("oras://ghcr.io/nosi:latest")


def test_parse_ref_rejects_missing_scheme() -> None:
    with pytest.raises(oras.OrasError, match="not an oras://"):
        oras.parse_ref("ghcr.io/safl/nosi:latest")


def test_parse_ref_rejects_empty_body() -> None:
    with pytest.raises(oras.OrasError, match="empty"):
        oras.parse_ref("oras://")


def test_parse_ref_rejects_missing_tag_and_digest() -> None:
    """Tagless / digestless refs aren't pullable; reject."""
    with pytest.raises(oras.OrasError, match="malformed"):
        oras.parse_ref("oras://ghcr.io/safl/nosi/debian-sysdev")


def test_parse_ref_rejects_short_digest() -> None:
    """sha256 digests must be 64 hex chars; partial digests would
    silently mis-address the blob."""
    with pytest.raises(oras.OrasError, match="malformed"):
        oras.parse_ref("oras://ghcr.io/safl/nosi/debian-sysdev@sha256:abc123")


# ---------- pick_image_layer --------------------------------------------------


def test_pick_image_layer_skips_sha256_sidecar() -> None:
    layer = oras.pick_image_layer(_NOSI_MANIFEST)
    title = layer["annotations"]["org.opencontainers.image.title"]
    assert title == "nosi-debian-sysdev-x86_64.img.gz"
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
    layer = oras.pick_image_layer(manifest)
    assert layer["size"] == 1_000_000


def test_pick_image_layer_raises_on_empty_layers() -> None:
    with pytest.raises(oras.OrasError, match="no layers"):
        oras.pick_image_layer({"layers": []})


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
    layer = oras.pick_image_layer(manifest)
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
        resolved = oras.resolve_ref("oras://ghcr.io/safl/nosi/debian-sysdev:latest")
    assert resolved.digest == "sha256:" + "aa" * 32
    assert resolved.size == 1923658046
    assert resolved.title == "nosi-debian-sysdev-x86_64.img.gz"
    assert (
        resolved.blob_url == f"https://ghcr.io/v2/safl/nosi/debian-sysdev/blobs/sha256:{'aa' * 32}"
    )
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
        resolved = oras.resolve_ref(f"oras://ghcr.io/safl/nosi/debian-sysdev@{digest}")
    assert resolved.digest == digest
    assert resolved.size is None  # unknown without the manifest
    assert resolved.title is None
    # Blob URL still builds correctly even without a manifest fetch.
    assert resolved.blob_url == f"https://ghcr.io/v2/safl/nosi/debian-sysdev/blobs/{digest}"


def test_resolve_ref_uses_host_from_url_in_token_endpoint() -> None:
    """Token endpoint URL must follow the URL's host, not be hardcoded
    to ghcr.io. Verifies cross-registry support stays intact even
    though only GHCR is exercised by the starter .bri set."""
    seen_urls: list[str] = []

    def _capturing_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        seen_urls.append(url)

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

        if "/token" in url:
            return _Resp(json.dumps({"token": "tok"}).encode())
        return _Resp(json.dumps(_NOSI_MANIFEST).encode())

    with patch("urllib.request.urlopen", _capturing_urlopen):
        oras.resolve_ref("oras://registry.example.com:5000/foo/bar:v1")

    assert any(url.startswith("https://registry.example.com:5000/token") for url in seen_urls), (
        f"expected token URL with custom host, saw: {seen_urls}"
    )


def test_resolve_ref_propagates_token_failure() -> None:
    def _failing_urlopen(req, timeout=None):
        raise OSError("network unreachable")

    with (
        patch("urllib.request.urlopen", _failing_urlopen),
        pytest.raises(oras.OrasError, match="token fetch failed"),
    ):
        oras.resolve_ref("oras://ghcr.io/safl/nosi/debian-sysdev:latest")


def test_is_oras_url() -> None:
    assert oras.is_oras_url("oras://ghcr.io/safl/nosi/debian-sysdev:latest")
    assert not oras.is_oras_url("https://ghcr.io/v2/safl/nosi/debian-sysdev/blobs/sha256:x")
    assert not oras.is_oras_url("https://example.invalid/x.img.gz")
    # The bare ``ghcr:`` scheme must NOT be recognised; oras refs
    # require the explicit ``oras://`` form.
    assert not oras.is_oras_url("ghcr:safl/nosi/debian-sysdev:latest")
