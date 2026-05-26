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


def test_list_images_skips_symlinks(tmp_path: Path) -> None:
    """Symlinks could point outside ``root``; serving their bytes
    via ``GET /images/<sha>`` would let the operator inadvertently
    expose files outside the configured image root. Listing skips
    them defensively."""
    real = tmp_path / "outside"
    real.mkdir()
    (real / "secret.qcow2").write_bytes(b"\0" * 16)
    inside = tmp_path / "images"
    inside.mkdir()
    (inside / "real.qcow2").write_bytes(b"\0" * 16)
    (inside / "linked.qcow2").symlink_to(real / "secret.qcow2")

    found = images.list_images(inside)
    names = [img.name for img in found]
    assert names == ["real.qcow2"]


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


def test_inspect_image_qcow2_unparseable_json_is_detail_error(tmp_path: Path) -> None:
    """``qemu-img info`` can exit 0 yet emit non-JSON (truncated /
    half-understood image). The inspect must not 500: it folds the
    decode failure into ``detail_error`` and leaves ``detail`` unset."""
    img = _touch(tmp_path / "x.qcow2", size=10)
    fake_proc = MagicMock()
    fake_proc.stdout = "not json at all {"
    fake_proc.returncode = 0
    with patch("bty.images.subprocess.run", return_value=fake_proc):
        info = images.inspect_image(img)
    assert "detail" not in info
    assert "detail_error" in info
    assert "unparseable" in info["detail_error"].lower()
    assert info["format"] == "qcow2"


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


def test_inspect_image_hints_about_tarballs(tmp_path: Path) -> None:
    """``inspect_image(foo.tar.gz)`` doesn't return a confusing
    blank record; instead it surfaces a friendly ``detail_error``
    that tells the operator to extract first. Surfaced in the
    wizard's flash-plan-rejected panel."""
    tarball = tmp_path / "ubuntu-22.04.tar.gz"
    tarball.write_bytes(b"\x1f\x8b" + b"\0" * 30)  # gzip magic + padding
    info = images.inspect_image(tarball)
    assert info["format"] is None
    assert "tarball" in info.get("detail_error", "").lower()
    assert "extract" in info["detail_error"].lower()


def test_inspect_image_hints_about_unrecognised_extensions(tmp_path: Path) -> None:
    """``inspect_image(README.md)`` (a real but non-image file)
    returns a clear ``detail_error`` naming the supported
    extensions, rather than a confusing blank ``format: ''``
    record. The wizard surfaces ``detail_error`` so the operator
    sees the actionable hint."""
    other = tmp_path / "README.md"
    other.write_text("# notes\n")
    info = images.inspect_image(other)
    assert info["format"] is None
    err = info.get("detail_error", "").lower()
    assert "unrecognised" in err
    # The hint lists at least the main supported formats.
    assert ".qcow2" in err
    assert ".img.gz" in err


def test_inspect_image_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        images.inspect_image(tmp_path / "nope.qcow2")


# -----------------------------------------------------------------------
# SHA-256 sidecar caching
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
    merged = images.merge_with_catalog(image_root, [manifest])
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
    merged = images.merge_with_catalog(image_root, [manifest])
    # SHA-keyed first, unhashed last.
    assert merged[0].sha256 == manifest_sha
    assert merged[1].sha256 is None
    assert merged[1].names == ("fresh.img",)


def test_merge_with_catalog_handles_manifest_entry_without_sha256(tmp_path: Path) -> None:
    """Manifest entries without a pinned sha256 (rolling oras tags,
    operator-added URLs without sha_url) appear in the unhashed
    tail with sha256=None and cached=False -- they don't break
    the merge with a ``cache_dir / None`` TypeError, which used to
    500 /ui/images whenever the catalog.toml had any unhashed
    entry."""
    from types import SimpleNamespace

    image_root = tmp_path / "imgs"
    cache_dir = tmp_path / "cache"
    image_root.mkdir()
    cache_dir.mkdir()

    # Manifest entry with sha=None (rolling oras ref).
    rolling = SimpleNamespace(
        sha256=None,
        name="nosi rolling",
        src="oras://ghcr.io/safl/nosi/x:latest",
        format="img.gz",
        size_bytes=None,
    )
    # And a pinned entry to confirm the other branch still works.
    pinned_sha = "c" * 64
    pinned = SimpleNamespace(
        sha256=pinned_sha,
        name="pinned.img",
        src="https://example.com/pinned.img",
        format="img",
        size_bytes=128,
    )
    merged = images.merge_with_catalog(image_root, [rolling, pinned])
    # SHA-keyed first, unhashed last.
    assert len(merged) == 2
    assert merged[0].sha256 == pinned_sha
    assert merged[1].sha256 is None
    assert merged[1].names == ("nosi rolling",)
    assert merged[1].cached is False


