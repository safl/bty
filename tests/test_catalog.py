"""Tests for ``bty.catalog`` (M22, v1).

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
    assert entry is not None and entry.format == "qcow2"


def test_load_rejects_unknown_version(tmp_path: Path) -> None:
    path = _write(tmp_path / "catalog.toml", "version = 99\n")
    with pytest.raises(catalog.CatalogError, match="version"):
        catalog.load(path)


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    body = """
        version = 1
        [[images]]
        name = "incomplete"
        src = "https://example.com/incomplete"
    """
    path = _write(tmp_path / "catalog.toml", body)
    with pytest.raises(catalog.CatalogError, match="sha256"):
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
    cache_dir = tmp_path / "cache"
    with patch("urllib.request.urlopen", _mock_urlopen(payload)):
        cached = catalog.fetch_to_cache(entry, cache_dir)
    assert cached.is_file()
    assert cached.read_bytes() == payload
    assert cached.name == entry.sha256


def test_fetch_to_cache_idempotent(tmp_path: Path) -> None:
    payload = b"fake image bytes"
    entry = _entry(payload)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Pre-populate the cache as if we had fetched before.
    target = cache_dir / entry.sha256
    target.write_bytes(payload)

    # urlopen would error if called; idempotency means it isn't.
    def _boom(*_a, **_kw):
        raise AssertionError("urlopen should not be called when cached")

    with patch("urllib.request.urlopen", _boom):
        cached = catalog.fetch_to_cache(entry, cache_dir)
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
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    entry = _entry(b"x")
    assert not catalog.is_cached(entry, cache_dir)
    (cache_dir / entry.sha256).write_bytes(b"x")
    assert catalog.is_cached(entry, cache_dir)


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
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / entry.sha256).write_bytes(payload)
    progress_log: list[tuple[int, int | None]] = []
    catalog.fetch_to_cache(entry, cache_dir, progress=lambda d, t: progress_log.append((d, t)))
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


def test_default_cache_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BTY_CATALOG_CACHE_DIR", raising=False)
    assert catalog.default_cache_dir() == tmp_path / "cache"
    monkeypatch.setenv("BTY_CATALOG_CACHE_DIR", "/var/cache/bty")
    assert catalog.default_cache_dir() == Path("/var/cache/bty")


# ---------- sha256 manifest parsing (M23) ----------------------------------


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
