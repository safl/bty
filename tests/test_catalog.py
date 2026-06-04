"""Tests for ``bty.catalog``.

Coverage:
  * TOML parsing happy + sad paths.
  * ``CatalogEntry.from_dict`` field validation (required, sha
    format, format auto-detection from filename extension).
  * ``fetch_to_cache``: success, SHA mismatch, idempotency,
    no-half-written-cache invariant on failure.

Network is mocked at ``urllib.request.urlopen`` so tests are
hermetic and fast.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from bty import catalog


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


# -----------------------------------------------------------------------
# load() / parser
# -----------------------------------------------------------------------


def test_load_returns_empty_catalog_when_no_images(tmp_path: Path) -> None:
    """A bare ``version = 1`` is valid; ``len(catalog) == 0``."""
    path = _write(tmp_path / "catalog.toml", "version = 1\n")
    cat = catalog.load(path)
    assert cat.version == 1
    assert len(cat) == 0
    assert cat.by_name("anything") is None


def test_load_parses_one_entry(tmp_path: Path) -> None:
    body = """
        version = 1

        [[images]]
        name = "ubuntu-server-22.04-bty.img.zst"
        src = "https://example.com/ubuntu.img.zst"
        sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        format = "img.zst"
        size_bytes = 12345
        description = "test"
    """
    path = _write(tmp_path / "catalog.toml", body)
    cat = catalog.load(path)
    assert len(cat) == 1
    entry = cat.by_name("ubuntu-server-22.04-bty.img.zst")
    assert entry is not None
    assert entry.src == "https://example.com/ubuntu.img.zst"
    assert entry.sha256 == "0123456789abcdef" * 4
    assert entry.format == "img.zst"
    assert entry.size_bytes == 12345
    assert entry.description == "test"


def test_load_format_auto_detected_from_name(tmp_path: Path) -> None:
    body = """
        version = 1
        [[images]]
        name = "auto.qcow2"
        src = "https://example.com/auto.qcow2"
        sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    """
    path = _write(tmp_path / "catalog.toml", body)
    entry = catalog.load(path).by_name("auto.qcow2")
    assert entry is not None
    assert entry.format == "qcow2"


def test_load_rejects_unknown_version(tmp_path: Path) -> None:
    path = _write(tmp_path / "catalog.toml", "version = 99\n")
    with pytest.raises(catalog.CatalogError, match="version"):
        catalog.load(path)


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    """``name`` and ``src`` are required; ``sha256`` is optional
    so rolling-tag oras:// and rolling-asset http URLs don't need
    a pre-pinned digest. This test pins the required-field rule:
    an entry without ``src`` is rejected, but an entry without
    ``sha256`` parses cleanly."""
    body = """
        version = 1
        [[images]]
        name = "no-src"
    """
    path = _write(tmp_path / "catalog.toml", body)
    with pytest.raises(catalog.CatalogError, match="src"):
        catalog.load(path)


def test_load_rejects_bad_sha256(tmp_path: Path) -> None:
    body = """
        version = 1
        [[images]]
        name = "bad"
        src = "https://example.com/bad"
        sha256 = "NOT-A-SHA"
    """
    path = _write(tmp_path / "catalog.toml", body)
    with pytest.raises(catalog.CatalogError, match="sha256"):
        catalog.load(path)


def test_load_rejects_duplicate_name(tmp_path: Path) -> None:
    body = """
        version = 1
        [[images]]
        name = "dupe.img.zst"
        src = "https://example.com/a"
        sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        [[images]]
        name = "dupe.img.zst"
        src = "https://example.com/b"
        sha256 = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
    """
    path = _write(tmp_path / "catalog.toml", body)
    with pytest.raises(catalog.CatalogError, match="duplicate"):
        catalog.load(path)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="not found"):
        catalog.load(tmp_path / "nope.toml")


def test_load_rejects_malformed_toml(tmp_path: Path) -> None:
    path = _write(tmp_path / "catalog.toml", "not = valid = toml\n")
    with pytest.raises(catalog.CatalogError, match="not valid TOML"):
        catalog.load(path)


def test_catalog_error_messages_say_catalog_not_manifest(tmp_path: Path) -> None:
    """Operator-facing ``CatalogError`` messages use the word
    "catalog", not "manifest" -- the UI renamed the concept to
    "Catalog" everywhere, and these errors print verbatim in both
    the ``bty`` wizard and bty-web. The internal sha256-sidecar
    checksum file is a different "manifest" and keeps that term.

    Pins the rename so a future edit to the error strings doesn't
    drift back to "manifest entry ..." / "catalog manifest at ...".
    """
    # Missing-field error.
    bad_field = _write(tmp_path / "a.toml", 'version = 1\n[[images]]\nname = "no-src"\n')
    with pytest.raises(catalog.CatalogError) as ei1:
        catalog.load(bad_field)
    assert "catalog entry" in str(ei1.value)
    assert "manifest entry" not in str(ei1.value)

    # Parse error.
    bad_toml = _write(tmp_path / "b.toml", "not = valid = toml\n")
    with pytest.raises(catalog.CatalogError) as ei2:
        catalog.load(bad_toml)
    assert "catalog at" in str(ei2.value)
    assert "catalog manifest at" not in str(ei2.value)

    # Missing-file error.
    with pytest.raises(catalog.CatalogError) as ei3:
        catalog.load(tmp_path / "nope.toml")
    assert "catalog not found" in str(ei3.value)


# -----------------------------------------------------------------------
# fetch_to_cache()
# -----------------------------------------------------------------------


def _entry(payload: bytes, name: str = "img.img.zst") -> catalog.CatalogEntry:
    return catalog.CatalogEntry(
        name=name,
        src="https://example.com/" + name,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _mock_urlopen(payload: bytes):
    """Helper: returns a context-manager-shaped mock that
    ``urllib.request.urlopen`` would produce."""

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self._buf

        def __exit__(self, *exc):
            self._buf.close()
            return False

    return lambda url, timeout=None: _Resp(payload)


def test_fetch_to_cache_success(tmp_path: Path) -> None:
    payload = b"fake image bytes"
    entry = _entry(payload)
    image_root = tmp_path / "images"
    with patch("urllib.request.urlopen", _mock_urlopen(payload)):
        cached = catalog.fetch_to_cache(entry, image_root)
    assert cached.is_file()
    assert cached.read_bytes() == payload
    # v0.31.0+: URL-keyed local filename, not the content sha.
    assert cached.name == entry.local_filename()
    assert cached.name.startswith("catalog-")


def test_fetch_to_cache_idempotent(tmp_path: Path) -> None:
    payload = b"fake image bytes"
    entry = _entry(payload)
    image_root = tmp_path / "images"
    image_root.mkdir()
    # Pre-populate as if we had fetched before. v0.31.0+: file is
    # named by URL-keyed local_filename, not content sha.
    target = image_root / entry.local_filename()
    target.write_bytes(payload)

    # urlopen would error if called; idempotency means it isn't.
    def _boom(*_a, **_kw):
        raise AssertionError("urlopen should not be called when cached")

    with patch("urllib.request.urlopen", _boom):
        cached = catalog.fetch_to_cache(entry, image_root)
    assert cached == target


def test_fetch_to_cache_sha_mismatch_discards_temp(tmp_path: Path) -> None:
    payload = b"the real bytes"
    entry = catalog.CatalogEntry(
        name="lying.img.zst",
        src="https://example.com/lying.img.zst",
        # Manifest claims this sha but the upstream returns
        # ``payload`` whose sha differs -- simulating a corrupted
        # mirror or a tampered file.
        sha256="0" * 64,
    )
    cache_dir = tmp_path / "cache"
    with (
        patch("urllib.request.urlopen", _mock_urlopen(payload)),
        pytest.raises(catalog.CatalogError, match="sha256 mismatch"),
    ):
        catalog.fetch_to_cache(entry, cache_dir)
    # Critical invariant: no half-written cache file remains.
    leftovers = list(cache_dir.iterdir()) if cache_dir.exists() else []
    assert leftovers == []


def test_is_cached_true_when_file_present(tmp_path: Path) -> None:
    image_root = tmp_path / "images"
    image_root.mkdir()
    entry = _entry(b"x")
    assert not catalog.is_cached(entry, image_root)
    (image_root / entry.local_filename()).write_bytes(b"x")
    assert catalog.is_cached(entry, image_root)


def test_fetch_to_cache_progress_callback(tmp_path: Path) -> None:
    """``progress(downloaded, total)`` is called once per chunk;
    final call reports the full size. ``total`` reflects the
    Content-Length when the upstream provides it."""
    payload = b"a" * (1 << 20) * 3 + b"tail"  # 3 MiB + 4 bytes
    entry = _entry(payload)
    cache_dir = tmp_path / "cache"
    progress_log: list[tuple[int, int | None]] = []

    class _RespWithCL:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)
            # Mimic urllib's ``addinfourl`` which exposes
            # response headers via ``.headers``.
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._buf.close()
            return False

        # urlopen returns an object that implements .read(), which
        # _stream_with_digest reads from directly.
        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

    with patch("urllib.request.urlopen", lambda *_a, **_kw: _RespWithCL(payload)):
        catalog.fetch_to_cache(
            entry,
            cache_dir,
            progress=lambda d, t: progress_log.append((d, t)),
            chunk_size=1 << 20,
        )

    # First call is (0, total) "starting"; final is (len, total).
    assert progress_log[0] == (0, len(payload))
    assert progress_log[-1] == (len(payload), len(payload))
    # All total values agree (we only check Content-Length once).
    assert all(t == len(payload) for _, t in progress_log)


def test_fetch_to_cache_cancel_aborts_cleanly(tmp_path: Path) -> None:
    """``cancel()`` returning True between chunks raises
    CatalogCancelled and leaves no half-written cache file."""
    payload = b"x" * (1 << 21)  # 2 MiB so we get >1 chunk
    entry = _entry(payload)
    cache_dir = tmp_path / "cache"

    # Cancel after the first chunk is read.
    state = {"polls": 0}

    def _cancel() -> bool:
        state["polls"] += 1
        return state["polls"] > 1  # let the first chunk through

    with (
        patch("urllib.request.urlopen", _mock_urlopen(payload)),
        pytest.raises(catalog.CatalogCancelled),
    ):
        catalog.fetch_to_cache(entry, cache_dir, cancel=_cancel, chunk_size=1 << 20)
    leftovers = list(cache_dir.iterdir()) if cache_dir.exists() else []
    assert leftovers == []


def test_fetch_to_cache_cached_entry_emits_terminal_progress(
    tmp_path: Path,
) -> None:
    """An already-cached entry still emits a ``progress(size, size)``
    so a UI that registered the request before the cache check sees
    a clean 100% terminal state instead of a stuck 0%."""
    payload = b"already-here"
    entry = _entry(payload)
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / entry.local_filename()).write_bytes(payload)
    progress_log: list[tuple[int, int | None]] = []
    catalog.fetch_to_cache(entry, image_root, progress=lambda d, t: progress_log.append((d, t)))
    assert progress_log == [(len(payload), len(payload))]


# -----------------------------------------------------------------------
# default_manifest_path() / default_cache_dir() env precedence
# -----------------------------------------------------------------------


def test_default_manifest_path_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTY_CATALOG_FILE", "/etc/bty/cat.toml")
    assert catalog.default_manifest_path() == Path("/etc/bty/cat.toml")


def test_default_manifest_path_falls_back_to_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BTY_CATALOG_FILE", raising=False)
    monkeypatch.setenv("BTY_STATE_DIR", str(tmp_path))
    assert catalog.default_manifest_path() is None  # not yet present
    (tmp_path / "catalog.toml").write_text("version = 1\n")
    assert catalog.default_manifest_path() == tmp_path / "catalog.toml"


# ``default_cache_dir`` removed in v0.31.0: there is no separate
# cache directory anymore -- catalog files land under the image_root
# with URL-derived ``catalog-<ref:12>-<slug>.<ext>`` filenames. The
# pertinent env-var tests live alongside ``default_image_root`` in
# tests/test_images.py.


# ---------- sha256 manifest parsing ----------------------------------------


def test_parse_sha256_manifest_single_bare_digest() -> None:
    """A manifest with one bare digest line (no filename column)
    parses; with no target_name, return that digest."""
    sha = "a" * 64
    assert catalog.parse_sha256_manifest(f"{sha}\n") == sha


def test_parse_sha256_manifest_sha256sum_format() -> None:
    """Standard ``<digest>  <filename>`` lines parse and a
    ``target_name`` lookup picks the right one."""
    body = (
        f"{'a' * 64}  ubuntu-22.04.img.gz\n"
        f"{'b' * 64}  debian-13.img.gz\n"
        f"{'c' * 64}  *./other.img.gz\n"
    )
    assert catalog.parse_sha256_manifest(body, "ubuntu-22.04.img.gz") == "a" * 64
    assert catalog.parse_sha256_manifest(body, "debian-13.img.gz") == "b" * 64
    # ``./`` and ``*`` filename prefixes are stripped (sha256sum
    # binary-mode marker / relative-path noise).
    assert catalog.parse_sha256_manifest(body, "other.img.gz") == "c" * 64


def test_parse_sha256_manifest_target_not_found_raises() -> None:
    body = f"{'a' * 64}  ubuntu.img.gz\n"
    with pytest.raises(catalog.CatalogError, match="does not list a digest"):
        catalog.parse_sha256_manifest(body, "missing.img.gz")


def test_parse_sha256_manifest_empty_raises() -> None:
    with pytest.raises(catalog.CatalogError, match="empty"):
        catalog.parse_sha256_manifest("\n\n  \n")


def test_parse_sha256_manifest_malformed_digest_raises() -> None:
    """A line whose first token isn't 64 hex chars rejects the
    whole manifest -- catches typos / wrong file uploaded as
    sha256-manifest."""
    with pytest.raises(catalog.CatalogError, match="malformed"):
        catalog.parse_sha256_manifest("not-a-digest  foo\n")


def test_fetch_sha256_for_url_rejects_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``sha_url`` that returns >1 MiB must be rejected without
    being parsed. Defends against the operator pasting an *image*
    URL into the sha_url field by accident: without the cap,
    a multi-GiB body would be read into memory before the parser
    rejected it as 'malformed digest'."""

    class _OversizeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._pos = 0

        def __enter__(self) -> _OversizeResp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self, n: int = -1) -> bytes:
            if n < 0 or n >= len(self._payload) - self._pos:
                chunk = self._payload[self._pos :]
                self._pos = len(self._payload)
                return chunk
            chunk = self._payload[self._pos : self._pos + n]
            self._pos += n
            return chunk

    # 2 MiB of garbage; the manager should bail at the cap.
    huge = b"x" * (2 * 1024 * 1024)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _OversizeResp(huge))
    with pytest.raises(catalog.CatalogError, match="larger than"):
        catalog.fetch_sha256_for_url(
            "https://example.invalid/foo.img.gz",
            "https://example.invalid/foo.img.gz.sha256",
        )


