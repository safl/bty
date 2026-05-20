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
``hashlib`` / ``urllib`` / ``shutil`` are too. ``bty`` and
``bty-web`` both consume this module without dragging in any
extra dependency beyond their own (rich for ``bty``, fastapi /
uvicorn for ``bty-web``).
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

# Manifest schema version this implementation understands.
SCHEMA_VERSION = 1


class CatalogError(Exception):
    """Raised when a catalog file fails to parse, validate, or fetch.

    The message is operator-facing: both the ``bty`` wizard and
    bty-web print it verbatim, so it uses "catalog" vocabulary to
    match the UI (the sha256-sidecar checksum file is a distinct
    "manifest" and keeps that term). Subclass only when a call
    site needs to discriminate.
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

    ``sha256`` is optional in the schema. Different consumers have
    different needs:

    - ``bty --catalog`` (portable catalog: display + flash from src)
      ignores sha; the digest verification happens at flash time
      for ``oras://`` (manifest layer digest) or relies on TLS for
      http(s).
    - bty-web's manifest cache (``$BTY_STATE_DIR/catalog.toml`` +
      ``fetch_to_cache``) needs sha for the SHA-keyed cache and
      machine binding; ``cached_path`` raises if it's None so the
      cache layer can't accidentally use a sha-less entry.

    The schema decoupling is intentional: rolling tags (``oras://
    ...:latest``, ``github.com/.../releases/latest/download/...``)
    have no stable sha at catalog-publish time. Pre-pinning would
    freeze the catalog to whatever upstream looked like that
    afternoon, defeating the rolling-publish design.
    """

    name: str
    src: str
    sha256: str | None
    format: str | None = None
    size_bytes: int | None = None
    description: str | None = None

    @property
    def ref(self) -> str:
        """Stable provenance id, derived from ``src``.

        ``image_ref_for_src(canonicalise_src(self.src))`` -- a
        64-hex sha256 over the canonical URL. Always present
        (it's pure math on ``src``); same value across processes,
        operators, and time. Distinct from ``sha256`` (which is
        the *observed content* hash and can be ``None``).

        Exposed as a property rather than a stored field so the
        invariant ``ref == image_ref_for_src(src)`` cannot drift
        -- there's no second copy to get out of sync.
        """
        return image_ref_for_src(self.src)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        for required in ("name", "src"):
            if required not in raw:
                raise CatalogError(
                    f"catalog entry missing required field: {required!r} (entry: {raw!r})"
                )
        src = str(raw["src"])
        sha: str | None = None
        if "sha256" in raw and raw["sha256"] is not None:
            sha = str(raw["sha256"]).strip().lower()
            if not _images.is_sha256_hex(sha):
                raise CatalogError(
                    f"catalog entry {raw['name']!r}: sha256 must be a 64-char "
                    f"lower-case hex string, got {sha!r}"
                )
        # Trust-but-verify: if the inbound dict carries a ``ref``
        # field, recompute it from ``src`` and reject on mismatch.
        # Catches drift between a producer's canonicalisation and
        # ours, and prevents a malformed import from binding
        # machines to bytes the operator didn't intend. Bare-text
        # ``ref`` is normalised to lower-case hex like ``sha256``.
        if "ref" in raw and raw["ref"] is not None:
            supplied_ref = str(raw["ref"]).strip().lower()
            try:
                expected_ref = image_ref_for_src(src)
            except ValueError as exc:
                raise CatalogError(
                    f"catalog entry {raw['name']!r}: cannot verify ``ref`` "
                    f"because ``src`` is malformed: {exc}"
                ) from exc
            if supplied_ref != expected_ref:
                raise CatalogError(
                    f"catalog entry {raw['name']!r}: ``ref`` mismatch: "
                    f"supplied {supplied_ref!r} but image_ref_for_src(src) "
                    f"= {expected_ref!r}. The ref must equal "
                    f"sha256(canonicalise_src(src)); either the producer's "
                    f"canonicalisation differs from ours or the entry was "
                    f"tampered with."
                )
        return cls(
            name=str(raw["name"]),
            src=src,
            sha256=sha,
            format=raw.get("format") or _images.detect_format(Path(raw["name"])),
            size_bytes=int(raw["size_bytes"]) if raw.get("size_bytes") is not None else None,
            description=raw.get("description"),
        )

    def cached_path(self, cache_dir: Path) -> Path:
        """Where this entry's bytes live once cached. Content-
        addressed by SHA so multiple entries pointing at the same
        upstream blob share one file.

        Raises :class:`CatalogError` if the entry has no ``sha256``
        (oras:// rolling-tag entries in a portable catalog don't
        carry one; they're flash-only and never enter the manifest-
        cache path).
        """
        if self.sha256 is None:
            raise CatalogError(
                f"catalog entry {self.name!r}: cached_path requires a sha256 "
                f"but this entry has none (oras:// rolling-tag entries don't "
                f"carry a pre-pinned digest; they flash directly without "
                f"hitting the sha-keyed cache)"
            )
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


