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

from pathlib import Path

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


def test_catalog_entry_arch_falls_back_to_filename_heuristic() -> None:
    """No ``arch =`` declared: the parser back-fills from the
    filename heuristic so display surfaces never show ``?`` for
    images whose name carries an arch token."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "debian-13-amd64.qcow2.zst",
            "src": "https://example.com/debian-13-amd64.qcow2.zst",
        }
    )
    assert entry.arch == "x86_64"


def test_catalog_entry_arch_explicit_field_wins_over_heuristic() -> None:
    """An explicit ``arch =`` value declared by the catalog publisher
    (the case the nosi catalog will use once its publisher is updated)
    is treated as authoritative. The filename heuristic only fills the
    field when the publisher said nothing."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "ambiguous-name.img.gz",
            "src": "https://example.com/ambiguous-name.img.gz",
            "arch": "riscv64",
        }
    )
    assert entry.arch == "riscv64"


def test_catalog_entry_arch_none_when_neither_source_resolves() -> None:
    """No ``arch =`` and no recognised filename token: ``None``,
    which the display layers render as ``?`` / ``-``. Better than
    guessing and putting the wrong value in front of the operator."""
    entry = catalog.CatalogEntry.from_dict(
        {
            "name": "appliance.qcow2",
            "src": "https://example.com/appliance.qcow2",
        }
    )
    assert entry.arch is None


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