# --------------------------------------------------------------------------
# sha256 is optional for oras:// entries (rolling-tag portable catalogs)
# --------------------------------------------------------------------------


def test_catalog_entry_accepts_oras_src_without_sha256() -> None:
    """``oras://`` entries can omit ``sha256``: the OCI manifest
    carries a layer digest that bty resolves and verifies at flash
    time. Pre-pinning would freeze a rolling tag to whatever was
    current at catalog-publish time -- the whole point of rolling
    is that the operator gets nosi's latest rebuild without a bty
    re-release."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "nosi-debian-sysdev",
            "src": "oras://ghcr.io/safl/nosi/debian-sysdev:latest",
            "format": "img.gz",
        }
    )
    assert entry.sha256 is None
    assert entry.src == "oras://ghcr.io/safl/nosi/debian-sysdev:latest"
    assert entry.format == "img.gz"


def test_catalog_entry_accepts_http_src_without_sha256() -> None:
    """http(s) entries can also be sha-less in the portable catalog
    use case: a rolling release-asset URL like
    ``github.com/.../releases/latest/download/...`` has no stable
    sha at catalog-publish time either. The flash path relies on
    TLS for integrity; bty-web's manifest cache enforces "sha
    required" at use-time via ``cached_path``."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "debian-13-server-latest",
            "src": "https://example.com/debian-13-server.img.gz",
            "format": "img.gz",
        }
    )
    assert entry.sha256 is None
    assert entry.src.startswith("https://")


