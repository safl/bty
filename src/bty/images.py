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
  ``bty-server-rpi-arm64.img.gz``). Universal flasher compat
  wins for one-shot setup; zstd's flash-time-decompression edge
  is irrelevant for the bty-server appliance itself, which is
  flashed once during initial setup -- the per-job reflash hot
  path applies to operator-supplied target images, not to
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

# Default image root. Operators override via the ``BTY_IMAGE_ROOT``
# environment variable. The USB live appliance mounts the BTY_IMAGES
# partition here.
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
        # Symlinks could point outside ``root``; the bytes would
        # then be served via ``GET /images/<sha>`` even though
        # they live outside the operator-configured image root.
        # Reject symlinks defensively -- operators who really
        # want to share files across roots can copy or hardlink.
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        # Skip sidecar files; they're not images themselves.
        if p.name.endswith(".sha256"):
            continue
        fmt = detect_format(p)
        if fmt is None:
            continue
        # ``stat`` can race with concurrent unlink (operator drops
        # a file out of BTY_IMAGES between iterdir and stat).
        # Skip rather than crash the listing.
        try:
            size_bytes = p.stat().st_size
        except FileNotFoundError:
            continue
        out.append(
            Image(
                name=p.name,
                path=p,
                format=fmt,
                size_bytes=size_bytes,
                sha256=_read_sidecar_sha(p),
            )
        )
    return out


def _sidecar_path(image_path: Path) -> Path:
    """Where the SHA-256 sidecar for ``image_path`` lives.

    Convention: ``foo.img.zst`` -> ``foo.img.zst.sha256``. Matches
    the sha256sum-style sidecar most release artifacts ship with
    so an operator can verify manually:

        sha256sum -c foo.img.zst.sha256
    """
    return image_path.with_name(image_path.name + ".sha256")


SHA256_HEX_LEN = 64
_SHA_HEX = frozenset("0123456789abcdef")


