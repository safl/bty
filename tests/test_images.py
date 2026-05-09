"""Tests for bty.images. Image files are fabricated under tmp_path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bty import images


def _touch(path: Path, size: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def test_detect_format() -> None:
    assert images.detect_format(Path("foo.qcow2")) == "qcow2"
    assert images.detect_format(Path("foo.img")) == "img"
    assert images.detect_format(Path("foo.img.zst")) == "img.zst"
    assert images.detect_format(Path("foo.img.xz")) == "img.xz"
    assert images.detect_format(Path("foo.img.gz")) == "img.gz"
    assert images.detect_format(Path("foo.img.bz2")) == "img.bz2"
    assert images.detect_format(Path("foo.iso")) is None
    assert images.detect_format(Path("Foo.QCOW2")) == "qcow2"


def test_detect_format_prefers_multi_suffix_over_bare_img() -> None:
    """When the filename ends in ``.img.<algo>``, the multi-suffix
    entry wins over the bare ``.img`` entry. Important for the
    flash-code dispatcher: detecting "img" on a "debian.img.gz"
    would route through the raw-img writer and dd compressed bytes
    onto the target."""
    assert images.detect_format(Path("debian.img.zst")) == "img.zst"
    assert images.detect_format(Path("debian.img.xz")) == "img.xz"
    assert images.detect_format(Path("debian.img.gz")) == "img.gz"
    assert images.detect_format(Path("debian.img.bz2")) == "img.bz2"


def test_list_images_walks_root(tmp_path: Path) -> None:
    _touch(tmp_path / "alpha.qcow2", size=1024)
    _touch(tmp_path / "beta.img", size=2048)
    _touch(tmp_path / "gamma.img.zst", size=4096)
    _touch(tmp_path / "ignored.iso", size=8192)
    _touch(tmp_path / "subdir/nested.qcow2")  # non-recursive: should be ignored

    found = images.list_images(tmp_path)
    names = [img.name for img in found]
    assert names == ["alpha.qcow2", "beta.img", "gamma.img.zst"]

    by_name = {img.name: img for img in found}
    assert by_name["alpha.qcow2"].format == "qcow2"
    assert by_name["beta.img"].format == "img"
    assert by_name["gamma.img.zst"].format == "img.zst"
    assert by_name["alpha.qcow2"].size_bytes == 1024


def test_list_images_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert images.list_images(tmp_path / "nonexistent") == []


def test_inspect_image_qcow2_invokes_qemu_img(tmp_path: Path) -> None:
    img = _touch(tmp_path / "x.qcow2", size=10)
    fake_proc = MagicMock()
    fake_proc.stdout = '{"virtual-size": 12345, "format": "qcow2"}'
    fake_proc.returncode = 0
    with patch("bty.images.subprocess.run", return_value=fake_proc) as run:
        info = images.inspect_image(img)
    run.assert_called_once()
    assert run.call_args.args[0][0:2] == ["qemu-img", "info"]
    assert info["format"] == "qcow2"
    assert info["detail"] == {"virtual-size": 12345, "format": "qcow2"}
    assert info["size_bytes"] == 10


def test_inspect_image_zst_invokes_zstd(tmp_path: Path) -> None:
    img = _touch(tmp_path / "x.img.zst", size=10)
    fake_proc = MagicMock()
    fake_proc.stdout = "Frames Compressed Uncompressed Ratio Check Filename\n..."
    fake_proc.returncode = 0
    with patch("bty.images.subprocess.run", return_value=fake_proc) as run:
        info = images.inspect_image(img)
    assert run.call_args.args[0][0] == "zstd"
    assert "detail" in info


def test_inspect_image_raw_img_no_external_tool(tmp_path: Path) -> None:
    img = _touch(tmp_path / "x.img", size=10)
    with patch("bty.images.subprocess.run") as run:
        info = images.inspect_image(img)
    run.assert_not_called()
    assert info["format"] == "img"
    assert info["size_bytes"] == 10


def test_inspect_image_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        images.inspect_image(tmp_path / "nope.qcow2")


# -----------------------------------------------------------------------
# SHA-256 sidecar caching (M22)
# -----------------------------------------------------------------------


def test_list_images_skips_sidecar_files(tmp_path: Path) -> None:
    """``foo.img.zst.sha256`` is not itself an image. Bare directory
    listings should ignore it."""
    _touch(tmp_path / "foo.img.zst", size=64)
    (tmp_path / "foo.img.zst.sha256").write_text("0" * 64 + "  foo.img.zst\n")
    found = images.list_images(tmp_path)
    assert [img.name for img in found] == ["foo.img.zst"]


def test_list_images_reads_sidecar_sha(tmp_path: Path) -> None:
    """Sidecar present + valid -> ``Image.sha256`` populated."""
    _touch(tmp_path / "foo.img", size=32)
    sha = "deadbeef" * 8  # 64 hex chars
    (tmp_path / "foo.img.sha256").write_text(f"{sha}  foo.img\n")
    found = images.list_images(tmp_path)
    assert found[0].sha256 == sha


def test_list_images_no_sidecar_means_none(tmp_path: Path) -> None:
    """No sidecar -> ``Image.sha256`` is None (lazy compute)."""
    _touch(tmp_path / "foo.img", size=32)
    found = images.list_images(tmp_path)
    assert found[0].sha256 is None


def test_list_images_rejects_bad_sidecar(tmp_path: Path) -> None:
    """A sidecar that doesn't look like a SHA-256 is treated as
    absent (None) -- we don't crash on operator typos."""
    _touch(tmp_path / "foo.img", size=32)
    (tmp_path / "foo.img.sha256").write_text("NOT-A-SHA\n")
    found = images.list_images(tmp_path)
    assert found[0].sha256 is None