def test_catalog_entry_cached_path_works_without_sha(tmp_path: Path) -> None:
    """v0.31.0+: the on-disk filename is URL-derived
    (``catalog-<ref:12>-<slug>.<ext>``), not content-sha-derived, so
    ``cached_path`` returns a usable path even for ``oras://`` rolling-
    tag entries with no pinned digest. Filename is stable and unique
    across all entries by construction (URLs canonicalise to distinct
    refs)."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "no-sha",
            "src": "oras://ghcr.io/owner/repo:latest",
        }
    )
    path = entry.cached_path(tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("catalog-")
    assert path.name == entry.local_filename()


def test_catalog_entry_accepts_explicit_null_sha256() -> None:
    """``sha256 = null`` in the TOML maps to a Python ``None``;
    treat it the same as an absent key. Operators who author the
    catalog by hand might write the explicit null for clarity."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "explicit-null",
            "src": "oras://ghcr.io/owner/repo:latest",
            "sha256": None,
        }
    )
    assert entry.sha256 is None


# -----------------------------------------------------------------------
# local_filename_for / _slugify edge cases (v0.31.0+)
# -----------------------------------------------------------------------
#
# ``local_filename_for`` is pure math over (ref, name, format) and lands
# on disk -- every variant the operator-typed ``name`` field can take
# must produce a portable filename. The ref-prefix carries uniqueness;
# the slug is purely cosmetic ``ls`` legibility, so its rules are kept
# small and predictable.