def is_sha256_hex(s: str) -> bool:
    """Return ``True`` iff ``s`` is a lower-case 64-char SHA-256
    hex digest. Single predicate shared by sidecar parsing,
    manifest validation, and the URL-key dispatch in bty-web.
    """
    return len(s) == SHA256_HEX_LEN and all(c in _SHA_HEX for c in s)


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
        head = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    if not head:
        return None
    digest = head[0].strip().lower()
    if not is_sha256_hex(digest):
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
    """Image record on the merged listing.

    Two identity fields, distinct on purpose:

    ``ref`` is the **provenance ID** -- ``sha256(canonicalise_src(src))``,
    a deterministic 64-hex digest of the canonical form of the source URL.
    Populated for every entry the merge produces (dir-scan files get
    ``src="file://<rel-path>"``; catalog entries get their declared
    ``src``). This is THE value machine bindings target -- a rolling
    oras tag's ref stays stable across re-pushes, so binding to a tag
    survives the next rebuild upstream. Always non-empty.

    ``sha256`` is the **observed content hash**. May be None for a
    rolling manifest entry that has never been cached, a dir-scan
    file lacking a ``.sha256`` sidecar, or an operator-added URL
    without a ``sha_url``. Back-fills on first cache / hash. Distinct
    from ``ref`` -- the same content can land under
    multiple refs (e.g. operator catalogs the same image under
    ``oras://a`` and ``http://b``), and the same ref can map to
    different content over time (rolling tag re-push).

    Merge collapse rule: same content-sha or same ref collapse into
    one entry; otherwise distinct. See :func:`merge_with_catalog`.

    ``names`` collects every label the image goes by; ``sources``
    every fetch path; ``cached`` is True iff a local file exists or
    the content-addressed cache holds the SHA.
    """

    ref: str
    sha256: str | None
    names: tuple[str, ...]
    format: str | None
    size_bytes: int | None
    sources: tuple[ImageSource, ...]
    cached: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
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
) -> list[UnifiedImage]:
    """Build the unified image listing.

    Inputs:

      * ``image_root``: directory scanned via :func:`list_images`.
        Files with a sidecar ``<file>.sha256`` get their SHA
        populated; files without remain unhashed (sha256=None
        on the resulting :class:`UnifiedImage`). v0.31.0+: also
        contains catalog-fetched files named
        ``catalog-<ref:12>-<slug>.<ext>``, treated identically to
        operator-typed files in pass 1 (their presence under the
        URL-keyed name implies cached=True for the matching catalog
        manifest entry in pass 2).
      * ``manifest_entries``: iterable of catalog entries
        (``bty.catalog.CatalogEntry`` or anything structurally
        equivalent: ``name`` / ``src`` / ``sha256`` / ``format`` /
        ``size_bytes`` / ``description``). Passed by structural
        type so this module avoids importing ``bty.catalog`` at
        module load; the local import for :func:`image_ref_for_src`
        below resolves only at call time, after the catalog
        module is fully initialised.

    Merge rule -- two collapse axes, both applied:

      1. **Content identity** (``sha256``): entries whose observed
         content hash matches collapse into one ``UnifiedImage``.
         This is the "same image, multiple sources" case (e.g. a
         local file + a manifest entry that pins the same SHA).
      2. **Provenance identity** (``ref =
         sha256(canonicalise_src(src))``): entries that share a
         canonical src URL collapse into one ``UnifiedImage`` even
         when there is no content sha to key on. This is what
         covers rolling oras tags + ``releases/latest/download/``
         URLs in the default catalog (every entry has ``sha256 =
         None`` at first sight), plus the duplicate-auto-import
         shape where the manifest entry and its
         ``catalog_entries`` DB row appear under the same src.

    Without axis (2) every entry with no pinned ``sha256`` ended
    up in an "unhashed" tail with no dedup -- so a single fetch-
    latest click rendered each rolling-tag entry twice on
    /ui/images (once from the in-memory manifest, once from the
    auto-imported DB row). The previous behaviour matched the
    spec when every entry came with a content sha, which the
    well-pinned tests covered, but not the rolling-tag case the
    default catalog actually ships.

    """
    # Local import: ``bty.catalog`` imports ``bty.images`` at
    # module load, so a top-of-file import here would cycle. By
    # call time the catalog module is fully loaded, so this is
    # cheap and safe.
    from bty.catalog import image_ref_for_src

    by_sha: dict[str, UnifiedImage] = {}
    by_ref: dict[str, UnifiedImage] = {}

    def add_entry(
        ref: str,
        sha256: str | None,
        name: str,
        format_: str | None,
        size_bytes: int | None,
        source: ImageSource,
        cached: bool,
    ) -> None:
        """Insert or merge an entry into by_sha (if sha known) and
        by_ref (always). Same UnifiedImage instance lives in both
        dicts when a sha is present, so a later ref-keyed hit will
        update the by_sha entry in place via re-assignment.
        """
        # Prefer the existing record under EITHER key, in priority
        # order (sha first because content-identity is stronger).
        existing = (by_sha.get(sha256) if sha256 is not None else None) or by_ref.get(ref)
        if existing is None:
            new = UnifiedImage(
                ref=ref,
                sha256=sha256,
                names=(name,),
                format=format_,
                size_bytes=size_bytes,
                sources=(source,),
                cached=cached,
            )
            if sha256 is not None:
                by_sha[sha256] = new
            by_ref[ref] = new
            return
        # Merge into the existing entry. Promote sha256 if we now
        # know it and existing did not.
        merged_sha = existing.sha256 if existing.sha256 is not None else sha256
        merged_format = existing.format or format_
        merged_size = existing.size_bytes if existing.size_bytes is not None else size_bytes
        merged_names = existing.names if name in existing.names else (*existing.names, name)
        merged_sources = (
            existing.sources if source in existing.sources else (*existing.sources, source)
        )
        merged = UnifiedImage(
            ref=existing.ref,
            sha256=merged_sha,
            names=merged_names,
            format=merged_format,
            size_bytes=merged_size,
            sources=merged_sources,
            cached=existing.cached or cached,
        )
        by_ref[merged.ref] = merged
        if merged_sha is not None:
            by_sha[merged_sha] = merged

    # Pass 1: directory scan. Each file becomes one entry; its
    # provenance ref is computed from the ``file://<rel-path>``
    # form so it matches whatever the auto-import wrote into
    # ``catalog_entries``.
    for img in list_images(image_root):
        try:
            rel = img.path.relative_to(image_root)
        except ValueError:
            # Symlink escaped the root, or scan ran against a
            # remounted-mid-stream root. Skip rather than mint a
            # ref against an unrooted absolute path.
            continue
        src = "file://" + rel.as_posix()
        try:
            ref = image_ref_for_src(src)
        except ValueError:
            continue  # untranslatable src, e.g. invalid characters
        local = ImageSource(kind="local", location=str(img.path))
        add_entry(
            ref=ref,
            sha256=img.sha256,
            name=img.name,
            format_=img.format,
            size_bytes=img.size_bytes,
            source=local,
            cached=True,  # the local file IS its own cache
        )

    # Pass 2: catalog manifest entries (and any structurally-
    # equivalent records the caller fed in -- the web layer
    # passes operator-added catalog_entries rows here too).
    from bty.catalog import local_filename_for

    for entry in manifest_entries:
        try:
            ref = image_ref_for_src(entry.src)
        except ValueError:
            continue  # malformed src; skip
        manifest_src = ImageSource(kind="manifest", location=str(entry.src))
        # v0.31.0+: catalog-fetched files live in image_root under
        # ``catalog-<ref:12>-<slug>.<ext>``. Pass 1 already added them
        # as ``local`` sources; this lookup is the secondary path
        # when pass 1 hasn't run (e.g. an entry whose local file
        # hasn't been scanned yet) but the file is on disk.
        cached_filename = local_filename_for(ref, entry.name, entry.format)
        cache_hit = (image_root / cached_filename).is_file()
        add_entry(
            ref=ref,
            sha256=entry.sha256,
            name=entry.name,
            format_=entry.format,
            size_bytes=entry.size_bytes,
            source=manifest_src,
            cached=cache_hit,
        )

    # Stable order: first by content-sha-presence (so sha-pinned
    # entries land before unhashed ones, matching the prior
    # behaviour the UI and ``bty`` wizard expect), then by first
    # name within each bucket.
    unique = list(by_ref.values())
    with_sha = sorted((u for u in unique if u.sha256 is not None), key=lambda u: u.names[0])
    without_sha = sorted((u for u in unique if u.sha256 is None), key=lambda u: u.names[0])
    return with_sha + without_sha