def load_bytes(raw_bytes: bytes, *, source: str = "<bytes>") -> Catalog:
    """Parse a TOML manifest from an in-memory bytes buffer.

    Used by ``bty.tui``'s ``--catalog`` fetcher when the catalog
    comes from HTTP / ORAS rather than a local path. ``source`` is
    a label for error messages (a URL, a label like ``<http>``);
    the bytes themselves carry no provenance.
    """
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"catalog at {source} is not valid TOML: {exc}") from exc

    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise CatalogError(
            f"catalog at {source}: version={version!r}, "
            f"this bty understands version={SCHEMA_VERSION}"
        )

    images_raw = raw.get("images", [])
    if not isinstance(images_raw, list):
        raise CatalogError(
            f"catalog at {source}: ``images`` must be an array of tables, "
            f"got {type(images_raw).__name__}"
        )

    entries: list[CatalogEntry] = []
    seen_names: set[str] = set()
    for raw_entry in images_raw:
        if not isinstance(raw_entry, dict):
            raise CatalogError(
                f"catalog at {source}: ``images`` entry must be a table, "
                f"got {type(raw_entry).__name__}"
            )
        entry = CatalogEntry.from_dict(raw_entry)
        if entry.name in seen_names:
            raise CatalogError(f"catalog at {source}: duplicate image name {entry.name!r}")
        seen_names.add(entry.name)
        entries.append(entry)

    return Catalog(version=version, entries=tuple(entries))


def load(path: Path) -> Catalog:
    """Parse a TOML manifest from a local file path.

    Thin wrapper around :func:`load_bytes` for the file-path case.
    Raises :class:`CatalogError` on missing files plus everything
    :func:`load_bytes` raises.
    """
    if not path.exists():
        raise CatalogError(f"catalog not found: {path}")
    return load_bytes(path.read_bytes(), source=str(path))


# Cap for ``fetch_bytes`` over http(s) and oras. 4 MiB is roomy for
# a hand-edited TOML index (hundreds of entries) while keeping a
# hostile / misconfigured remote from OOMing the caller. Local-file
# reads are uncapped: the operator's own filesystem is not a hostile
# boundary.
REMOTE_CATALOG_MAX_BYTES = 4 * 1024 * 1024


def classify_source(source: str) -> str:
    """Return the dispatch kind for a catalog source: ``"path"``,
    ``"http"``, or ``"oras"``. Raises :class:`ValueError` otherwise.

    Heuristic: an explicit ``http://`` / ``https://`` / ``oras://`` /
    ``file://`` scheme dispatches by scheme. Everything else (bare
    paths like ``./catalog.toml`` or ``/etc/bty/catalog.toml``) is
    treated as a filesystem path. ``file://`` maps to ``"path"``.
    """
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in ("http", "https"):
        if not parsed.netloc:
            raise ValueError(
                f"catalog URL missing a host: {source!r} (expected http(s)://<host>/<path>)"
            )
        return "http"
    if parsed.scheme == "oras":
        return "oras"
    if parsed.scheme in ("", "file"):
        return "path"
    raise ValueError(
        f"catalog source must be a local path or http(s):// / oras:// URL; "
        f"got scheme {parsed.scheme!r} in {source!r}"
    )