def test_local_filename_for_basic_shape() -> None:
    """The canonical shape is ``catalog-<ref:12>-<slug>.<ext>``: a 12-char
    ref prefix (lower-hex), a slug derived from the operator-typed name,
    and the format as the extension. Anchoring this lets the discovery
    code regex against it confidently."""
    ref = "a" * 64
    f = catalog.local_filename_for(ref, "fedora-sysdev", "img.gz")
    assert f == "catalog-aaaaaaaaaaaa-fedora-sysdev.img.gz"


def test_local_filename_for_unicode_name_slugs_to_ascii() -> None:
    """Operator-typed names can contain unicode (catalog publishers
    are international); the slug strips to lower-case ASCII alnum +
    hyphen + underscore so the filename stays portable across
    filesystems (NTFS, exFAT, ext4 with restrictive locales, FAT32)."""
    ref = "0" * 64
    f = catalog.local_filename_for(ref, "smart “quotes”", "img")
    # Unicode chars collapse to one hyphen each (no semantic loss --
    # uniqueness lives in the ref-prefix).
    assert f.startswith("catalog-000000000000-")
    # No bytes outside [a-z0-9_-].
    slug_and_ext = f[len("catalog-000000000000-") :]
    assert all(c.isalnum() or c in "-_." for c in slug_and_ext)


