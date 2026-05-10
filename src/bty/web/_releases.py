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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

# Names match the artifacts the ``netboot-x86`` variant publishes
# (see ``cijoe/scripts/live_build.py::PUBLISH_BASENAMES``).
ARTIFACT_NAMES: tuple[str, ...] = (
    "bty-netboot-x86_64.vmlinuz",
    "bty-netboot-x86_64.initrd",
    "bty-netboot-x86_64.squashfs",
)
SHA256_NAME = "bty-netboot-x86_64.sha256"
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


class FetchCancelled(Exception):
    """Raised by :func:`fetch_release` when the supplied ``cancel``
    callback returns ``True`` between chunks. Distinct from
    :class:`FetchError` so callers can treat operator-cancellation
    as a normal control-flow signal, not an error condition."""


# Type aliases mirror :mod:`bty.catalog`'s ``fetch_to_cache`` shape so
# the manager wiring is structurally identical to DownloadManager /
# HashManager / ReleaseFetchManager.
FetchProgressCallback: TypeAlias = Callable[[int, "int | None"], None]
"""Signature: ``progress(bytes_done, total_bytes_or_None)``. Called
once per chunk written for the *currently-streaming* artefact.
``total_bytes`` is the upstream ``Content-Length`` if the server
sent one (most do), else ``None``."""

FetchCancelCheck: TypeAlias = Callable[[], bool]
"""Signature: ``cancel() -> bool``. Polled between chunks; returning
``True`` raises :class:`FetchCancelled`. Used with
``threading.Event.is_set`` so the manager (running the fetcher in
a worker thread) can abort from outside."""


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
    progress: FetchProgressCallback | None = None,
    cancel: FetchCancelCheck | None = None,
    on_artefact_start: Callable[[str], None] | None = None,
) -> FetchResult:
    """Download all expected artifacts for ``tag`` into ``boot_dir``.

    ``base_url`` overrides the GitHub URL construction (used by tests
    to point at a local ``http.server`` instead of github.com).
    Raises :class:`FetchError` on any failure; on success the boot dir
    is bit-for-bit identical to the release's manifest.

    ``progress(bytes_done, total)`` and ``cancel()`` enable the
    :class:`bty.web._release_mgr.ReleaseFetchManager` UI: progress
    is called per-chunk during each artefact's stream, and cancel
    is polled so the operator's "Cancel" button lands within
    seconds.

    ``on_artefact_start(name)`` is called once at the start of each
    individual artefact's stream so the manager can update the
    ``state.artefact`` field for the live UI. Without this hook
    the /ui/boot page rendered ``"... - N MiB / M MiB"`` because
    the artefact field stayed None across the whole multi-file
    fetch.
    """
    repo = repo or os.environ.get("BTY_BOOT_RELEASE_REPO") or DEFAULT_REPO
    if base_url is None:
        if tag == "latest":
            base_url = f"https://github.com/{repo}/releases/latest/download"
        else:
            base_url = f"https://github.com/{repo}/releases/download/{tag}"
    base_url = base_url.rstrip("/")

    # The tempdir lives *inside* ``boot_dir`` (rather than the system
    # default ``/tmp``) so the final ``Path.replace`` is a same-
    # filesystem rename. Crossing mounts (the prod layout has
    # ``/var/lib/bty`` on its own volume on cooked appliances)
    # raises ``OSError 18 Invalid cross-device link`` from the
    # rename syscall.
    boot_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    with tempfile.TemporaryDirectory(prefix="bty-boot-", dir=boot_dir) as tmp:
        tmp_path = Path(tmp)
        for name in ALL_NAMES:
            url = f"{base_url}/{name}"
            if on_artefact_start is not None:
                on_artefact_start(name)
            try:
                total += _stream(url, tmp_path / name, progress=progress, cancel=cancel)
            except FetchCancelled:
                raise
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
        # only after the manifest has verified. Same-filesystem
        # guaranteed by the ``dir=boot_dir`` above.
        for name in ALL_NAMES:
            (tmp_path / name).replace(boot_dir / name)

    return FetchResult(base_url=base_url, artifacts=ALL_NAMES, total_bytes=total)


def _stream(
    url: str,
    dest: Path,
    *,
    progress: FetchProgressCallback | None = None,
    cancel: FetchCancelCheck | None = None,
) -> int:
    """Stream ``url`` to ``dest`` in 1 MiB chunks; return bytes written.

    ``timeout=300`` so a flaky GitHub mirror (or a network blip mid-
    artefact) doesn't wedge the bty-web ``fetch latest release``
    action indefinitely.

    ``progress`` is called per-chunk with cumulative bytes written
    for *this artefact* (not the whole release); ``cancel`` is
    polled per-chunk and raises :class:`FetchCancelled` on True.
    """
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    written = 0
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
        try:
            cl = resp.headers.get("Content-Length")
            content_length: int | None = int(cl) if cl is not None else None
        except (ValueError, AttributeError):
            content_length = None
        if progress is not None:
            progress(0, content_length)
        while True:
            if cancel is not None and cancel():
                raise FetchCancelled("fetch cancelled by caller")
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            if progress is not None:
                progress(written, content_length)
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
        # Stream-hash instead of ``target.read_bytes()``: the squashfs
        # artefact alone is ~300 MiB; a Pi 4 / small NUC running
        # bty-web can OOM if N artefacts get fully buffered. 1 MiB
        # chunks keep peak memory bounded regardless of artefact
        # size; performance is identical to read_bytes() for small
        # files (a single chunk).
        h = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual.lower() != digest_expected.lower():
            raise ValueError(
                f"sha256 mismatch for {name}: expected {digest_expected}, got {actual}"
            )
        seen += 1
    if seen == 0:
        raise ValueError(f"empty {SHA256_NAME} manifest")
