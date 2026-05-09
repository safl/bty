"""bty catalog manifest with src URLs + content-addressed cache.

A catalog manifest (TOML, ``${BTY_STATE_DIR}/catalog.toml`` by
default) lists named images with upstream ``src`` URLs and pinned
``sha256`` digests:

.. code-block:: toml

    version = 1

    [[images]]
    name = "ubuntu-server-22.04-bty.img.zst"
    src = "https://github.com/safl/bty-images/releases/download/v0.1/ubuntu-22.04.img.zst"
    sha256 = "abc123..."
    format = "img.zst"

The fetcher downloads each ``src`` on demand, verifies SHA-256
against the manifest, and atomically writes into a content-
addressed cache (``${BTY_STATE_DIR}/cache/<sha256>``). The cache
key is the SHA itself so duplicate hashes across manifest entries
dedupe naturally; corrupted bytes never serve, since SHA mismatch
fails before the temp file is renamed into place.

Module is stdlib-only -- ``tomllib`` is in Python 3.11+ stdlib,
``hashlib`` / ``urllib`` / ``shutil`` are too. Importing this
module from the CLI does NOT pull in fastapi or textual.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Self, TypeAlias

from bty import images as _images

# Manifest schema version this implementation understands. We bump
# this when an incompatible change is made to the on-disk shape;
# older manifests then need a migration step or fail validation.
SCHEMA_VERSION = 1


class CatalogError(Exception):
    """Raised when a manifest fails to parse, validate, or fetch.

    Subclassed only when call sites need to discriminate (rare so
    far -- the CLI just prints the message).
    """


class CatalogCancelled(Exception):
    """Raised by :func:`fetch_to_cache` when a caller-supplied
    cancel callback returns ``True`` between chunks.

    Distinct from :class:`CatalogError` because cancellation is
    a normal control flow (the operator clicked Cancel), not an
    error condition. Callers that want to treat both alike can
    ``except (CatalogError, CatalogCancelled)``.
    """


@dataclass(frozen=True)
class CatalogEntry:
    """One image declared in a manifest.

    ``format`` defaults to whatever ``bty.images.detect_format``
    returns for ``name`` -- so a manifest entry named
    ``foo.img.zst`` doesn't need to repeat ``format = "img.zst"``.
    Operators can still set it explicitly to disambiguate or to
    name a format detection wouldn't infer (e.g. extension-less
    files served from a CDN).
    """

    name: str
    src: str
    sha256: str
    format: str | None = None
    size_bytes: int | None = None
    description: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        for required in ("name", "src", "sha256"):
            if required not in raw:
                raise CatalogError(
                    f"manifest entry missing required field: {required!r} (entry: {raw!r})"
                )
        sha = str(raw["sha256"]).strip().lower()
        if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
            raise CatalogError(
                f"manifest entry {raw['name']!r}: sha256 must be a 64-char "
                f"lower-case hex string, got {sha!r}"
            )
        return cls(
            name=str(raw["name"]),
            src=str(raw["src"]),
            sha256=sha,
            format=raw.get("format") or _images.detect_format(Path(raw["name"])),
            size_bytes=int(raw["size_bytes"]) if raw.get("size_bytes") is not None else None,
            description=raw.get("description"),
        )

    def cached_path(self, cache_dir: Path) -> Path:
        """Where this entry's bytes live once cached. Content-
        addressed by SHA so multiple entries pointing at the same
        upstream blob share one file."""
        return cache_dir / self.sha256


@dataclass(frozen=True)
class Catalog:
    """Parsed manifest. Wraps the entry list with a few lookup
    helpers; otherwise just a typed container.
    """

    version: int
    entries: tuple[CatalogEntry, ...] = field(default_factory=tuple)

    def by_name(self, name: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def __iter__(self) -> Iterator[CatalogEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def load(path: Path) -> Catalog:
    """Parse a TOML manifest into a :class:`Catalog`.

    Raises :class:`CatalogError` on missing files, malformed TOML,
    schema-version mismatch, missing required fields, or duplicate
    image names within the manifest.
    """
    if not path.exists():
        raise CatalogError(f"catalog manifest not found: {path}")
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"catalog manifest at {path} is not valid TOML: {exc}") from exc

    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise CatalogError(
            f"catalog manifest at {path}: version={version!r}, "
            f"this bty understands version={SCHEMA_VERSION}"
        )

    images_raw = raw.get("images", [])
    if not isinstance(images_raw, list):
        raise CatalogError(
            f"catalog manifest at {path}: ``images`` must be an array of tables, "
            f"got {type(images_raw).__name__}"
        )

    entries: list[CatalogEntry] = []
    seen_names: set[str] = set()
    for raw_entry in images_raw:
        if not isinstance(raw_entry, dict):
            raise CatalogError(
                f"catalog manifest at {path}: ``images`` entry must be a table, "
                f"got {type(raw_entry).__name__}"
            )
        entry = CatalogEntry.from_dict(raw_entry)
        if entry.name in seen_names:
            raise CatalogError(f"catalog manifest at {path}: duplicate image name {entry.name!r}")
        seen_names.add(entry.name)
        entries.append(entry)

    return Catalog(version=version, entries=tuple(entries))


def default_manifest_path() -> Path | None:
    """Resolve the manifest path from the environment.

    Order: ``$BTY_CATALOG_FILE`` (explicit path), else
    ``${BTY_STATE_DIR}/catalog.toml`` (default colocation). Returns
    ``None`` if no path is configured AND the default-colocated
    file does not exist -- so the absence of a catalog is not an
    error, it's just "no catalog configured".
    """
    explicit = os.environ.get("BTY_CATALOG_FILE")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.environ.get("BTY_STATE_DIR", "/var/lib/bty"))
    candidate = state_dir / "catalog.toml"
    if candidate.exists():
        return candidate
    return None


def default_cache_dir() -> Path:
    """Resolve the cache directory from the environment.

    Order: ``$BTY_CATALOG_CACHE_DIR`` (explicit), else
    ``${BTY_STATE_DIR}/cache`` (default colocation).
    """
    explicit = os.environ.get("BTY_CATALOG_CACHE_DIR")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.environ.get("BTY_STATE_DIR", "/var/lib/bty"))
    return state_dir / "cache"


def is_cached(entry: CatalogEntry, cache_dir: Path) -> bool:
    """``True`` iff the cache holds a file matching this entry's
    SHA-256. We trust the filename (which IS the SHA) and the
    presence of a regular file; full re-verification on every read
    would be expensive for multi-GiB images, so we only verify on
    write."""
    cached = entry.cached_path(cache_dir)
    return cached.is_file()


ProgressCallback: TypeAlias = Callable[[int, "int | None"], None]
"""Signature: ``progress(bytes_downloaded, total_bytes_or_None)``.
Called once per chunk written. ``total_bytes`` is the upstream
``Content-Length`` if the server sent one (most do), else ``None``."""

CancelCheck: TypeAlias = Callable[[], bool]
"""Signature: ``cancel() -> bool``. Polled between chunks; returning
``True`` raises :class:`CatalogCancelled`. Use this with
``threading.Event.is_set`` or ``asyncio.Event.is_set`` so the
fetcher (running in a worker thread) can be aborted from outside."""


def fetch_to_cache(
    entry: CatalogEntry,
    cache_dir: Path,
    *,
    timeout: float = 300.0,
    chunk_size: int = 1 << 20,  # 1 MiB
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """Download ``entry.src`` into ``cache_dir/<sha>``, verifying
    SHA-256 against ``entry.sha256``.

    Idempotent: if the cached file already exists, no-op (we trust
    that we wrote it under the correct SHA). On SHA mismatch or
    cancellation the temp file is removed before raising; the cache
    is never left in a half-written state. Atomic via ``os.replace``
    after the SHA check passes.

    ``progress(downloaded, total_or_none)`` is called once per chunk
    written, with ``total`` from the upstream ``Content-Length`` if
    available. ``cancel()`` is polled between chunks; returning
    ``True`` raises :class:`CatalogCancelled`. Both are optional; the
    CLI's offline ``bty catalog fetch`` doesn't pass them.

    Returns the cached path on success.
    """
    cached = entry.cached_path(cache_dir)
    if cached.is_file():
        # Even a cached entry should announce itself as "100% done"
        # so a UI that registered the request before the cache check
        # gets a clean terminal state.
        if progress is not None:
            size = cached.stat().st_size
            progress(size, size)
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Stream into a temp file in the same dir as the eventual
    # target so the final ``os.replace`` is a single rename within
    # the same filesystem (no cross-device copy).
    fd, tmp_name = tempfile.mkstemp(prefix=f".{entry.sha256[:8]}.", dir=cache_dir)
    tmp_path = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        with (
            os.fdopen(fd, "wb") as out,
            urllib.request.urlopen(entry.src, timeout=timeout) as resp,
        ):
            # Try to extract Content-Length; not all servers send it.
            total: int | None
            try:
                cl = resp.headers.get("Content-Length")
                total = int(cl) if cl is not None else None
            except (ValueError, AttributeError):
                total = None
            _stream_with_digest(
                resp,
                out,
                digest,
                chunk_size,
                progress=progress,
                cancel=cancel,
                total=total,
            )
        actual = digest.hexdigest()
        if actual != entry.sha256:
            raise CatalogError(
                f"catalog fetch {entry.name!r}: sha256 mismatch "
                f"(expected {entry.sha256}, got {actual}); discarded"
            )
        os.replace(tmp_path, cached)
        return cached
    except BaseException:
        # Any failure (network, SHA mismatch, cancellation,
        # KeyboardInterrupt) leaves no half-written cache entry behind.
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def _stream_with_digest(
    src: IO[bytes],
    dst: IO[bytes],
    digest: hashlib._Hash,
    chunk_size: int,
    *,
    progress: ProgressCallback | None,
    cancel: CancelCheck | None,
    total: int | None,
) -> None:
    """Pump bytes from ``src`` to ``dst`` in chunks while updating
    the running SHA. Caller owns the ``digest.hexdigest()`` check.

    Polls ``cancel()`` between chunks (1 MiB granularity by
    default -- a multi-GiB download cancels within seconds, not
    minutes), and reports ``progress(downloaded, total)`` per chunk.
    """
    downloaded = 0
    if progress is not None:
        progress(0, total)
    while True:
        if cancel is not None and cancel():
            raise CatalogCancelled("fetch cancelled by caller")
        chunk = src.read(chunk_size)
        if not chunk:
            return
        dst.write(chunk)
        digest.update(chunk)
        downloaded += len(chunk)
        if progress is not None:
            progress(downloaded, total)


def parse_sha256_manifest(text: str, target_name: str | None = None) -> str:
    """Parse a sha256sum-style manifest body and return the matching
    digest.

    Accepted shapes:

    - Single-line bare digest: ``"abc123...def\\n"`` (64 lower-hex).
    - sha256sum output: ``"<digest>  <filename>\\n"`` -- one or more
      lines, with two-space or whitespace separator. ``*`` and
      ``./`` filename prefixes are stripped (sha256sum binary-mode
      marker / relative-path noise).

    If ``target_name`` is given, return the digest whose filename
    matches; otherwise return the digest of the first usable line.
    Raises :class:`CatalogError` on empty input, malformed digests,
    or a missing target filename.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise CatalogError("empty sha256 manifest")

    candidates: list[tuple[str, str | None]] = []
    for raw in lines:
        parts = raw.split(maxsplit=1)
        digest = parts[0].strip().lower()
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise CatalogError(f"malformed sha256 line in manifest: {raw!r}")
        name = parts[1].lstrip("*./").strip() if len(parts) == 2 else None
        candidates.append((digest, name))

    if target_name is not None:
        for digest, name in candidates:
            if name == target_name:
                return digest
        raise CatalogError(
            f"sha256 manifest does not list a digest for {target_name!r}; "
            f"available names: {sorted(n for _, n in candidates if n)}"
        )
    # No target requested: take the first line. Caller's choice.
    return candidates[0][0]


def fetch_sha256_for_url(image_url: str, sha_url: str, *, timeout: float = 30.0) -> str:
    """Fetch ``sha_url``, parse it, and return the sha256 digest of
    the file that ``image_url`` would download.

    The match is filename-based: ``Path(urlparse(image_url).path).name``
    is the target. Most upstream conventions ship a per-artifact
    sha256 manifest with that exact filename, so the lookup is
    direct. If the manifest only carries one entry, that entry is
    returned regardless of name.
    """
    target = Path(urllib.parse.urlparse(image_url).path).name
    req = urllib.request.Request(sha_url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise CatalogError(f"GET {sha_url} failed: {exc}") from exc
    return parse_sha256_manifest(body, target_name=target if target else None)