def test_local_filename_for_very_long_name_no_truncation() -> None:
    """A long catalog name produces a long filename; we do NOT truncate
    (collision-by-truncation would silently merge two entries with
    different long names but identical prefixes -- worse than a long
    path). Filesystems used in practice (ext4, exfat, NTFS) all support
    names well past 200 chars."""
    ref = "f" * 64
    long_name = "x" * 200
    f = catalog.local_filename_for(ref, long_name, "img.gz")
    assert f == f"catalog-{'f' * 12}-{long_name}.img.gz"
    # Length sanity: under typical filesystems' per-component cap (255).
    assert len(f) < 255


def test_local_filename_for_collapses_consecutive_separators() -> None:
    """The slug collapses runs of non-alnum-underscore characters to a
    single hyphen so a name like ``foo   bar`` doesn't become
    ``foo---bar`` -- nicer ``ls`` output, and stable across whatever
    whitespace the operator typed."""
    ref = "1" * 64
    f = catalog.local_filename_for(ref, "foo   bar  (rev 2)", "img")
    # Whitespace + parens + space collapse to single hyphens; trailing
    # hyphens stripped.
    assert f == "catalog-111111111111-foo-bar-rev-2.img"


def test_local_filename_for_strips_leading_dot_from_format() -> None:
    """The ``format`` field is occasionally stored with a leading dot
    (``.img.gz``) by mistake or from a stricter producer; the helper
    tolerates it so the filename doesn't end up as ``...img.gz..img.gz``
    or similar."""
    ref = "2" * 64
    f = catalog.local_filename_for(ref, "name", ".img.gz")
    assert f == "catalog-222222222222-name.img.gz"