def test_merge_with_catalog_manifest_only_uses_image_root(tmp_path: Path) -> None:
    """A manifest entry with no matching operator-typed local file shows
    ``cached=True`` iff the URL-keyed ``catalog-<ref:12>-<slug>.<ext>``
    file exists under the image_root. v0.31.0+: no separate cache_dir;
    catalog-fetched files live alongside operator-typed ones."""
    from types import SimpleNamespace

    from bty.catalog import image_ref_for_src, local_filename_for

    image_root = tmp_path / "imgs"
    image_root.mkdir()
    src = "https://example.com/cached.img"
    name = "cached.img"
    fmt = "img"
    ref = image_ref_for_src(src)
    cached_filename = local_filename_for(ref, name, fmt)
    (image_root / cached_filename).write_bytes(b"cached blob")

    manifest = SimpleNamespace(
        sha256="c" * 64,
        name=name,
        src=src,
        format=fmt,
        size_bytes=11,
    )
    merged = images.merge_with_catalog(image_root, [manifest])
    assert merged[0].sha256 == "c" * 64
    assert merged[0].cached is True

    # Same manifest entry but the cached file is absent.
    (image_root / cached_filename).unlink()
    merged2 = images.merge_with_catalog(image_root, [manifest])
    assert merged2[0].cached is False


def test_merge_with_catalog_picks_up_sidecar_for_uncached_entry(tmp_path: Path) -> None:
    """REGRESSION (v0.33.17): a catalog entry whose
    ``catalog-<ref:12>-<slug>.<ext>`` cache file exists in image_root
    AND has a ``.sha256`` sidecar must surface with the SIDECAR's
    sha as the ``UnifiedImage.sha256`` even when the catalog row
    itself carries ``sha256=None``.

    Surfaced by the appliance-upgrade integration test: an operator
    upgrades a bty-web with a separate state disk (image_root
    survives). The cached file + its sidecar are still on disk;
    they were hashed by the old release's HashManager. The new
    bty-web's auto-import skips the catalog-prefixed file
    (v0.33.1), so the file's sha never reaches the catalog row's
    ``disk_image_sha`` -- it stays NULL. Pre-this-fix, /images then
    defensively dropped the entry (cached=True + sha=None is
    impossible -- except it WASN'T after the v0.33.1 skip).

    With this fix the merge reads the sidecar at cache-hit time, so
    the sha lands on the UnifiedImage even when the catalog row
    hasn't been refreshed.
    """
    import hashlib
    from types import SimpleNamespace

    from bty.catalog import image_ref_for_src, local_filename_for

    image_root = tmp_path / "imgs"
    image_root.mkdir()
    upstream_src = "https://example.invalid/u.img.gz"
    name = "u.img.gz"
    fmt = "img.gz"
    ref = image_ref_for_src(upstream_src)
    cache_filename = local_filename_for(ref, name, fmt)
    payload = b"persistent image bytes"
    (image_root / cache_filename).write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()
    (image_root / f"{cache_filename}.sha256").write_text(f"{expected_sha}  {cache_filename}\n")

    # Catalog row WITHOUT a sha (e.g. operator added the entry by URL
    # without a sha_url, or it's an oras entry whose sha hasn't been
    # back-filled on this release).
    manifest = SimpleNamespace(
        sha256=None,
        name=name,
        src=upstream_src,
        format=fmt,
        size_bytes=len(payload),
    )
    merged = images.merge_with_catalog(image_root, [manifest])
    assert len(merged) == 1
    u = merged[0]
    assert u.cached is True
    assert u.sha256 == expected_sha, (
        "merge must read the cached file's sidecar to populate "
        "UnifiedImage.sha256 when the catalog row's disk_image_sha "
        "is NULL"
    )


def test_merge_with_catalog_skips_catalog_cache_files_in_dir_scan(tmp_path: Path) -> None:
    """REGRESSION (v0.33.0 -> v0.33.1): a ``catalog-<ref:12>-<slug>.<ext>``
    file under the image_root is the cache form of an upstream catalog
    entry; the merge must NOT emit a separate UnifiedImage for it (with
    src=file://catalog-...) alongside the real catalog entry. Without
    this skip the same image surfaced twice on /ui/images -- once with
    the upstream name, once with the raw cache filename.

    Surfaced visually: two rows ("nosi fedora-sysdev (x86_64, rolling)"
    and "catalog-e3a4d87079ad-nosi-fedora-sysdev-x86_64-rolling.img.gz")
    listing the same underlying file.
    """
    from types import SimpleNamespace

    from bty.catalog import image_ref_for_src, local_filename_for

    image_root = tmp_path / "imgs"
    image_root.mkdir()
    upstream_src = "oras://ghcr.io/safl/nosi/fedora-sysdev:latest"
    name = "nosi fedora-sysdev (x86_64, rolling)"
    fmt = "img.gz"
    ref = image_ref_for_src(upstream_src)
    cache_filename = local_filename_for(ref, name, fmt)
    (image_root / cache_filename).write_bytes(b"cached blob")

    manifest = SimpleNamespace(
        sha256=None,
        name=name,
        src=upstream_src,
        format=fmt,
        size_bytes=11,
    )
    merged = images.merge_with_catalog(image_root, [manifest])
    # Exactly one row: the catalog entry, with cached=True (the
    # cache file is recognised) and the human upstream name.
    assert len(merged) == 1, (
        f"expected one merged row (catalog cache file folded into "
        f"its catalog entry), got {[u.names for u in merged]!r}"
    )
    assert merged[0].names == (name,)
    assert merged[0].cached is True
    # And critically: no source carrying the raw cache filename as
    # a file:// src -- that's the v0.33.0 duplicate-row shape.
    src_locations = {s.location for s in merged[0].sources}
    assert all("catalog-" not in loc.rsplit("/", 1)[-1] for loc in src_locations), (
        f"merged row must not list file://catalog-... as a source: {src_locations!r}"
    )


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