class HashCancelled(Exception):
    """Raised by :func:`ensure_sha256` when a caller-supplied
    cancel callback returns ``True`` between chunks. Distinct
    from generic exceptions so the hash manager can translate
    cleanly into ``status="cancelled"``."""


HashProgressCallback: TypeAlias = Callable[[int, int], None]
"""Signature: ``progress(bytes_hashed, total_bytes)``. Called once
per chunk processed; ``total_bytes`` is the file's pre-hash size
(``Path.stat().st_size``)."""

HashCancelCheck: TypeAlias = Callable[[], bool]
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
    tmp.write_text(f"{hex_digest}  {image_path.name}\n", encoding="utf-8")
    os.replace(tmp, sidecar)
    return hex_digest


def _run_detail_tool(cmd: list[str], *, timeout: float = 30.0) -> tuple[str | None, str | None]:
    """Run a metadata-listing tool, returning ``(detail, error)``.

    Exactly one element is non-None: ``detail`` is the tool's
    stripped stdout on success, otherwise ``error`` carries the
    stderr (or a timeout note). Bounds the call with ``timeout`` so
    a hung tool (corrupt file, slow network mount) can't wedge an
    inspect request -- the same defensive shell-out pattern
    :mod:`bty.disks` and :mod:`bty.web._sysconfig` use.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{cmd[0]} timed out after {timeout:g}s"
    if proc.returncode == 0:
        return proc.stdout.strip(), None
    return None, proc.stderr.strip()


def _set_detail(info: dict[str, Any], detail: str | None, error: str | None) -> None:
    """Stash a text ``detail`` block or a ``detail_error`` onto the
    inspect result, depending on which :func:`_run_detail_tool`
    returned."""
    if detail is not None:
        info["detail"] = detail
    else:
        info["detail_error"] = error


def inspect_image(path: Path) -> dict[str, Any]:
    """Return detailed metadata for a single image file.

    Always includes ``path``, ``format``, and ``size_bytes``. Adds a
    format-specific ``detail`` block when the relevant tool succeeds:

    - ``qcow2`` -> the JSON output of ``qemu-img info --output=json``
    - ``img.zst`` -> the textual output of ``zstd -l``
    - ``img.xz`` -> the textual output of ``xz -l``
    - ``img.gz`` -> the textual output of ``gzip -l``
    - ``img.bz2`` -> nothing (bzip2 has no listing tool)

    Raises :class:`FileNotFoundError` if the path does not exist,
    :class:`IsADirectoryError` if the path is a directory (operator
    almost certainly meant a file inside; surfacing a "format='',
    size_bytes=40" record for a directory was misleading), or
    """
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir():
        raise IsADirectoryError(path)

    fmt = detect_format(path)
    info: dict[str, Any] = {
        "path": str(path),
        "format": fmt,
        "size_bytes": path.stat().st_size,
    }

    # Tarballs aren't flashable -- the inspection helper points
    # the operator at the right next step instead of returning a
    # blank ``format: ''`` record that looks like a bty bug.
    if fmt is None and is_tarball_extension(path.name):
        info["detail_error"] = (
            "tarball; not directly flashable -- extract first "
            f"(e.g. ``tar -xf {path.name}``) and drop the resulting "
            "``.img`` / ``.qcow2`` onto BTY_IMAGES"
        )
        return info

    # Any other unrecognised extension: same shape as the tarball
    # branch, just a generic "this isn't a format bty knows about"
    # message listing what IS supported. Without this, an inspect
    # against e.g. README.md returned a confusing blank record
    # with format=''.
    if fmt is None:
        supported = ", ".join(ext for ext, _ in _EXTENSIONS)
        info["detail_error"] = (
            f"unrecognised format for {path.name!r}; supported extensions: {supported}"
        )
        return info

    if fmt == "qcow2":
        detail, error = _run_detail_tool(["qemu-img", "info", "--output=json", str(path)])
        if detail is not None:
            # ``qemu-img info`` can exit 0 yet emit non-JSON (truncated
            # output, an image it half-understood). Treat a decode
            # failure as a detail error rather than crashing the
            # inspect request -- mirrors the guarded parse in
            # ``flash._image_virtual_size``.
            try:
                info["detail"] = json.loads(detail)
            except json.JSONDecodeError as exc:
                info["detail_error"] = f"qemu-img info returned unparseable JSON: {exc}"
        else:
            info["detail_error"] = error
    elif fmt == "img.zst":
        detail, error = _run_detail_tool(["zstd", "-l", str(path)])
        _set_detail(info, detail, error)
    elif fmt == "img.xz":
        detail, error = _run_detail_tool(["xz", "-l", str(path)])
        _set_detail(info, detail, error)
    elif fmt == "img.gz":
        detail, error = _run_detail_tool(["gzip", "-l", str(path)])
        _set_detail(info, detail, error)
    # img.bz2: no listing tool ships with bzip2; ``detail`` block
    # is intentionally omitted.

    return info