def test_local_filename_for_format_none_defaults_to_img() -> None:
    """A catalog entry with no ``format`` set (rare; ``from_dict``
    defaults it from the name's extension) still produces a usable
    filename. ``img`` is the catch-all so the result is a readable
    placeholder rather than a name ending in a bare dot."""
    ref = "3" * 64
    f = catalog.local_filename_for(ref, "no-format", None)
    assert f == "catalog-333333333333-no-format.img"


def test_local_filename_for_empty_name_falls_back_to_image_slug() -> None:
    """An empty / all-non-ASCII name slugs to the literal string
    ``image`` so the filename keeps the canonical shape (the ref-
    prefix still disambiguates -- two empty-name entries with
    different URLs land at different filenames)."""
    ref = "4" * 64
    f = catalog.local_filename_for(ref, "ÿÿÿ", "img.gz")
    assert f == "catalog-444444444444-image.img.gz"


def test_local_filename_for_same_inputs_idempotent() -> None:
    """Same (ref, name, format) -> same filename, every call. This is
    the on-disk dedup contract -- fetch_to_cache writing a second time
    overwrites the same file rather than producing a parallel."""
    ref = "5" * 64
    a = catalog.local_filename_for(ref, "demo", "img")
    b = catalog.local_filename_for(ref, "demo", "img")
    assert a == b


def test_local_filename_for_different_urls_distinct_filenames() -> None:
    """Two distinct ``src`` URLs canonicalise to two distinct refs (by
    construction, in :func:`image_ref_for_src`); the local_filename
    derives from the ref so they end up at distinct filenames even
    if the operator-typed ``name`` is identical. This is what rules
    out on-disk collisions between catalog entries."""
    ref_a = catalog.image_ref_for_src("oras://ghcr.io/owner/repo-a:latest")
    ref_b = catalog.image_ref_for_src("oras://ghcr.io/owner/repo-b:latest")
    f_a = catalog.local_filename_for(ref_a, "shared-name", "img")
    f_b = catalog.local_filename_for(ref_b, "shared-name", "img")
    assert f_a != f_b
    assert f_a.endswith("-shared-name.img")
    assert f_b.endswith("-shared-name.img")


# ---------------------------------------------------------------------------
# Canonicalisation + image-ref derivation.
# Every per-scheme canonicalisation rule is covered here so a future
# contributor can see at a glance whether their input falls in or out
# of scope.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("file://debian.img.gz", "file://debian.img.gz"),
        ("file://./debian.img.gz", "file://debian.img.gz"),  # ./ stripped
        ("file://a//b.img.gz", "file://a/b.img.gz"),  # // collapsed
        ("file:///debian.img.gz", "file://debian.img.gz"),  # leading / stripped
        ("file://topic/bar.img.gz", "file://topic/bar.img.gz"),  # subdir preserved
        ("file://A/B/CamelCase.img.gz", "file://A/B/CamelCase.img.gz"),  # case preserved
        ("file://a/./b/c.img.gz", "file://a/b/c.img.gz"),  # mid-path . stripped
    ],
)
def test_canonicalise_src_file_scheme(raw: str, expected: str) -> None:
    assert catalog.canonicalise_src(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "file://../etc/passwd",  # ".." segment
        "file://a/../../etc/passwd",  # ".." mid-path
        "file://",  # empty path
        "file://./.",  # normalises empty
        "file:///",  # leading slash only
        "file://debian\x00.img.gz",  # NUL byte
    ],
)
def test_canonicalise_src_file_scheme_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        catalog.canonicalise_src(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/foo", "https://example.com/foo"),
        ("https://EXAMPLE.com/foo", "https://example.com/foo"),  # host lowered
        ("HTTPS://example.com/foo", "https://example.com/foo"),  # scheme lowered
        ("https://example.com:443/foo", "https://example.com/foo"),  # default port stripped
        ("http://example.com:80/foo", "http://example.com/foo"),  # default port stripped
        ("https://example.com:8080/foo", "https://example.com:8080/foo"),  # non-default kept
        ("https://example.com/Path/Foo.GZ", "https://example.com/Path/Foo.GZ"),  # case preserved
        ("https://example.com/p?a=1&b=2", "https://example.com/p?a=1&b=2"),  # query kept
        ("https://example.com/p/", "https://example.com/p/"),  # trailing / preserved
        ("https://example.com/p%20q", "https://example.com/p%20q"),  # %-encoding kept
    ],
)
def test_canonicalise_src_http_scheme(raw: str, expected: str) -> None:
    assert catalog.canonicalise_src(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "oras://ghcr.io/safl/foo:latest",
            "oras://ghcr.io/safl/foo:latest",
        ),
        (
            "oras://GHCR.IO/safl/foo:latest",
            "oras://ghcr.io/safl/foo:latest",  # host lowered
        ),
        (
            "oras://ghcr.io/SAFL/Foo:latest",
            "oras://ghcr.io/safl/foo:latest",  # repository lowered
        ),
        (
            "oras://ghcr.io/safl/foo:LATEST",
            "oras://ghcr.io/safl/foo:LATEST",  # tag preserved
        ),
        (
            "oras://ghcr.io/safl/foo@sha256:" + "a" * 64,
            "oras://ghcr.io/safl/foo@sha256:" + "a" * 64,
        ),
    ],
)
def test_canonicalise_src_oras_scheme(raw: str, expected: str) -> None:
    assert catalog.canonicalise_src(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/foo.img.gz",  # unsupported scheme
        "",  # empty
        "/var/lib/bty/images/foo.img.gz",  # bare path -- no scheme
        "https://",  # missing host
        "oras://ghcr.io/foo:latest",  # missing owner (single segment)
    ],
)
def test_canonicalise_src_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        catalog.canonicalise_src(raw)