def test_merge_with_catalog_dedupes_by_sha(tmp_path: Path) -> None:
    """A directory-scan image and a manifest entry with the same
    SHA collapse into one ``UnifiedImage`` whose ``names`` and
    ``sources`` arrays carry both sides."""
    # Stub catalog entry shape; merge_with_catalog uses structural
    # access (``entry.sha256`` / ``entry.name`` / ``entry.src`` /
    # ``entry.format`` / ``entry.size_bytes``) so a plain
    # SimpleNamespace is enough.
    from types import SimpleNamespace

    image_root = tmp_path / "imgs"
    cache_dir = tmp_path / "cache"
    image_root.mkdir()
    cache_dir.mkdir()
    sha = "a" * 64
    _touch(image_root / "local.img", size=8)
    (image_root / "local.img.sha256").write_text(f"{sha}  local.img\n")

    manifest = SimpleNamespace(
        sha256=sha,
        name="upstream.img",
        src="https://example.com/upstream.img",
        format="img",
        size_bytes=8,
    )
    merged = images.merge_with_catalog(image_root, [manifest], cache_dir)
    assert len(merged) == 1
    u = merged[0]
    assert u.sha256 == sha
    assert set(u.names) == {"local.img", "upstream.img"}
    kinds = sorted(s.kind for s in u.sources)
    assert kinds == ["local", "manifest"]
    assert u.cached is True  # local file makes it cached


def test_merge_with_catalog_unhashed_dirscan_kept_separate(tmp_path: Path) -> None:
    """An unhashed dir-scan file (no sidecar) becomes its own
    UnifiedImage entry with sha256=None. Cannot dedupe by SHA so
    we keep it visible -- the operator can find it + trigger
    hashing -- but it sorts after the SHA-keyed entries."""
    from types import SimpleNamespace

    image_root = tmp_path / "imgs"
    cache_dir = tmp_path / "cache"
    image_root.mkdir()
    cache_dir.mkdir()
    _touch(image_root / "fresh.img", size=8)  # no sidecar

    manifest_sha = "b" * 64
    manifest = SimpleNamespace(
        sha256=manifest_sha,
        name="other.img",
        src="https://example.com/other.img",
        format="img",
        size_bytes=64,
    )
    merged = images.merge_with_catalog(image_root, [manifest], cache_dir)
    # SHA-keyed first, unhashed last.
    assert merged[0].sha256 == manifest_sha
    assert merged[1].sha256 is None
    assert merged[1].names == ("fresh.img",)


def test_merge_with_catalog_manifest_only_uses_cache_dir(tmp_path: Path) -> None:
    """A manifest entry with no matching local file shows
    ``cached=True`` iff the SHA exists under the content-
    addressed cache directory."""
    from types import SimpleNamespace

    image_root = tmp_path / "imgs"
    cache_dir = tmp_path / "cache"
    image_root.mkdir()
    cache_dir.mkdir()
    sha = "c" * 64
    (cache_dir / sha).write_bytes(b"cached blob")

    manifest = SimpleNamespace(
        sha256=sha,
        name="cached.img",
        src="https://example.com/cached.img",
        format="img",
        size_bytes=11,
    )
    merged = images.merge_with_catalog(image_root, [manifest], cache_dir)
    assert merged[0].sha256 == sha
    assert merged[0].cached is True

    # Same manifest entry but the cache file is absent.
    (cache_dir / sha).unlink()
    merged2 = images.merge_with_catalog(image_root, [manifest], cache_dir)
    assert merged2[0].cached is False


def test_ensure_sha256_computes_and_writes_sidecar(tmp_path: Path) -> None:
    """First call hashes the file + writes the sidecar; second call
    is O(1) because the sidecar is cached."""
    import hashlib

    payload = b"hello bty " * 100
    img = tmp_path / "foo.img"
    img.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    sidecar = tmp_path / "foo.img.sha256"
    assert not sidecar.exists()

    digest = images.ensure_sha256(img)
    assert digest == expected
    assert sidecar.is_file()
    # sha256sum-compatible format: ``<digest>  <filename>``.
    assert sidecar.read_text().strip().split()[0] == expected
    assert sidecar.read_text().strip().split()[1] == "foo.img"

    # Second call: sidecar should be honoured without recomputing.
    # We monkey-prove this by overwriting the file but leaving the
    # sidecar -- if ensure_sha256 re-hashed, the digest would change.
    img.write_bytes(b"different bytes")
    second = images.ensure_sha256(img)
    assert second == expected  # cached, not recomputed
