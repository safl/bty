"""Fetch live-env boot artifacts from GitHub releases.

Operators populate ``BTY_BOOT_DIR`` (default ``/var/lib/bty/boot/``)
via the browser UI's "fetch latest release" action. This module hits
the predictable release-asset URLs

    https://github.com/<repo>/releases/latest/download/<asset>

(and the analogous ``/releases/download/<tag>/<asset>`` form) so we
don't need a GitHub token, an API client, or rate-limit handling for
public repos.

Download is atomic: artifacts land in a tempdir, the sha256 manifest
is verified, and only then are they renamed into ``boot_dir``. A
mid-fetch failure leaves the existing artifacts (if any) untouched.

The HTTP layer is injectable via ``base_url`` so tests can point at a
local ``http.server`` instead of github.com.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Names match the artifacts ``bty-media``'s ``live`` variant publishes
# (see ``bty-media/scripts/live_build.py::PUBLISH_BASENAMES``).
ARTIFACT_NAMES: tuple[str, ...] = (
    "bty-live-x86_64.vmlinuz",
    "bty-live-x86_64.initrd",
    "bty-live-x86_64.squashfs",
)
SHA256_NAME = "bty-live-x86_64.sha256"
ALL_NAMES = (*ARTIFACT_NAMES, SHA256_NAME)

DEFAULT_REPO = "safl/bty"
DEFAULT_USER_AGENT = "bty-web release-fetcher"


@dataclass(frozen=True)
class ArtifactState:
    """One row in the ``inspect_boot_dir`` result."""

    name: str
    present: bool
    size: int | None
    mtime: datetime | None


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a successful ``fetch_release`` call."""

    base_url: str
    artifacts: tuple[str, ...]  # filenames written
    total_bytes: int


class FetchError(Exception):
    """Wraps an upstream failure (network / HTTP / verification)."""


def inspect_boot_dir(boot_dir: Path) -> list[ArtifactState]:
    """Return the present/missing state of each expected artifact."""
    out: list[ArtifactState] = []
    for name in ALL_NAMES:
        path = boot_dir / name
        if path.is_file():
            stat = path.stat()
            out.append(
                ArtifactState(
                    name=name,
                    present=True,
                    size=stat.st_size,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        else:
            out.append(ArtifactState(name=name, present=False, size=None, mtime=None))
    return out


def fetch_release(
    boot_dir: Path,
    *,
    repo: str | None = None,
    tag: str = "latest",
    base_url: str | None = None,
) -> FetchResult:
    """Download all expected artifacts for ``tag`` into ``boot_dir``.

    ``base_url`` overrides the GitHub URL construction (used by tests
    to point at a local ``http.server`` instead of github.com).
    Raises :class:`FetchError` on any failure; on success the boot dir
    is bit-for-bit identical to the release's manifest.
    """
    repo = repo or os.environ.get("BTY_BOOT_RELEASE_REPO") or DEFAULT_REPO
    if base_url is None:
        if tag == "latest":
            base_url = f"https://github.com/{repo}/releases/latest/download"
        else:
            base_url = f"https://github.com/{repo}/releases/download/{tag}"
    base_url = base_url.rstrip("/")

    total = 0
    with tempfile.TemporaryDirectory(prefix="bty-boot-") as tmp:
        tmp_path = Path(tmp)
        for name in ALL_NAMES:
            url = f"{base_url}/{name}"
            try:
                total += _stream(url, tmp_path / name)
            except urllib.error.HTTPError as exc:
                raise FetchError(f"GET {url} returned HTTP {exc.code} {exc.reason}") from exc
            except urllib.error.URLError as exc:
                raise FetchError(f"GET {url} failed: {exc.reason}") from exc
            except OSError as exc:
                raise FetchError(f"GET {url} failed: {exc}") from exc

        try:
            _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc

        # Atomic install: rename each artifact into the live boot_dir
        # only after the manifest has verified.
        boot_dir.mkdir(parents=True, exist_ok=True)
        for name in ALL_NAMES:
            (tmp_path / name).replace(boot_dir / name)

    return FetchResult(base_url=base_url, artifacts=ALL_NAMES, total_bytes=total)


def _stream(url: str, dest: Path) -> int:
    """Stream ``url`` to ``dest`` in 1 MiB chunks; return bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    written = 0
    with urllib.request.urlopen(req) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return written


def _verify_sha256_manifest(manifest_path: Path, files_dir: Path) -> None:
    """Verify each entry in the ``sha256sum``-format manifest.

    Raises :class:`ValueError` if a file is missing, the manifest is
    malformed, or any digest mismatches.
    """
    text = manifest_path.read_text()
    seen = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # ``sha256sum`` output: ``<64-hex-digest>  <filename>`` (two spaces).
        # Be tolerant of any whitespace separator.
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"malformed line in {SHA256_NAME}: {line!r}")
        digest_expected, name = parts
        # Manifests sometimes prefix names with "*" (binary mode marker)
        # or "./"; strip both.
        name = name.lstrip("*./")
        if name == SHA256_NAME:
            continue  # the manifest does not include itself
        target = files_dir / name
        if not target.is_file():
            raise ValueError(f"manifest references missing file: {name}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual.lower() != digest_expected.lower():
            raise ValueError(
                f"sha256 mismatch for {name}: expected {digest_expected}, got {actual}"
            )
        seen += 1
    if seen == 0:
        raise ValueError(f"empty {SHA256_NAME} manifest")