def test_image_ref_for_src_is_sha256_hex() -> None:
    """The ref is always a 64-char lowercase hex string."""
    ref = catalog.image_ref_for_src("file://debian.img.gz")
    assert len(ref) == 64
    assert all(c in "0123456789abcdef" for c in ref)


def test_image_ref_for_src_dedupes_trivial_variations() -> None:
    """Variations the canonicaliser normalises away produce the same ref."""
    base = catalog.image_ref_for_src("https://example.com/foo.img.gz")
    assert catalog.image_ref_for_src("https://EXAMPLE.com/foo.img.gz") == base
    assert catalog.image_ref_for_src("HTTPS://example.com/foo.img.gz") == base
    assert catalog.image_ref_for_src("https://example.com:443/foo.img.gz") == base


def test_image_ref_for_src_distinguishes_path_case_on_file_scheme() -> None:
    """file:// paths are case-sensitive (Linux filesystems are)."""
    a = catalog.image_ref_for_src("file://Debian.img.gz")
    b = catalog.image_ref_for_src("file://debian.img.gz")
    assert a != b


def test_image_ref_for_src_distinguishes_oras_tag_case() -> None:
    """OCI tags are case-sensitive per spec, even though most registries
    treat them as case-insensitive. We preserve operator input."""
    a = catalog.image_ref_for_src("oras://ghcr.io/safl/foo:latest")
    b = catalog.image_ref_for_src("oras://ghcr.io/safl/foo:LATEST")
    assert a != b


def test_image_ref_for_src_dedupes_oras_host_case() -> None:
    """OCI hosts are DNS-style; case-insensitive."""
    a = catalog.image_ref_for_src("oras://GHCR.IO/safl/foo:latest")
    b = catalog.image_ref_for_src("oras://ghcr.io/safl/foo:latest")
    assert a == b


def test_image_ref_for_src_distinguishes_trailing_slash_on_http() -> None:
    """RFC says these are different resources; we preserve."""
    a = catalog.image_ref_for_src("https://example.com/foo")
    b = catalog.image_ref_for_src("https://example.com/foo/")
    assert a != b


def test_image_ref_for_src_distinguishes_local_vs_remote_with_same_name() -> None:
    """``file://debian.img.gz`` and ``https://.../debian.img.gz`` are
    different catalog identities even if they end up holding the same
    bytes -- different provenance, different refs."""
    a = catalog.image_ref_for_src("file://debian.img.gz")
    b = catalog.image_ref_for_src("https://example.com/debian.img.gz")
    assert a != b


# ---------- storage-format marker -------------------------------------------


