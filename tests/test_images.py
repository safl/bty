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


def test_detect_arch_from_name_canonicalises_common_tokens() -> None:
    """The heuristic maps the common token spellings to a single
    canonical name per arch, matching ``uname -m`` on Linux. The
    canonical names are what surfaces in the TUI / bty-web Arch
    column."""
    # x86_64 family
    assert images.detect_arch_from_name("debian-13-amd64.qcow2.zst") == "x86_64"
    assert images.detect_arch_from_name("bty-usbboot-pc-x86_64-v0.53.0.iso") == "x86_64"
    assert images.detect_arch_from_name("foo-x86-64-bar.img") == "x86_64"
    # arm64 family
    assert images.detect_arch_from_name("rpios-arm64-headless.img.xz") == "arm64"
    assert images.detect_arch_from_name("ubuntu-aarch64-server.img.gz") == "arm64"
    # 32-bit arm
    assert images.detect_arch_from_name("debian-armhf-rpi3.img.gz") == "arm"
    assert images.detect_arch_from_name("foo-armv7l.img") == "arm"
    # Misc Linux arches
    assert images.detect_arch_from_name("debian-riscv64-sid.qcow2") == "riscv64"
    assert images.detect_arch_from_name("debian-ppc64le.qcow2") == "ppc64le"
    assert images.detect_arch_from_name("debian-s390x.qcow2") == "s390x"
    assert images.detect_arch_from_name("debian-i386-legacy.img") == "i386"
    assert images.detect_arch_from_name("debian-i686-old.img") == "i386"


def test_detect_arch_from_name_returns_none_for_unrecognised() -> None:
    """No arch token present -> None. Callers display this as ``?``
    or ``-`` so an operator can see at a glance the metadata is
    absent (rather than missing or wrong)."""
    assert images.detect_arch_from_name("rolling-base.img.zst") is None
    assert images.detect_arch_from_name("appliance.qcow2") is None
    assert images.detect_arch_from_name("") is None


def test_detect_arch_from_name_is_case_insensitive() -> None:
    """Operators sometimes use mixed case in filenames; the heuristic
    shouldn't miss those."""
    assert images.detect_arch_from_name("Debian-AMD64.qcow2") == "x86_64"
    assert images.detect_arch_from_name("ARM64-image.img.gz") == "arm64"


def test_list_images_populates_arch_field(tmp_path: Path) -> None:
    """A real list_images scan threads arch through onto the Image
    record. Catches a regression where the field was added to the
    dataclass but no caller populated it."""
    _touch(tmp_path / "alpha-amd64.img.gz", size=64)
    _touch(tmp_path / "beta-arm64.img.zst", size=64)
    _touch(tmp_path / "no-arch-here.qcow2", size=64)
    imgs = {img.name: img for img in images.list_images(tmp_path)}
    assert imgs["alpha-amd64.img.gz"].arch == "x86_64"
    assert imgs["beta-arm64.img.zst"].arch == "arm64"
    assert imgs["no-arch-here.qcow2"].arch is None


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
