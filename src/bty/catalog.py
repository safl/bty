"""bty catalog manifest with src URLs + URL-keyed local filenames.

A catalog manifest (TOML, ``${BTY_STATE_DIR}/catalog.toml`` by
default) lists named images with upstream ``src`` URLs and (optional)
pinned ``sha256`` digests:

.. code-block:: toml

    version = 1

    [[images]]
    name = "ubuntu-server-22.04-bty.img.zst"
    src = "https://github.com/safl/bty-images/releases/download/v0.1/ubuntu-22.04.img.zst"
    sha256 = "abc123..."
    format = "img.zst"

The fetcher downloads each ``src`` on demand, verifies SHA-256
against the manifest if one is given, and atomically writes the
file into the operator's ``BTY_IMAGE_ROOT`` directory with a
URL-derived name: ``catalog-<bty_image_ref[:12]>-<slug(name)>.<ext>``
(e.g. ``catalog-8e54fdb21522-nosi-debian-sysdev.img.gz``). Same URL
hashes to the same filename, so re-fetches are idempotent and
catalog files dedup naturally with the operator's local images
under a single directory -- no separate ``cache/`` subdir, no
sha-keyed content addressing. The ``catalog-`` prefix calls out
catalog-fetched files vs operator-typed ones at a glance.

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
import re
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

# Filename prefix for catalog-fetched images under the image_root.
# Discovery code uses this to tell apart operator-typed files (no
# prefix) from catalog-cached ones (with the prefix + URL-derived
# hash so two distinct URLs never collide on disk).
_CATALOG_PREFIX = "catalog-"

# Image-store naming-convention version. Independent of
# ``bty.__version__``: this number is bumped ONLY when the on-disk
# layout / filename grammar changes in a way that older bty-web
# can no longer read. The current scheme (v1):
#
#   image_root/
#     <operator-typed>.<ext>            # operator uploaded / dropped
#     catalog-<ref:12>-<slug>.<ext>     # bty-fetched from a catalog entry
#     <file>.sha256                     # sidecar manifests (any of the above)
#     .bty-storage.json                 # marker; carries this version
#
# The marker file is created on first bty-web startup against a
# fresh image_root and validated on every subsequent start. A
# mismatch makes bty-web bail (operator-facing message points at
# bty-state-init / shell remediation) rather than silently treating
# a future layout as the current one.
STORAGE_FORMAT_VERSION = 1


def is_catalog_cache_filename(name: str) -> bool:
    """True iff ``name`` is the basename of a catalog-fetched cache
    file (``catalog-<ref:12>-<slug>.<ext>``). Used by the dir-scan
    paths (``images.merge_with_catalog`` pass 1, the
    ``_auto_import_dir_scan_rows`` startup pass) to recognise cache
    files as belonging to an existing catalog entry rather than as
    standalone operator-typed images. Without this gate the same
    image surfaces twice on ``/ui/images`` -- once as the catalog
    entry, once as a synthetic ``file://`` entry derived from its
    cache filename.
    """
    return name.startswith(_CATALOG_PREFIX)


def ref_prefix_from_cache_filename(name: str) -> str | None:
    """Extract the 12-hex ``bty_image_ref`` prefix encoded in a
    catalog-cache filename, or ``None`` if ``name`` is not a
    well-formed cache filename. Mirror of :func:`local_filename_for`:
    composes catalog-<ref:12>-... -> ref:12.

    Used by the HashManager terminal callback to backfill
    ``catalog_entries.disk_image_sha`` for an operator-triggered
    hash of a catalog-cache file -- the row's ``src`` is the
    upstream URL (not ``file://catalog-...``), so the src-keyed
    UPDATE there can't find it; the ref-prefix LIKE WHERE clause
    does.
    """
    if not name.startswith(_CATALOG_PREFIX):
        return None
    rest = name[len(_CATALOG_PREFIX) :]
    sep = rest.find("-")
    if sep != _CATALOG_REF_LEN:
        return None
    prefix = rest[:_CATALOG_REF_LEN]
    if any(c not in "0123456789abcdef" for c in prefix):
        return None
    return prefix


# Files under image_root the storage layer recognises explicitly.
# Everything else triggers an "unconventional name" warning on scan
# so an operator who dropped a stray file (notes, a non-bty backup,
# a half-downloaded curl that didn't atomic-rename) can see it.
# The dot-prefixed marker + sha256 sidecars are bookkeeping;
# catalog- + operator-typed image extensions are payload.
_STORAGE_MARKER_FILENAME = ".bty-storage.json"


class StorageFormatMismatch(RuntimeError):
    """Raised by :func:`check_or_write_storage_marker` when the marker
    on disk doesn't match the version this bty-web understands. The
    message is operator-facing: it names the on-disk version, the
    running version, and recommends the manual cleanup path."""


def check_or_write_storage_marker(image_root: Path) -> int:
    """Validate (or create) ``image_root/.bty-storage.json``.

    On first use (fresh image_root, no marker), writes the current
    :data:`STORAGE_FORMAT_VERSION` + creation timestamp. On
    subsequent calls, reads the marker; if the stored version
    matches, returns the version + does nothing.

    If the stored version DOES NOT match the running
    ``STORAGE_FORMAT_VERSION``, raises :class:`StorageFormatMismatch`
    so the bty-web start aborts. Operator response: drop to a
    shell, archive / wipe the state directory, re-init the
    storage layout (``bty-state-init`` -- a follow-up tool; for
    now ``rm -rf $image_root/*`` then restart). The on-disk
    layout can't be silently upgraded because the naming /
    semantics may have diverged.

    Returns the version on disk (or just-written) so callers can
    log it.
    """
    import json
    from datetime import UTC, datetime

    image_root.mkdir(parents=True, exist_ok=True)
    marker = image_root / _STORAGE_MARKER_FILENAME
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageFormatMismatch(
                f"image-store marker {marker} is unreadable / malformed "
                f"({exc!r}). The image_root may be corrupt or was created "
                f"by a non-bty process. Inspect, then either restore from "
                f"backup or wipe + re-init (drop to a shell with Alt+F2, "
                f"archive {image_root!s} / *, restart bty-web)."
            ) from exc
        stored = data.get("format_version")
        if stored != STORAGE_FORMAT_VERSION:
            raise StorageFormatMismatch(
                f"image-store at {image_root} uses storage format "
                f"v{stored!r}; this bty-web understands v{STORAGE_FORMAT_VERSION}. "
                f"Older / newer layouts are NOT auto-migrated -- the "
                f"naming conventions may have diverged. Operator action: "
                f"drop to a shell (Alt+F2), archive the contents of "
                f"{image_root!s} (e.g. ``mv {image_root!s} {image_root!s}.bak``) "
                f"then restart bty-web -- a fresh image_root will get "
                f"the current marker on init."
            )
        return int(stored)
    # No marker: this is a fresh / never-used image_root. Stamp it.
    marker.write_text(
        json.dumps(
            {
                "format_version": STORAGE_FORMAT_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "created_by_bty_version": _bty_version_for_marker(),
            },
            indent=2,
        )
        + "\n"
    )
    return STORAGE_FORMAT_VERSION


def _bty_version_for_marker() -> str:
    """Read ``bty.__version__`` for the storage-marker's diagnostic
    field. Lazy-imported so this module can be parsed even before
    bty.__init__ has populated __version__ (which would be
    pathological but cheap to guard against)."""
    try:
        import bty

        return str(bty.__version__)
    except Exception:
        return "unknown"


def is_recognised_image_store_filename(name: str) -> bool:
    """True iff ``name`` follows one of the documented image-store
    conventions (v1):

    - ``catalog-<ref:12>-<slug>.<ext>`` catalog-fetched cache file
    - ``<any>.<known-image-ext>`` operator-typed image
    - ``<file>.sha256`` sidecar
    - ``.bty-storage.json`` storage marker
    - ``.<ref:8>.<random>`` mid-fetch tempfile (cleaned up on
      success, leaks on crash; tolerated, not warned about)

    Anything else is operator-droppings or a stray file from a
    failed migration -- callers use this to decide whether to
    warn / bail.
    """
    if name == _STORAGE_MARKER_FILENAME:
        return True
    if name.endswith(".sha256"):
        return True
    if name.endswith(".partial"):
        # Upload-in-progress sidecar (bty.web._app._stream_upload).
        return True
    # Mid-fetch tempfile from catalog.fetch_to_cache: ``.<ref:8>.<random>``.
    # The leading dot + non-recognised extension would otherwise warn;
    # tolerate so a crashed fetch doesn't leak operator-facing noise.
    if name.startswith(".") and "." in name[1:]:
        return True
    # Image extensions live in bty.images.detect_format; check by
    # path-only detection so we don't read the file.
    from bty.images import detect_format

    return detect_format(Path(name)) is not None


# Length of the bty_image_ref segment in catalog filenames. 12 hex
# chars is 48 bits, collision-free at any plausible homelab catalog
# size; long enough to be useful for human disambiguation, short
# enough not to dominate the filename.
_CATALOG_REF_LEN = 12

# Slug character set: lower-case ASCII alnum + hyphen + underscore.
# Anything else collapses to a single hyphen so the filename stays
# portable across filesystems.
_SLUG_BAD = re.compile(r"[^a-z0-9_]+")
_SLUG_DEDUP = re.compile(r"-+")


def _slugify(text: str) -> str:
    """Filename-safe lower-case ASCII slug.

    "nosi debian-sysdev (x86_64, rolling)" -> "nosi-debian-sysdev-x86_64-rolling"

    The slug carries no semantic weight (uniqueness lives in the
    ``bty_image_ref`` prefix); it's only there to keep ``ls`` legible
    when an operator browses the image_root.
    """
    s = _SLUG_BAD.sub("-", text.lower())
    s = _SLUG_DEDUP.sub("-", s).strip("-")
    return s or "image"


def local_filename_for(bty_image_ref: str, name: str, fmt: str | None) -> str:
    """Compose the on-disk filename for a catalog-fetched image from
    its raw fields. Used by bty-web's image-serving path where the
    catalog row is read from the DB and a full :class:`CatalogEntry`
    isn't constructed -- mirror of :meth:`CatalogEntry.local_filename`
    over the same field set.

    Pattern: ``catalog-<bty_image_ref[:12]>-<slug(name)>.<ext>``.
    """
    ref_prefix = bty_image_ref[:_CATALOG_REF_LEN]
    slug = _slugify(name)
    ext = (fmt or "img").lstrip(".")
    return f"{_CATALOG_PREFIX}{ref_prefix}-{slug}.{ext}"


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

    def local_filename(self) -> str:
        """The on-disk filename this entry's bytes land at under the
        image_root. Pattern: ``catalog-<bty_image_ref[:12]>-<slug>.<ext>``.

        Derived purely from ``src`` (via ``bty_image_ref``), ``name``,
        and ``format``. Same URL -> same filename, independent of
        whether sha256 is pinned. No requirement on ``sha256``: ORAS
        rolling-tag entries (``oras://...:latest``) get a stable
        filename and benefit from on-disk dedup just like pinned
        entries do.

        ``format`` defaults to ``"img"`` if missing -- catalog entries
        always carry a format in practice (``from_dict`` defaults it
        from the name's extension), so this fallback is only for hand-
        constructed test entries.
        """
        return local_filename_for(self.ref, self.name, self.format)

    def cached_path(self, image_root: Path) -> Path:
        """Where this entry's bytes live once fetched. URL-keyed via
        ``local_filename`` so a re-fetch of the same ``src`` lands on
        the same file (idempotent), and two distinct ``src`` URLs
        never collide.

        Same return shape as ``image_root / local_filename()`` -- kept
        as a method on ``CatalogEntry`` for the natural call site
        ``entry.cached_path(image_root)``.
        """
        return image_root / self.local_filename()


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
        # Contract: a catalog manifest is a PUBLISHABLE artifact -- bty-server
        # serves one, the GitHub release publishes one, an operator points
        # ``bty --catalog`` at one. ``file://`` srcs are meaningless to any
        # receiver other than the publisher's host (the path's gone the
        # moment you copy the file elsewhere), so they cannot appear in a
        # parsed catalog. Local files live in the image-root and are
        # discovered separately (``images.list_images``); they never make
        # it into a manifest.
        if entry.src.startswith("file://"):
            raise CatalogError(
                f"catalog at {source}: entry {entry.name!r} has ``file://`` src "
                f"({entry.src!r}); catalog manifests carry only remote sources "
                f"(oras:// / http:// / https://) so the file is meaningful to a "
                f"receiver. Local files belong in the image-root, not a manifest."
            )
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


# ``default_cache_dir`` was removed in v0.31.0 when catalog files moved
# under the image_root with URL-derived ``catalog-<ref:12>-<slug>.<ext>``
# names. There is no separate cache directory anymore -- callers use
# ``bty.images.default_image_root()`` and pass it everywhere this
# module used to take ``cache_dir``.


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


def is_cached(entry: CatalogEntry, image_root: Path) -> bool:
    """``True`` iff the image_root holds a file at this entry's URL-
    keyed ``local_filename``. We trust the filename (which encodes the
    bty_image_ref) and the presence of a regular file; full re-
    verification on every read would be expensive for multi-GiB
    images, so we only verify on write."""
    return entry.cached_path(image_root).is_file()


ProgressCallback: TypeAlias = Callable[[int, "int | None"], None]
"""Signature: ``progress(bytes_done, total_bytes_or_None)``.
Called once per chunk written. ``total_bytes`` is the upstream
``Content-Length`` if the server sent one (most do), else ``None``."""

CancelCheck: TypeAlias = Callable[[], bool]
"""Signature: ``cancel() -> bool``. Polled between chunks; returning
``True`` raises :class:`CatalogCancelled`. Use this with
``threading.Event.is_set`` or ``asyncio.Event.is_set`` so the
fetcher (running in a worker thread) can be aborted from outside."""


def fetch_to_cache(
    entry: CatalogEntry,
    image_root: Path,
    *,
    timeout: float = 300.0,
    chunk_size: int = 1 << 20,  # 1 MiB
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """Download ``entry.src`` into ``image_root / entry.local_filename()``,
    verifying SHA-256 against ``entry.sha256`` when one is pinned.

    Idempotent: if the file already exists, no-op (the URL-keyed
    filename means same src always lands the same path). On SHA
    mismatch or cancellation the temp file is removed before raising;
    the image_root is never left with a half-written file. Atomic via
    ``os.replace`` after the SHA check passes.

    ``progress(downloaded, total_or_none)`` is called once per chunk
    written, with ``total`` from the upstream ``Content-Length`` if
    available. ``cancel()`` is polled between chunks; returning
    ``True`` raises :class:`CatalogCancelled`. Both are optional.

    Returns the final path on success.
    """
    cached = entry.cached_path(image_root)
    if cached.is_file():
        # Even a cached entry should announce itself as "100% done"
        # so a UI that registered the request before the cache check
        # gets a clean terminal state.
        if progress is not None:
            size = cached.stat().st_size
            progress(size, size)
        return cached

    image_root.mkdir(parents=True, exist_ok=True)

    # Stream into a hidden temp file in the same dir as the eventual
    # target so the final ``os.replace`` is a single rename within
    # the same filesystem (no cross-device copy). ``.<ref:8>.`` prefix
    # is debuggable (operator can grep partial downloads) and won't
    # collide with the final ``catalog-`` filename.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{entry.ref[:8]}.", dir=image_root)
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
        # Only verify when the manifest pinned a sha256; rolling-tag
        # entries (``oras://...:latest``) don't carry one. The
        # observed hash still gets returned via the local file's
        # ``catalog_entries.disk_image_sha`` write at the call site.
        if entry.sha256 is not None and actual != entry.sha256:
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
    image_root: Path,
    *,
    local_filename: str,
    expected_sha: str | None = None,
    timeout: float = 300.0,
    chunk_size: int = 1 << 20,  # 1 MiB
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> tuple[Path, str]:
    """Eagerly fetch a remote ``src`` into ``image_root / local_filename``,
    computing the SHA-256 as bytes flow.

    Unlike :func:`fetch_to_cache`, this variant does NOT require the
    SHA to be known in advance. It streams the bytes from ``src``,
    computes the sha, and atomic-renames into the URL-keyed local
    filename. When ``expected_sha`` is given, the streamed digest
    must match it; on mismatch the temp file is discarded and
    :class:`CatalogError` is raised (the image_root is never left
    with a half-written file).

    Used by the bty-web ``DownloadManager`` for explicit,
    operator-initiated fetches of a catalog entry whose ``sha256``
    field was empty -- the manager passes ``local_filename`` from
    ``entry.local_filename()`` so the on-disk shape matches
    :func:`fetch_to_cache`'s for sha-pinned entries.

    ``src`` must be an http(s):// or oras:// URL; file:// srcs don't
    need fetching (bytes are already on disk under ``BTY_IMAGE_ROOT``)
    and a :class:`ValueError` surfaces instead.

    Returns ``(local_path, computed_sha)``.
    """
    if src.startswith("file://"):
        raise ValueError(
            f"fetch_src_to_cache does not handle file:// srcs (bytes are already local): {src!r}"
        )
    image_root.mkdir(parents=True, exist_ok=True)
    tmp_path = image_root / f".tmp.{os.urandom(8).hex()}"
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
            # Read the header once and guard the parse: a malformed
            # ``Content-Length`` should fold into "unknown total"
            # rather than crash the fetch (mirrors ``fetch_to_cache``
            # and ``_releases._stream``).
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
        if expected_sha is not None and actual != expected_sha.lower():
            raise CatalogError(
                f"fetch_src_to_cache {src!r}: sha256 mismatch "
                f"(expected {expected_sha}, got {actual}); discarded"
            )
        cached = image_root / local_filename
        os.replace(tmp_path, cached)
        return cached, actual
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def stream_src(
    src: str,
    *,
    chunk_size: int = 1 << 20,
    timeout: float = 300.0,
) -> tuple[Iterator[bytes], int | None]:
    """Open a remote ``src`` (oras:// / http(s)://) and return
    ``(chunk_iterator, content_length_or_None)`` for streaming the bytes
    straight through to a client WITHOUT caching to disk.

    bty-web's image-serve path uses this to proxy a remote image to a
    flashing client: bytes flow source -> server -> client as they
    arrive (no buffer-then-serve, no .tmp), so the client's
    ``curl | dd`` starts writing immediately and a large image never
    times out a probe or thrashes a cache. ``file://`` srcs are already
    local and raise ``ValueError``.

    The returned iterator owns the connection and closes it when
    exhausted (or when the consumer stops iterating + it's GC'd).
    """
    if src.startswith("oras://"):
        from bty import oras as _oras

        resolved = _oras.resolve_ref(src, timeout=timeout)
        req = urllib.request.Request(resolved.blob_url, headers=resolved.headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
    elif src.startswith(("http://", "https://")):
        resp = urllib.request.urlopen(src, timeout=timeout)
    else:
        raise ValueError(f"stream_src handles only oras:// / http(s):// srcs: {src!r}")
    try:
        cl = resp.headers.get("Content-Length")
        total = int(cl) if cl is not None else None
    except (ValueError, AttributeError):
        total = None

    def _chunks() -> Iterator[bytes]:
        try:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    return _chunks(), total


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