def fetch_bytes(source: str, *, timeout: float = 30.0) -> bytes:
    """Fetch a catalog TOML's raw bytes from a path / http(s) / oras source.

    Caps remote responses at :data:`REMOTE_CATALOG_MAX_BYTES`. Resolves
    ``oras://`` references through :mod:`bty.oras` (anonymous-pull flow
    against the OCI registry). Returns the raw TOML bytes; the caller
    feeds these to :func:`load_bytes`.
    """
    kind = classify_source(source)
    if kind == "path":
        parsed = urllib.parse.urlparse(source)
        path = Path(parsed.path) if parsed.scheme == "file" else Path(source)
        return path.read_bytes()
    if kind == "oras":
        # Defer the import so callers that never use oras don't pay
        # the import cost. (``bty.oras`` is pure-stdlib, so this is
        # mostly cosmetic, but keeps the load graph tidy.)
        from bty import oras as _oras

        resolved = _oras.resolve_ref(source, timeout=timeout)
        req = urllib.request.Request(resolved.blob_url, headers=resolved.headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(REMOTE_CATALOG_MAX_BYTES + 1)
    else:  # kind == "http"
        with urllib.request.urlopen(source, timeout=timeout) as resp:
            raw = resp.read(REMOTE_CATALOG_MAX_BYTES + 1)
    if len(raw) > REMOTE_CATALOG_MAX_BYTES:
        raise CatalogError(
            f"catalog response from {source} exceeded "
            f"{REMOTE_CATALOG_MAX_BYTES} bytes; refusing to parse"
        )
    return bytes(raw)


def load_source(source: str, *, timeout: float = 30.0) -> Catalog:
    """Fetch + parse a catalog from any supported source.

    Convenience wrapper combining :func:`fetch_bytes` and
    :func:`load_bytes`. Used by ``bty --catalog`` and bty-web's
    catalog ingestion path.
    """
    raw = fetch_bytes(source, timeout=timeout)
    return load_bytes(raw, source=source)


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


# ---------------------------------------------------------------------------
# Image-ref derivation
#
# Every catalog entry has a stable identifier ``bty_image_ref`` derived from
# its ``src`` URL. The same canonicalisation rules apply to all source
# schemes so trivial variations don't produce phantom-duplicate entries.
# See ``docs/src/reference.md`` for the locked rule tables; tests in
# ``tests/test_catalog.py`` cover every row of each table.

_HTTP_DEFAULT_PORTS = {"http": 80, "https": 443}


def _canonicalise_file(src: str) -> str:
    """Canonicalise a ``file://<root-relative-path>`` src.

    - strip the ``file://`` prefix
    - reject any ``..`` segment (no escaping image-root)
    - reject NUL bytes
    - drop ``.`` and empty path segments (collapse ``./``, ``//``,
      leading ``/``)
    - preserve case (Linux filesystems are case-sensitive)
    - reject empty result
    """
    assert src.startswith("file://")
    path = src[len("file://") :]
    if "\x00" in path:
        raise ValueError(f"file:// src contains NUL byte: {src!r}")
    segments = path.split("/")
    if any(seg == ".." for seg in segments):
        raise ValueError(f"file:// src contains '..' segment: {src!r}")
    kept = [seg for seg in segments if seg and seg != "."]
    if not kept:
        raise ValueError(f"file:// src normalises to empty path: {src!r}")
    return "file://" + "/".join(kept)


def _canonicalise_http(src: str) -> str:
    """Canonicalise an ``http://`` / ``https://`` src.

    - lower-case scheme + host
    - strip default port (``:80`` for http, ``:443`` for https)
    - preserve path / query / fragment / trailing slash / percent-
      encoding literally (servers can disambiguate by these)
    """
    parsed = urllib.parse.urlsplit(src)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unexpected scheme {scheme!r} for http canonicaliser: {src!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"http(s) src missing host: {src!r}")
    host = host.lower()
    netloc = host
    if parsed.port is not None and parsed.port != _HTTP_DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _canonicalise_oras(src: str) -> str:
    """Canonicalise an ``oras://`` src.

    - lower-case host + repository (DNS / OCI distribution spec)
    - preserve tag literally (OCI tags are case-sensitive)
    - preserve digest literally
    - validates structure via ``bty.oras.parse_ref`` so a malformed
      ref errors here rather than mid-flash

    Lower-cases the ``<host>/<repo>`` prefix BEFORE handing to
    parse_ref so operators who type ``oras://Ghcr.IO/Owner/...``
    don't get rejected by parse_ref's lowercase-only repo regex
    (which enforces the OCI spec but is stricter than what real
    registries accept).
    """
    from bty import oras as _oras

    body = src[len("oras://") :]
    if not body:
        raise ValueError("empty oras src")
    # Locate the tag/digest separator and lower-case only the
    # <host>/<repo> prefix; tag / digest after it keep their case.
    if "@" in body:
        idx = body.rindex("@")
    elif ":" in body:
        idx = body.rindex(":")
    else:
        # No separator -- parse_ref will reject below.
        idx = len(body)
    prefix = body[:idx].lower()
    suffix = body[idx:]
    try:
        ref = _oras.parse_ref(f"oras://{prefix}{suffix}")
    except _oras.OrasError as exc:
        raise ValueError(f"malformed oras src: {exc}") from exc
    if ref.digest is not None:
        return f"oras://{ref.host}/{ref.repository}@{ref.digest}"
    return f"oras://{ref.host}/{ref.repository}:{ref.tag}"


def canonicalise_src(src: str) -> str:
    """Return the canonical form of a catalog ``src`` URL.

    Dispatches on scheme:

    - ``file://<rel-path>``  -- root-relative; segments normalised.
    - ``http(s)://...``      -- scheme + host lower-cased; default
      port stripped.
    - ``oras://...``         -- host + repo lower-cased.

    Raises :class:`ValueError` for any other scheme or malformed
    input. The full per-scheme rule table lives in
    ``docs/src/reference.md``; tests cover every row.

    Scheme prefix matching is case-insensitive (so ``HTTPS://...``
    dispatches to the http canonicaliser, which then lower-cases
    the scheme). Other parts of each scheme have scheme-specific
    case rules in their respective helpers.
    """
    if not src:
        raise ValueError("empty src")
    lowered = src.lower()
    if lowered.startswith("file://"):
        return _canonicalise_file(src)
    if lowered.startswith(("http://", "https://")):
        return _canonicalise_http(src)
    if lowered.startswith("oras://"):
        return _canonicalise_oras(src)
    raise ValueError(f"unsupported src scheme: {src!r} (expected file://, http(s)://, or oras://)")


def image_ref_for_src(src: str) -> str:
    """Compute the ``bty_image_ref`` for a catalog ``src``.

    Returns a 64-character lowercase hex string -- the SHA-256 of
    the canonical form of ``src`` (see :func:`canonicalise_src`).
    Same algorithm for every source kind so the catalog has a
    uniform identifier space.

    The image-ref is **stable provenance**: identical src strings
    produce identical refs across operators, machines, and time.
    It is **not** a content hash; an oras rolling tag's ref stays
    the same when the underlying content changes. The observed
    content hash lives separately in ``CatalogEntry.disk_image_sha``
    (populated on first cache).
    """
    canonical = canonicalise_src(src)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    ``True`` raises :class:`CatalogCancelled`. Both are optional.

    Returns the cached path on success.
    """
    # ``cached_path`` raises if sha is None, so the assertion is a
    # type-narrowing aid for mypy: from here on entry.sha256 is str.
    cached = entry.cached_path(cache_dir)
    assert entry.sha256 is not None
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
        # ``oras://`` entries route through bty.oras to resolve the
        # manifest layer and inject a bearer-token Authorization
        # header on the blob GET. Plain http(s) URLs use urlopen
        # with the URL directly. urllib.request.urlopen accepts
        # both ``str`` and ``Request``.
        fetch_request: str | urllib.request.Request
        if entry.src.startswith("oras://"):
            from bty import oras as _oras

            resolved = _oras.resolve_ref(entry.src, timeout=timeout)
            fetch_request = urllib.request.Request(resolved.blob_url, headers=resolved.headers)
        else:
            fetch_request = entry.src
        with (
            os.fdopen(fd, "wb") as out,
            urllib.request.urlopen(fetch_request, timeout=timeout) as resp,
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


def fetch_src_to_cache(
    src: str,
    cache_dir: Path,
    *,
    expected_sha: str | None = None,
    timeout: float = 300.0,
    chunk_size: int = 1 << 20,  # 1 MiB
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> tuple[Path, str]:
    """Eagerly fetch a remote ``src`` into the content-addressed cache.

    Unlike :func:`fetch_to_cache`, this variant does NOT require the
    SHA to be known in advance. It streams the bytes from ``src``,
    computes the sha as bytes flow, and atomic-renames into
    ``cache_dir/<sha>``. When ``expected_sha`` is given, the streamed
    digest must match it; on mismatch the temp file is discarded and
    :class:`CatalogError` is raised (the cache is never left in a
    half-written state).

    Used by the bty-web PXE flash path's eager cache-through: when a
    machine is bound to a ``bty_image_ref`` whose ``disk_image_sha``
    is unknown (rolling oras tag never fetched, URL-only entry that
    hasn't been resolved), the live env's ``GET /images/<ref>/<name>``
    triggers this fetch + caches + serves the bytes.

    ``src`` must be an http(s):// or oras:// URL; file:// srcs don't
    need fetching (bytes are already on disk under ``BTY_IMAGE_ROOT``)
    and a :class:`ValueError` surfaces instead.

    Returns ``(cache_path, computed_sha)``.
    """
    if src.startswith("file://"):
        raise ValueError(
            f"fetch_src_to_cache does not handle file:// srcs (bytes are already local): {src!r}"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_dir / f".tmp.{os.urandom(8).hex()}"
    digest = hashlib.sha256()
    try:
        if src.startswith("oras://"):
            from bty import oras as _oras

            resolved = _oras.resolve_ref(src, timeout=timeout)
            req = urllib.request.Request(resolved.blob_url, headers=resolved.headers)
            opener = urllib.request.urlopen(req, timeout=timeout)
        else:
            opener = urllib.request.urlopen(src, timeout=timeout)
        with opener as resp, tmp_path.open("wb") as out:
            total = (
                int(resp.headers.get("Content-Length"))
                if resp.headers.get("Content-Length")
                else None
            )
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
        if expected_sha is not None and actual != expected_sha.lower():
            raise CatalogError(
                f"fetch_src_to_cache {src!r}: sha256 mismatch "
                f"(expected {expected_sha}, got {actual}); discarded"
            )
        cached = cache_dir / actual
        os.replace(tmp_path, cached)
        return cached, actual
    except BaseException:
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
        if not _images.is_sha256_hex(digest):
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


# sha256sum-style manifests are tiny in practice: one line per
# artifact at ~80 bytes each. 1 MiB caps a maliciously-large or
# wrong-URL response without rejecting any plausible real
# manifest. Without this cap, a ``sha_url`` that points at a
# multi-GB file (operator typo into the image url field) reads
# the whole body into memory.
_SHA_MANIFEST_MAX_BYTES = 1 << 20


def fetch_sha256_for_url(image_url: str, sha_url: str, *, timeout: float = 30.0) -> str:
    """Fetch ``sha_url``, parse it, and return the sha256 digest of
    the file that ``image_url`` would download.

    The match is filename-based: ``Path(urlparse(image_url).path).name``
    is the target. Most upstream conventions ship a per-artifact
    sha256 manifest with that exact filename, so the lookup is
    direct. If the manifest only carries one entry, that entry is
    returned regardless of name.

    Raises :class:`CatalogError` if the body is larger than
    :data:`_SHA_MANIFEST_MAX_BYTES` (defends against the operator
    pasting an *image* URL into the sha_url field by accident).
    """
    target = Path(urllib.parse.urlparse(image_url).path).name
    req = urllib.request.Request(sha_url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Read one byte past the cap so we can distinguish
            # "fits in the cap" from "exceeded the cap"; the OS
            # short-reads aren't reliable here.
            raw = resp.read(_SHA_MANIFEST_MAX_BYTES + 1)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise CatalogError(f"GET {sha_url} failed: {exc}") from exc
    if len(raw) > _SHA_MANIFEST_MAX_BYTES:
        raise CatalogError(
            f"sha256 manifest at {sha_url} is larger than "
            f"{_SHA_MANIFEST_MAX_BYTES} bytes; refusing to parse "
            f"(did you paste an image URL into the sha_url field?)"
        )
    body = raw.decode("utf-8")
    return parse_sha256_manifest(body, target_name=target or None)