def test_storage_marker_written_on_fresh_image_root(tmp_path: Path) -> None:
    """First call against a fresh image_root creates
    ``.bty-storage.json`` carrying the current STORAGE_FORMAT_VERSION
    + a creation timestamp + the bty version that wrote it
    (diagnostic only)."""
    from bty.catalog import STORAGE_FORMAT_VERSION, check_or_write_storage_marker

    v = check_or_write_storage_marker(tmp_path)
    assert v == STORAGE_FORMAT_VERSION
    marker = tmp_path / ".bty-storage.json"
    import json as _json

    data = _json.loads(marker.read_text(encoding="utf-8"))
    assert data["format_version"] == STORAGE_FORMAT_VERSION
    assert "created_at" in data
    assert "created_by_bty_version" in data


def test_storage_marker_idempotent_on_match(tmp_path: Path) -> None:
    """A second call against the same marker is a no-op: doesn't
    rewrite the marker (created_at would otherwise drift)."""
    from bty.catalog import check_or_write_storage_marker

    check_or_write_storage_marker(tmp_path)
    marker = tmp_path / ".bty-storage.json"
    first = marker.read_text(encoding="utf-8")
    check_or_write_storage_marker(tmp_path)
    second = marker.read_text(encoding="utf-8")
    assert first == second, "matching-version call must not rewrite the marker"


def test_storage_marker_rejects_mismatch(tmp_path: Path) -> None:
    """A marker stamped with a different format_version raises
    StorageFormatMismatch with operator-facing remediation text."""
    import json as _json

    from bty.catalog import StorageFormatMismatch, check_or_write_storage_marker

    (tmp_path / ".bty-storage.json").write_text(
        _json.dumps({"format_version": 99, "created_at": "fake", "created_by_bty_version": "x"})
    )
    with pytest.raises(StorageFormatMismatch) as exc:
        check_or_write_storage_marker(tmp_path)
    msg = str(exc.value)
    # The message names both versions + tells the operator what to do.
    assert "v99" in msg
    assert "Alt+F2" in msg or "shell" in msg
    assert "archive" in msg or "wipe" in msg or "rm" in msg.lower()


def test_storage_marker_rejects_malformed_json(tmp_path: Path) -> None:
    """A corrupt / non-JSON marker file raises StorageFormatMismatch
    rather than crashing bty-web at startup. Could happen if a
    partial write was interrupted (cosmic ray, OOM kill mid-flush)."""
    from bty.catalog import StorageFormatMismatch, check_or_write_storage_marker

    (tmp_path / ".bty-storage.json").write_text("this is not json {{{ ")
    with pytest.raises(StorageFormatMismatch, match="unreadable / malformed"):
        check_or_write_storage_marker(tmp_path)


# ---------- image-store filename recognition --------------------------------


def test_recognised_filenames_accept_documented_shapes(tmp_path: Path) -> None:
    """Every name the storage layer documents as legal must be
    recognised. New file types added to the convention need a
    matching predicate update."""
    from bty.catalog import is_recognised_image_store_filename

    legal = [
        "catalog-deadbeefcafe-nosi-fedora.img.gz",  # catalog cache
        "operator.img.gz",  # operator-typed (img.gz format)
        "operator.qcow2",  # operator-typed (qcow2 format)
        "operator.img",  # operator-typed (raw img format)
        "any-image-name.img.gz.sha256",  # sidecar
        "any-image-name.img.gz.partial",  # streamed upload in progress
        ".bty-storage.json",  # marker itself
        ".abc12345.tmp-xyz",  # mid-fetch tempfile
    ]
    for name in legal:
        assert is_recognised_image_store_filename(name), name


def test_recognised_filenames_reject_unconventional(tmp_path: Path) -> None:
    """Operator-dropped notes, README, random scripts -- none of
    these match an image extension or sidecar pattern. The lifespan
    survey warns about them so the operator can clean up."""
    from bty.catalog import is_recognised_image_store_filename

    bad = [
        "README.md",
        "notes.txt",
        "screenshot.png",
        "some-binary",
        "test.json",  # the .bty-storage.json hidden marker is OK, this isn't
    ]
    for name in bad:
        assert not is_recognised_image_store_filename(name), name
