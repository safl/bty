"""Image catalog discovery and inspection.

Recognises the supported on-disk image formats (``.qcow2``, ``.img``,
``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``), lists them
under a configured image root, and extracts detail metadata for
individual images via the appropriate tool (``qemu-img info`` for
qcow2, ``zstd -l`` / ``xz -l`` / ``gzip -l`` for the corresponding
compressed raws; bzip2 has no listing tool so .img.bz2 has no
detail block).

Format-choice rationale: bty-shipped images all use **gzip** for
universal flasher / OS / tooling support. The flash code accepts
**any** of ``.img``, ``.img.zst``, ``.img.xz``, ``.img.gz``,
``.img.bz2`` for operator-supplied images so format choice is
not forced on operators with their own pipelines.

- The **USB stick image** ships as ``.iso.gz``. Operators write
  it host-side via Etcher / Rufus / Raspberry Pi Imager, which
  decompress .gz natively (xz tripped Etcher's bundled
  decompressor regardless of how the file was shaped; gzip has
  no equivalent quirk). Stick prep is a one-shot, host-side cost.
- The **server appliance images** ship as ``.img.gz``
  (``bty-server-x86_64.img.gz``,
  ``bty-server-rpi-arm64.img.gz``). The earlier rationale that
  drove .img.zst here -- "flash-time decompression is on the hot
  path of per-job CI reflash" -- conflated two different cases:
  the per-job reflash hot path applies to operator-supplied
  target images (any of the 4 compressed forms work), NOT to
  the bty-server appliance itself, which is flashed once during
  initial setup. Universal flasher compat wins for one-shot
  setup; the speed advantage of zstd was buying nothing for the
  bty-shipped artifacts.
- Operators running per-job CI reflash on a fast disk can pick
  ``.img.zst`` for their own images and the flash code will
  stream-decompress at zstd's ~800-1500 MB/s. zstd's only
  downside is the version-cliff in some host-side flasher
  ecosystems, which doesn't apply to bty's flash code -- it
  shells out to the system ``zstd`` binary, which is universal
  on Linux.
- Decompression speed ranking (rough): zstd > gzip > xz > bzip2.
  Pick based on workload: gzip for one-shot delivery, zstd for
  hot-path reflash.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Default image root. Operators override via ``--image-root`` or the
# ``BTY_IMAGE_ROOT`` environment variable. The USB live appliance mounts
# the BTY_IMAGES partition here.
DEFAULT_IMAGE_ROOT = Path("/var/lib/bty/images")

# Supported extensions, ordered most-specific first so multi-suffix
# variants (``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``)
# win over the bare ``.img``.
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    (".img.zst", "img.zst"),
    (".img.xz", "img.xz"),
    (".img.gz", "img.gz"),
    (".img.bz2", "img.bz2"),
    (".qcow2", "qcow2"),
    (".img", "img"),
)

# Extensions explicitly NOT supported by the single-stream flash
# pipeline. Tarballs wrap the actual image inside per-file headers;
# decompressing the gzip/xz layer doesn't yield raw image bytes,
# it yields a tar stream. dd'ing that into a target disk would
# write tar headers into the MBR. Operators with these files must
# extract first (``tar -xzf foo.tar.gz`` etc.) and drop the
# resulting .img onto BTY_IMAGES.
_TARBALL_HINT_EXTS: tuple[str, ...] = (
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tgz",
    ".txz",
    ".tbz2",
    ".tzst",
)


def is_tarball_extension(name: str) -> bool:
    """Return True if ``name`` looks like a tar archive that bty
    cannot flash directly (caller should hint the operator to
    extract first)."""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _TARBALL_HINT_EXTS)


@dataclass(frozen=True)
class Image:
    """A discovered image file. Plain bytes-on-disk metadata only.

    ``sha256`` is the lower-case hex SHA-256 of the image bytes when
    a cached value is available (sidecar ``.sha256`` file or
    in-memory). ``None`` means "not yet computed" -- callers that
    need it (machine binding, manifest cross-ref) call
    :func:`ensure_sha256` to materialise it lazily.
    """

    name: str
    path: Path
    format: str
    size_bytes: int
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def default_image_root() -> Path:
    """Resolve the configured image root.

    Precedence: ``BTY_IMAGE_ROOT`` env var, then ``DEFAULT_IMAGE_ROOT``.
    """
    env = os.environ.get("BTY_IMAGE_ROOT")
    return Path(env) if env else DEFAULT_IMAGE_ROOT


def detect_format(path: Path) -> str | None:
    """Return the image format identifier for ``path``, or ``None``."""
    name = path.name.lower()
    for ext, fmt in _EXTENSIONS:
        if name.endswith(ext):
            return fmt
    return None


def list_images(root: Path) -> list[Image]:
    """List supported images directly under ``root`` (non-recursive).

    Reads any cached SHA from the sidecar ``<file>.sha256`` if
    present (cheap; the operator may have written it themselves
    or a prior :func:`ensure_sha256` call did). Does NOT compute
    SHA on the fly -- multi-GiB hashing on every catalog list
    would be punishing. Callers that need the SHA call
    :func:`ensure_sha256` for the entries that matter.
    """
    if not root.exists() or not root.is_dir():
        return []

    out: list[Image] = []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        # Skip sidecar files; they're not images themselves.
        if p.name.endswith(".sha256"):
            continue
        fmt = detect_format(p)
        if fmt is None:
            continue
        out.append(
            Image(
                name=p.name,
                path=p,
                format=fmt,
                size_bytes=p.stat().st_size,
                sha256=_read_sidecar_sha(p),
            )
        )
    return out


# bty Remote Image (.bri) descriptor file extension. A tiny TOML
# file the operator drops into BTY_IMAGES to advertise an image
# that lives on the network rather than on the local filesystem.
# Mailable / Slackable: an operator can attach a ``.bri`` to a
# message and the recipient drops it into their own BTY_IMAGES to
# get the same flashable entry. Used at bty-usb bake time to ship
# a default pointer at the latest bty-server release on GitHub.
BRI_EXTENSION = ".bri"


@dataclass(frozen=True)
class RemoteImage:
    """A bty Remote Image (.bri) descriptor.

    Loaded from a tiny TOML file. The descriptor file path itself
    lives on the local filesystem under BTY_IMAGES; the *image
    bytes* it points at live at ``url``. Same role as :class:`Image`
    but for over-the-network sources, so the catalog UI can show
    "fetchable" images alongside local ones.

    Required fields: ``url`` (the bytes' upstream HTTP(S) location).
    Everything else is optional with sensible defaults: ``name``
    falls back to the URL's last path segment, ``format`` to the
    extension-derived format, ``size_bytes`` and ``sha256`` stay
    ``None`` until the operator (or a bty-side fetcher) materialises
    them.
    """

    name: str
    url: str
    path: Path  # the .bri descriptor file's location on disk
    format: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "path": str(self.path),
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "description": self.description,
        }


class BriError(Exception):
    """Raised when a ``.bri`` descriptor fails to parse or validate.
    Distinct from generic exceptions so callers (CLI / TUI / tests)
    can surface a friendly per-file error without crashing the whole
    listing."""


def _name_from_url(url: str) -> str:
    """Derive a display name from a URL by taking its last path
    segment. ``https://host/path/foo.img.gz`` -> ``foo.img.gz``.
    Empty or trailing-slash paths fall back to the netloc so the
    operator at least sees *something* identifiable."""
    parsed = urlparse(url)
    last = parsed.path.rsplit("/", 1)[-1]
    return last or parsed.netloc or url


def read_bri(path: Path) -> RemoteImage:
    """Parse one ``.bri`` TOML file into a :class:`RemoteImage`.
    Raises :class:`BriError` on missing required fields or bad TOML.
    """
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise BriError(f"{path}: not valid TOML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BriError(f"{path}: top-level must be a table")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise BriError(f"{path}: missing required field: url")
    url = url.strip()

    name = raw.get("name")
    if name is None:
        name = _name_from_url(url)
    elif not isinstance(name, str):
        raise BriError(f"{path}: name must be a string")

    fmt = raw.get("format")
    if fmt is None:
        # Try to infer from URL last path segment so "img.gz",
        # "iso.gz", etc. flow through the same detect_format logic
        # used for local files.
        fmt = detect_format(Path(_name_from_url(url)))
    elif not isinstance(fmt, str):
        raise BriError(f"{path}: format must be a string")

    size_bytes = raw.get("size_bytes")
    if size_bytes is not None and not isinstance(size_bytes, int):
        raise BriError(f"{path}: size_bytes must be an integer")

    sha = raw.get("sha256")
    if sha is not None:
        if not isinstance(sha, str):
            raise BriError(f"{path}: sha256 must be a string")
        sha = sha.strip().lower()
        if len(sha) != 64 or not all(c in _SHA_HEX for c in sha):
            raise BriError(f"{path}: sha256 must be a 64-char lower-case hex string")

    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise BriError(f"{path}: description must be a string")

    return RemoteImage(
        name=str(name),
        url=url,
        path=path,
        format=fmt,
        size_bytes=size_bytes,
        sha256=sha,
        description=description,
    )


def list_remote_images(root: Path) -> list[RemoteImage]:
    """List ``.bri`` descriptor files directly under ``root``
    (non-recursive).

    Mirrors :func:`list_images`'s shape but for the remote half of
    the catalog. Malformed ``.bri`` files are silently skipped so
    one bad descriptor doesn't break the whole listing -- a
    standalone ``bty bri inspect`` (TODO) is the place to surface
    parse errors loudly.
    """
    if not root.exists() or not root.is_dir():
        return []
    out: list[RemoteImage] = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.suffix != BRI_EXTENSION:
            continue
        try:
            out.append(read_bri(p))
        except BriError:
            continue
    return out


# Where the live env (and any installed bty package) ships built-in
# ``.bri`` descriptors. Distinct from BTY_IMAGES (the operator's
# catalog) so the bake-time bootstrap pointers don't get hidden by
# the operator's read-only exFAT mount over /var/lib/bty/images.
# Operators can drop their own ``.bri`` into BTY_IMAGES with the
# same name to override a system entry (operator wins).
DEFAULT_SYSTEM_BRI_ROOT = Path("/usr/share/bty/bri")


def system_bri_root() -> Path | None:
    """Resolve the system-wide ``.bri`` directory if present.

    Order: ``$BTY_SYSTEM_BRI_ROOT`` env var, else
    :data:`DEFAULT_SYSTEM_BRI_ROOT`. Returns ``None`` if neither
    exists -- a missing system bri root is not an error, just "no
    bake-time bootstrap pointers shipped".
    """
    candidate = Path(os.environ.get("BTY_SYSTEM_BRI_ROOT", str(DEFAULT_SYSTEM_BRI_ROOT)))
    return candidate if candidate.is_dir() else None


def list_all_remote_images(image_root: Path) -> list[RemoteImage]:
    """Operator-supplied + system-shipped ``.bri`` descriptors,
    merged. Used by the CLI and TUI catalog so the bty-server
    bootstrap pointer (shipped under ``/usr/share/bty/bri/``) is
    visible on a fresh USB stick boot, alongside whatever the
    operator dropped into BTY_IMAGES.

    Dedupe rule: operator entries win on filename collision -- if
    the operator dropped their own ``bty-server.bri`` into
    BTY_IMAGES, the system one is hidden so the operator can pin
    a specific release URL without editing the rootfs.
    """
    primary = list_remote_images(image_root)
    sys_root = system_bri_root()
    if sys_root is None or sys_root == image_root:
        return primary
    seen_names = {r.path.name for r in primary}
    extras = [r for r in list_remote_images(sys_root) if r.path.name not in seen_names]
    return primary + extras


def _sidecar_path(image_path: Path) -> Path:
    """Where the SHA-256 sidecar for ``image_path`` lives.

    Convention: ``foo.img.zst`` -> ``foo.img.zst.sha256``. Matches
    the sha256sum-style sidecar most release artifacts ship with
    so an operator can verify manually:

        sha256sum -c foo.img.zst.sha256
    """
    return image_path.with_name(image_path.name + ".sha256")


_SHA_HEX = frozenset("0123456789abcdef")


def _read_sidecar_sha(image_path: Path) -> str | None:
    """Read a sidecar ``<file>.sha256`` if present + parseable.

    Tolerates two common shapes:

      * Just the hex digest on one line (``abc123...``).
      * ``sha256sum`` output: ``abc123...  filename`` (we take
        the first whitespace-separated token).

    Returns ``None`` (not an error) if the sidecar is missing,
    unreadable, or the digest doesn't look like a 64-char lower-
    case hex string. The caller will treat None as "not yet
    computed" and fall back to :func:`ensure_sha256` if it cares.
    """
    sidecar = _sidecar_path(image_path)
    try:
        head = sidecar.read_text().strip().split(maxsplit=1)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    if not head:
        return None
    digest = head[0].strip().lower()
    if len(digest) != 64 or not all(c in _SHA_HEX for c in digest):
        return None
    return digest


@dataclass(frozen=True)
class ImageSource:
    """One way to obtain an image's bytes.

    ``kind`` distinguishes between an on-disk file (``"local"``,
    ``location`` is an absolute filesystem path) and a manifest
    entry (``"manifest"``, ``location`` is the upstream HTTP URL).
    A single :class:`UnifiedImage` may carry multiple sources --
    the same SHA-256 could be present locally AND declared in the
    catalog manifest, in which case both sources are listed and
    flash code is free to pick whichever is nearest.
    """

    kind: str  # "local" | "manifest"
    location: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": self.location}


@dataclass(frozen=True)
class UnifiedImage:
    """SHA-keyed image record. Merges directory-scan + catalog
    manifest entries that share a content hash so the API / UI /
    machine bindings see one row per actual image, not one per
    name-where-it-was-found.

    ``sha256`` is the durable identity (None for an
    unhashed-dir-scan-only entry the operator hasn't yet
    materialised; the row exists so the operator can find it +
    trigger hashing, but it cannot be bound to a machine until
    the SHA is computed). ``names`` collects every label the
    image goes by -- typically one (filename or manifest entry
    name), occasionally two when a dir-scan file's SHA matches a
    manifest entry. ``sources`` lists every fetch path; ``cached``
    is True if either a local file exists or the content-addressed
    cache holds the SHA.
    """

    sha256: str | None
    names: tuple[str, ...]
    format: str | None
    size_bytes: int | None
    sources: tuple[ImageSource, ...]
    cached: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "names": list(self.names),
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sources": [s.to_dict() for s in self.sources],
            "cached": self.cached,
        }


def merge_with_catalog(
    image_root: Path,
    manifest_entries: Iterable[Any],
    cache_dir: Path,
) -> list[UnifiedImage]:
    """Build the SHA-keyed unified image listing.

    Inputs:

      * ``image_root``: directory scanned via :func:`list_images`.
        Files with a sidecar ``<file>.sha256`` get their SHA
        populated; files without remain unhashed (sha256=None
        in the result).
      * ``manifest_entries``: iterable of
        ``bty.catalog.CatalogEntry`` objects (passed by structural
        type so this module does not import ``bty.catalog`` --
        keeps the dependency graph one-directional: ``bty.catalog``
        knows about ``bty.images``, never the reverse).
      * ``cache_dir``: where the content-addressed cache lives
        (``${BTY_STATE_DIR}/cache``). Used to determine ``cached``
        for SHAs that are NOT present as a local file.

    Merge rule: directory-scan images and manifest entries with
    the same SHA-256 collapse into one ``UnifiedImage`` whose
    ``names`` and ``sources`` arrays contain both sides. SHAs
    seen only in one source produce single-name single-source
    entries. Unhashed dir-scan files get one entry each, keyed
    by name (no SHA available to dedupe).
    """
    by_sha: dict[str, UnifiedImage] = {}
    unhashed: list[UnifiedImage] = []

    # Pass 1: directory scan.
    for img in list_images(image_root):
        local = ImageSource(kind="local", location=str(img.path))
        if img.sha256 is None:
            unhashed.append(
                UnifiedImage(
                    sha256=None,
                    names=(img.name,),
                    format=img.format,
                    size_bytes=img.size_bytes,
                    sources=(local,),
                    cached=True,  # the local file IS its own cache
                )
            )
            continue
        existing = by_sha.get(img.sha256)
        if existing is None:
            by_sha[img.sha256] = UnifiedImage(
                sha256=img.sha256,
                names=(img.name,),
                format=img.format,
                size_bytes=img.size_bytes,
                sources=(local,),
                cached=True,
            )
        else:
            # Multiple local files with the same SHA (rare but
            # possible if the operator copied an image). Merge.
            new_names = (
                existing.names if img.name in existing.names else (*existing.names, img.name)
            )
            by_sha[img.sha256] = UnifiedImage(
                sha256=existing.sha256,
                names=new_names,
                format=existing.format or img.format,
                size_bytes=existing.size_bytes or img.size_bytes,
                sources=(*existing.sources, local),
                cached=True,
            )

    # Pass 2: catalog manifest entries.
    for entry in manifest_entries:
        manifest_src = ImageSource(kind="manifest", location=str(entry.src))
        cache_hit = (cache_dir / entry.sha256).is_file()
        existing = by_sha.get(entry.sha256)
        if existing is None:
            by_sha[entry.sha256] = UnifiedImage(
                sha256=entry.sha256,
                names=(entry.name,),
                format=entry.format,
                size_bytes=entry.size_bytes,
                sources=(manifest_src,),
                cached=cache_hit,
            )
        else:
            new_names = (
                existing.names if entry.name in existing.names else (*existing.names, entry.name)
            )
            by_sha[entry.sha256] = UnifiedImage(
                sha256=existing.sha256,
                names=new_names,
                format=existing.format or entry.format,
                size_bytes=existing.size_bytes or entry.size_bytes,
                sources=(*existing.sources, manifest_src),
                # Cached if EITHER a local file exists (already
                # marked True in pass 1) OR the content-addressed
                # cache holds the SHA.
                cached=existing.cached or cache_hit,
            )

    # Stable order: SHA-keyed entries by first name, then unhashed
    # dir-scan tail also by name, so the UI / CLI / API output is
    # deterministic across runs.
    sha_keyed = sorted(by_sha.values(), key=lambda u: u.names[0])
    unhashed.sort(key=lambda u: u.names[0])
    return sha_keyed + unhashed


class HashCancelled(Exception):
    """Raised by :func:`ensure_sha256` when a caller-supplied
    cancel callback returns ``True`` between chunks. Distinct
    from generic exceptions so the hash manager can translate
    cleanly into ``status="cancelled"``."""


HashProgressCallback = "Callable[[int, int], None]"
"""Signature: ``progress(bytes_hashed, total_bytes)``. Called once
per chunk processed; ``total_bytes`` is the file's pre-hash size
(``Path.stat().st_size``)."""

HashCancelCheck = "Callable[[], bool]"
"""Signature: ``cancel() -> bool``. Polled between chunks; returning
``True`` raises :class:`HashCancelled`. Same shape as the cancel
callback :func:`bty.catalog.fetch_to_cache` accepts."""


def ensure_sha256(
    image_path: Path,
    *,
    chunk_size: int = 1 << 20,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> str:
    """Return the SHA-256 of ``image_path``, computing + caching
    if not already cached.

    Read order:

      1. Sidecar ``<file>.sha256`` -- O(1).
      2. Otherwise: stream the file through ``hashlib.sha256``
         (~60s per 8 GiB on a typical NVMe; minutes on a Pi /
         old NUC). Write the resulting digest to the sidecar so
         the next call is O(1).

    The sidecar is written atomically (write to ``.tmp``,
    ``os.replace``) so a crash during compute doesn't leave a
    half-written file masquerading as a valid sidecar.

    Optional callbacks (same contract as
    :func:`bty.catalog.fetch_to_cache`):

    - ``progress(downloaded, total)`` is called once per chunk
      processed; ``total`` is the file size from ``stat()``.
    - ``cancel()`` is polled between chunks; returning ``True``
      raises :class:`HashCancelled`. The hash manager wires
      this to a ``threading.Event`` so the operator can cancel
      a running hash from the UI.
    """
    cached = _read_sidecar_sha(image_path)
    if cached is not None:
        if progress is not None:
            size = image_path.stat().st_size
            progress(size, size)
        return cached
    import hashlib

    total = image_path.stat().st_size
    digest = hashlib.sha256()
    hashed = 0
    if progress is not None:
        progress(0, total)
    with image_path.open("rb") as fh:
        while True:
            if cancel is not None and cancel():
                raise HashCancelled("hash cancelled by caller")
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            hashed += len(chunk)
            if progress is not None:
                progress(hashed, total)
    hex_digest = digest.hexdigest()
    sidecar = _sidecar_path(image_path)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    tmp.write_text(f"{hex_digest}  {image_path.name}\n")
    os.replace(tmp, sidecar)
    return hex_digest


def inspect_image(path: Path) -> dict[str, Any]:
    """Return detailed metadata for a single image file.

    Always includes ``path``, ``format``, and ``size_bytes``. Adds a
    format-specific ``detail`` block when the relevant tool succeeds:

    - ``qcow2`` -> the JSON output of ``qemu-img info --output=json``
    - ``img.zst`` -> the textual output of ``zstd -l``
    - ``img.xz`` -> the textual output of ``xz -l``
    - ``img.gz`` -> the textual output of ``gzip -l``
    - ``img.bz2`` -> nothing (bzip2 has no listing tool)
    """
    if not path.exists():
        raise FileNotFoundError(path)

    fmt = detect_format(path)
    info: dict[str, Any] = {
        "path": str(path),
        "format": fmt,
        "size_bytes": path.stat().st_size,
    }

    if fmt == "qcow2":
        proc = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = json.loads(proc.stdout)
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.zst":
        proc = subprocess.run(
            ["zstd", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.xz":
        proc = subprocess.run(
            ["xz", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.gz":
        proc = subprocess.run(
            ["gzip", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    # img.bz2: no listing tool ships with bzip2; ``detail`` block
    # is intentionally omitted.

    return info
