"""Tests for bty.flash.

Validation logic (``make_plan`` / ``validate_plan``) is
exercised with hand-built ``ImageInfo`` / ``TargetInfo`` dataclasses -
no mocking. The probe functions, which actually shell out, get their
own targeted tests; subprocess calls are patched there because tests
can't (and shouldn't) actually run ``qemu-img`` / ``zstd`` / ``lsblk``.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from bty import flash

# ---------- Helpers ----------------------------------------------------------


def _img(
    fmt: str | None = "img",
    size: int = 1024,
    virtual: int | None = 1024,
    path: Path = Path("/fake/image.img"),
) -> flash.ImageInfo:
    return flash.ImageInfo(
        path=path,
        format=fmt,
        size_bytes=size,
        virtual_size_bytes=virtual,
    )


def _tgt(
    *,
    exists: bool = True,
    is_block: bool = True,
    size: int | None = 32 * 1024,
    mountpoints: list[str] | None = None,
    path: Path = Path("/dev/sdX"),
) -> flash.TargetInfo:
    return flash.TargetInfo(
        path=path,
        exists=exists,
        is_block_device=is_block,
        size_bytes=size,
        mountpoints=list(mountpoints or []),
    )


# ---------- make_plan / validate_plan: pure data, no mocking ----------------


def test_make_plan_records_no_notes_when_virtual_size_known() -> None:
    plan = flash.make_plan(_img(), _tgt())
    assert plan.notes == []


def test_make_plan_notes_unknown_virtual_size() -> None:
    plan = flash.make_plan(_img(virtual=None), _tgt())
    assert any("size-fits-target check skipped" in n for n in plan.notes)


def test_make_plan_skips_note_when_format_is_unrecognised() -> None:
    """Unrecognised format already yields a validation error;
    we don't double-report it as a 'virtual size unknown' note."""
    plan = flash.make_plan(_img(fmt=None, virtual=None), _tgt())
    assert all("size-fits-target check skipped" not in n for n in plan.notes)


def test_validate_ok_for_sane_plan() -> None:
    plan = flash.make_plan(_img(virtual=1024), _tgt(size=1024 * 1024))
    assert flash.validate_plan(plan) == []


def test_validate_unknown_format() -> None:
    plan = flash.make_plan(_img(fmt=None), _tgt())
    errors = flash.validate_plan(plan)
    assert any("image format not recognised" in e for e in errors)


def test_validate_tarball_gives_specific_extract_first_message() -> None:
    """Operators dropping a .tar.gz / .tgz / .tar.xz on BTY_IMAGES
    get a specific guidance message ("extract first") rather than
    the generic "format not recognised" -- tarballs are common
    enough in image-distribution channels that the specific hint
    saves the next confused operator a debugging round."""
    tarball = _img(fmt=None, path=Path("/fake/images/raspbian.tar.gz"))
    plan = flash.make_plan(tarball, _tgt())
    errors = flash.validate_plan(plan)
    assert any("tarball" in e and "Extract first" in e for e in errors)
    # And the generic "format not recognised" message must NOT
    # also fire for the same tarball -- one specific error is
    # better than two confusing ones.
    assert not any("format not recognised" in e for e in errors)


def test_validate_target_missing() -> None:
    plan = flash.make_plan(_img(), _tgt(exists=False, is_block=False, size=None))
    errors = flash.validate_plan(plan)
    assert any("does not exist" in e for e in errors)


def test_validate_target_not_block() -> None:
    plan = flash.make_plan(_img(), _tgt(is_block=False, size=None))
    errors = flash.validate_plan(plan)
    assert any("not a block device" in e for e in errors)


def test_validate_target_too_small() -> None:
    plan = flash.make_plan(_img(virtual=10_000), _tgt(size=1_000))
    errors = flash.validate_plan(plan)
    assert any("larger than target" in e for e in errors)


def test_validate_target_mounted() -> None:
    """A target with MOUNTED partitions is in use -> reject. (Partitions
    + data alone are fine; bty overwrites whole disks. Only a mount
    signals use -- and the live env must not auto-mount the target.)"""
    plan = flash.make_plan(_img(), _tgt(mountpoints=["/", "/boot"]))
    errors = flash.validate_plan(plan)
    assert any("mounted partitions" in e for e in errors)


def test_validate_target_with_partitions_but_unmounted_is_ok() -> None:
    """Partitions + data on the target are NOT an error -- only a mount
    is. An unmounted disk full of an old OS flashes fine."""
    plan = flash.make_plan(_img(), _tgt(mountpoints=[]))
    errors = flash.validate_plan(plan)
    assert all("mounted" not in e for e in errors)


def test_validate_skips_size_check_when_virtual_unknown() -> None:
    plan = flash.make_plan(_img(virtual=None), _tgt(size=1))
    errors = flash.validate_plan(plan)
    assert all("larger than target" not in e for e in errors)


def test_to_dict_round_trips_plain_types() -> None:
    plan = flash.make_plan(_img(), _tgt(mountpoints=["/"]))
    payload = plan.to_dict()
    assert payload["image"]["format"] == "img"
    assert payload["target"]["mountpoints"] == ["/"]
    assert "provisioning_mode" not in payload


# ---------- probe_image: subprocess shelling, mocked ------------------------


def test_probe_image_raw_img(tmp_path: Path) -> None:
    img = tmp_path / "raw.img"
    img.write_bytes(b"\0" * 1024)
    info = flash.probe_image(img)
    assert info.format == "img"
    assert info.size_bytes == 1024
    assert info.virtual_size_bytes == 1024


def test_probe_image_qcow2_uses_qemu_img(tmp_path: Path) -> None:
    img = tmp_path / "x.qcow2"
    img.write_bytes(b"\0" * 256)
    fake_proc = MagicMock(returncode=0, stdout='{"virtual-size": 4194304}')
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "qcow2"
    assert info.virtual_size_bytes == 4194304


def test_probe_image_zst_parses_zstd_output(tmp_path: Path) -> None:
    img = tmp_path / "x.img.zst"
    img.write_bytes(b"\0" * 128)
    zstd_output = (
        "Frames Skips Compressed Uncompressed Ratio Check Filename\n"
        "     1     0    100.00 KiB    1.00 MiB  10.00 XXH64 x.img.zst\n"
    )
    fake_proc = MagicMock(returncode=0, stdout=zstd_output)
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "img.zst"
    assert info.virtual_size_bytes == 1024 * 1024


def test_probe_image_xz_parses_xz_output(tmp_path: Path) -> None:
    """``xz -l`` output has a different header but the same
    ``<value> <unit>`` pair shape, so the shared listing parser
    extracts the uncompressed size correctly."""
    img = tmp_path / "x.img.xz"
    img.write_bytes(b"\0" * 128)
    xz_output = (
        "Strms  Blocks   Compressed Uncompressed  Ratio  Check   Filename\n"
        "    1       1     12.0 MiB    140.4 MiB  0.085  CRC64   x.img.xz\n"
    )
    fake_proc = MagicMock(returncode=0, stdout=xz_output)
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "img.xz"
    # 140.4 MiB = 140.4 * 1024^2 bytes
    assert info.virtual_size_bytes == int(140.4 * 1024 * 1024)


def test_probe_image_xz_xz_failure_returns_unknown(tmp_path: Path) -> None:
    """``xz -l`` returning non-zero leaves virtual_size_bytes None;
    validate_plan falls back to the size-fits-target skip note
    rather than blocking the flash."""
    img = tmp_path / "x.img.xz"
    img.write_bytes(b"\0")
    fake_proc = MagicMock(returncode=1, stdout="", stderr="xz: file is corrupt")
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "img.xz"
    assert info.virtual_size_bytes is None


def test_probe_image_gz_parses_gzip_listing(tmp_path: Path) -> None:
    """``gzip -l`` emits unit-less byte counts in two columns
    (compressed uncompressed). The shared listing parser doesn't
    apply here -- gzip uses a separate ``_parse_gzip_listing``
    helper that splits on whitespace and takes the second cell."""
    img = tmp_path / "x.img.gz"
    img.write_bytes(b"\0" * 64)
    # Realistic compression ratio (uncompressed > compressed). The
    # wrap-detection guard below refuses outputs where uncompressed
    # < compressed (see test_probe_image_gz_refuses_wrapped_size),
    # so the synthetic fixture also has to look like a real-world
    # listing.
    gzip_output = (
        "         compressed        uncompressed  ratio uncompressed_name\n"
        "                 73                 200  63.5% x.img\n"
    )
    fake_proc = MagicMock(returncode=0, stdout=gzip_output)
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "img.gz"
    assert info.virtual_size_bytes == 200


def test_probe_image_gz_refuses_wrapped_size(tmp_path: Path) -> None:
    """gzip stores uncompressed size mod 2^32 in the file trailer.
    When the reported uncompressed value is smaller than the
    compressed value, the wrap definitely happened and the number
    is a lie: trusting it would let ``validate_plan`` greenlight a
    flash of a multi-GiB image onto a target too small to hold it,
    and ``dd`` would run off the end of the disk mid-write. Refuse
    the lie (return None) so the size-fits-target check is skipped
    with a note instead of cheerfully passing on a fake number.
    """
    img = tmp_path / "big.img.gz"
    img.write_bytes(b"\0" * 64)
    # A 5 GiB raw image (compressed to ~3 GiB) reports uncompressed
    # = 5 GiB - 4 GiB = 1073741824 bytes in the trailer (wrapped).
    # 3 GiB compressed > 1 GiB "uncompressed" -> wrap detected.
    wrapped_output = (
        "         compressed        uncompressed  ratio uncompressed_name\n"
        "         3221225472          1073741824 -200.0% big.img\n"
    )
    fake_proc = MagicMock(returncode=0, stdout=wrapped_output)
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.format == "img.gz"
    # None, not the wrapped 1 GiB lie.
    assert info.virtual_size_bytes is None


def test_probe_image_bz2_returns_unknown_virtual_size(tmp_path: Path) -> None:
    """bzip2 has no listing tool that reports the uncompressed size,
    so ``virtual_size_bytes`` is always ``None`` and validate_plan
    skips the size-fits-target check with a note."""
    img = tmp_path / "x.img.bz2"
    img.write_bytes(b"\0" * 64)
    info = flash.probe_image(img)
    assert info.format == "img.bz2"
    assert info.virtual_size_bytes is None


def test_is_tarball_extension_detects_common_tarballs() -> None:
    """``.tar.gz`` / ``.tgz`` / ``.tar.xz`` etc. must NOT be flashed
    directly -- they're container formats, not single-stream
    compression. ``is_tarball_extension`` is the helper that lets
    callers warn operators (rather than silently ignoring) when
    they drop a tarball onto BTY_IMAGES."""
    from bty import images

    for name in (
        "img.tar.gz",
        "appliance.tar.xz",
        "raspbian.img.tgz",
        "thing.tar.bz2",
        "BUILD.tzst",
    ):
        assert images.is_tarball_extension(name), name
    for name in ("img.gz", "x.img.zst", "y.qcow2", "raw.img"):
        assert not images.is_tarball_extension(name), name


def test_probe_image_qcow2_qemu_img_failure_returns_unknown(tmp_path: Path) -> None:
    img = tmp_path / "x.qcow2"
    img.write_bytes(b"\0")
    fake_proc = MagicMock(returncode=1, stdout="", stderr="oh no")
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        info = flash.probe_image(img)
    assert info.virtual_size_bytes is None


def test_probe_image_unknown_format_no_virtual_size(tmp_path: Path) -> None:
    img = tmp_path / "weird.tar"
    img.write_bytes(b"\0")
    info = flash.probe_image(img)
    assert info.format is None
    assert info.virtual_size_bytes is None


def test_probe_image_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        flash.probe_image(tmp_path / "nope.img")


# ---------- probe_target: filesystem facts, no patching needed for negatives ---


def test_probe_target_missing_path(tmp_path: Path) -> None:
    info = flash.probe_target(tmp_path / "ghost")
    assert info.exists is False
    assert info.is_block_device is False
    assert info.size_bytes is None
    assert info.mountpoints == []


def test_probe_target_regular_file(tmp_path: Path) -> None:
    """A regular file is not a block device - covered without any patching."""
    target = tmp_path / "regular.txt"
    target.write_text("not a disk")
    info = flash.probe_target(target)
    assert info.exists is True
    assert info.is_block_device is False
    assert info.size_bytes is None
    assert info.mountpoints == []


# ---------- execute_plan: dispatch logic, helpers stubbed --------------------


def _stub_block_target(path: Path) -> flash.TargetInfo:
    return flash.TargetInfo(
        path=path,
        exists=True,
        is_block_device=True,
        size_bytes=1024 * 1024 * 1024,
        mountpoints=[],
    )


def _stub_post_write(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(flash, "_sync_target", lambda _t: calls.append("sync"))
    monkeypatch.setattr(flash, "_partprobe_target", lambda _t: calls.append("partprobe"))


def test_execute_plan_dispatches_to_img_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img"), _tgt())
    flash.execute_plan(plan)

    assert calls == ["img", "sync", "partprobe"]


def test_execute_plan_dispatches_to_zst_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img.zst"), _tgt())
    flash.execute_plan(plan)

    assert calls == ["zst", "sync", "partprobe"]


def test_execute_plan_dispatches_to_xz_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.img.xz`` source dispatches to ``_flash_xz`` (mirrors the
    .img.zst path; bty's flash code accepts both compression formats
    even though bty itself ships .img.zst for hot-path flash speed)."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_xz", lambda _i, _t, **_kw: calls.append("xz"))
    monkeypatch.setattr(flash, "_flash_gz", lambda _i, _t, **_kw: calls.append("gz"))
    monkeypatch.setattr(flash, "_flash_bz2", lambda _i, _t, **_kw: calls.append("bz2"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img.xz"), _tgt())
    flash.execute_plan(plan)

    assert calls == ["xz", "sync", "partprobe"]


def test_execute_plan_dispatches_to_gz_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.img.gz`` (universal legacy format -- older Ubuntu / RPi OS,
    appliance vendor bundles) dispatches to ``_flash_gz``."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_xz", lambda _i, _t, **_kw: calls.append("xz"))
    monkeypatch.setattr(flash, "_flash_gz", lambda _i, _t, **_kw: calls.append("gz"))
    monkeypatch.setattr(flash, "_flash_bz2", lambda _i, _t, **_kw: calls.append("bz2"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img.gz"), _tgt())
    flash.execute_plan(plan)

    assert calls == ["gz", "sync", "partprobe"]


def test_execute_plan_dispatches_to_bz2_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.img.bz2`` (legacy / archival format) dispatches to
    ``_flash_bz2``. Note bz2 has no metadata header for uncompressed
    size, so ``virtual_size_bytes`` is None and the validation
    size-fits-target check is skipped with a note."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_xz", lambda _i, _t, **_kw: calls.append("xz"))
    monkeypatch.setattr(flash, "_flash_gz", lambda _i, _t, **_kw: calls.append("gz"))
    monkeypatch.setattr(flash, "_flash_bz2", lambda _i, _t, **_kw: calls.append("bz2"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img.bz2", virtual=None), _tgt())
    flash.execute_plan(plan)

    assert calls == ["bz2", "sync", "partprobe"]


def test_execute_plan_dispatches_to_qcow2_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_xz", lambda _i, _t, **_kw: calls.append("xz"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="qcow2"), _tgt())
    flash.execute_plan(plan)

    assert calls == ["qcow2", "sync", "partprobe"]


def test_execute_plan_emits_lifecycle_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress callback receives started -> writing -> synced -> partprobed."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: None)
    _stub_post_write(monkeypatch, calls)  # we don't care about call order here

    events: list[flash.FlashProgress] = []
    plan = flash.make_plan(_img(fmt="img", virtual=12345), _tgt())
    flash.execute_plan(plan, progress=events.append)

    names = [e.event for e in events]
    assert names == ["started", "writing", "synced", "partprobed"]
    assert events[0].total_bytes == 12345
    assert events[1].note == "img"


def test_execute_plan_emits_failed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any FlashError gets a 'failed' progress event before the re-raise."""
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)

    def boom(_i: Path, _t: Path, **_kw: Any) -> None:
        raise flash.FlashError("simulated dd failure")

    monkeypatch.setattr(flash, "_flash_img", boom)

    events: list[flash.FlashProgress] = []
    plan = flash.make_plan(_img(fmt="img"), _tgt())
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan, progress=events.append)

    assert events[-1].event == "failed"
    assert "simulated dd failure" in events[-1].note


def test_execute_plan_refuses_unknown_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    plan = flash.make_plan(_img(fmt=None), _tgt())
    with pytest.raises(flash.FlashError, match="cannot flash image of format"):
        flash.execute_plan(plan)


# ---------- URL-sourced images ----------------------------------------------


def _img_url(
    *,
    fmt: str | None = "img.zst",
    url: str = "http://server.local:8080/images/test.img.zst",
    size: int = 1024,
    virtual: int | None = None,
) -> flash.ImageInfo:
    return flash.ImageInfo(
        path=None,
        url=url,
        format=fmt,
        size_bytes=size,
        virtual_size_bytes=virtual,
    )


def test_image_info_display_uses_url_when_set() -> None:
    info = _img_url(url="http://server/foo.img.zst")
    assert info.display == "http://server/foo.img.zst"


def test_image_info_display_uses_path_when_url_unset() -> None:
    info = _img(path=Path("/var/lib/bty/images/foo.img"))
    assert info.display == "/var/lib/bty/images/foo.img"


def test_to_dict_includes_url_for_url_sourced_image() -> None:
    plan = flash.make_plan(_img_url(fmt="img.zst", virtual=2048), _tgt())
    d = plan.to_dict()
    assert d["image"]["url"] == "http://server.local:8080/images/test.img.zst"
    assert d["image"]["path"] is None


def test_probe_image_url_parses_format_from_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Format detection works off the URL path's filename extension."""

    class _FakeResp:
        headers: ClassVar[dict[str, str]] = {"Content-Length": "12345"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kw: _FakeResp(),
    )
    info = flash.probe_image_url("http://server/foo.img.zst")
    assert info.url == "http://server/foo.img.zst"
    assert info.path is None
    assert info.format == "img.zst"
    assert info.size_bytes == 12345
    # .img.zst can't determine virtual size from HEAD alone.
    assert info.virtual_size_bytes is None


def test_probe_image_url_falls_back_to_format_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when the URL path filename has no recognised
    extension (bty-web's ``/images/<sha>/<display-name>`` route
    emits URLs whose trailing segment is human text like
    ``nosi%20fedora-sysdev%20%28x86_64%2C%20rolling%29`` with no
    extension), URL-based format detection returns None and
    ``validate_plan`` rejects with "image format not recognised".

    The caller (``bty``, which has the catalog entry's ``format``
    field) passes that as ``format_hint``; the probe uses it as a
    fallback when extension detection fails.
    """

    class _FakeResp:
        headers: ClassVar[dict[str, str]] = {"Content-Length": "9000"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kw: _FakeResp(),
    )
    url = "http://server/images/abc123/nosi%20fedora-sysdev%20%28x86_64%2C%20rolling%29"
    info = flash.probe_image_url(url, format_hint="img.gz")
    assert info.format == "img.gz"
    assert info.size_bytes == 9000

    # No hint -> format stays None (caller-side responsibility
    # to pass the hint when they have it).
    info_no_hint = flash.probe_image_url(url)
    assert info_no_hint.format is None

    # URL-derived format still takes precedence over the hint
    # (no need to second-guess a URL that does carry an
    # extension).
    info_url_wins = flash.probe_image_url("http://server/foo.qcow2", format_hint="img.gz")
    assert info_url_wins.format == "qcow2"


def test_probe_image_url_raw_img_uses_content_length_as_virtual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For raw .img URLs the source size IS the virtual size."""

    class _FakeResp:
        headers: ClassVar[dict[str, str]] = {"Content-Length": "98765"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _FakeResp())
    info = flash.probe_image_url("http://server/foo.img")
    assert info.format == "img"
    assert info.size_bytes == 98765
    assert info.virtual_size_bytes == 98765


def test_probe_image_url_tolerates_malformed_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus Content-Length must fold into "unknown size" (0), not
    crash the probe with an uncaught ValueError -- matches the guards
    in catalog / releases."""

    class _FakeResp:
        headers: ClassVar[dict[str, str]] = {"Content-Length": "not-a-number"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _FakeResp())
    info = flash.probe_image_url("http://server/foo.img")
    assert info.size_bytes == 0
    assert info.virtual_size_bytes is None


def test_sync_and_partprobe_swallow_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-flash housekeeping is bounded + best-effort: a ``sync`` /
    ``partprobe`` / ``udevadm`` that times out (stuck disk) must not
    raise -- the bytes are already written."""

    def _timeout(cmd: list[str], **_kw: object) -> None:
        raise flash.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(flash.subprocess, "run", _timeout)
    # Neither should propagate the TimeoutExpired.
    flash._sync_target(Path("/dev/sdz"))
    flash._partprobe_target(Path("/dev/sdz"))


def test_probe_image_url_unreachable_raises_filenotfound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(FileNotFoundError, match="not reachable"):
        flash.probe_image_url("http://nowhere.example/foo.img")


def test_probe_image_url_rejects_non_http_scheme() -> None:
    """``oras://`` is now accepted alongside http(s) (resolves through
    the ORAS adapter); everything else still rejects with a clear
    message naming the supported schemes."""
    with pytest.raises(ValueError, match=r"http://, https://, or oras://"):
        flash.probe_image_url("ftp://server/foo.img")


def test_execute_plan_dispatches_to_url_writers_for_url_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``image.url`` is set, execute_plan calls the streaming
    helpers (``_flash_*_from_url``) instead of the local-path ones."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t, **_kw: calls.append("img-local"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t, **_kw: calls.append("zst-local"))
    monkeypatch.setattr(flash, "_flash_xz", lambda _i, _t, **_kw: calls.append("xz-local"))
    monkeypatch.setattr(flash, "_flash_gz", lambda _i, _t, **_kw: calls.append("gz-local"))
    monkeypatch.setattr(flash, "_flash_bz2", lambda _i, _t, **_kw: calls.append("bz2-local"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2-local"))
    monkeypatch.setattr(
        flash,
        "_flash_img_from_url",
        lambda _u, _t, **_kw: calls.append("img-url"),
    )
    monkeypatch.setattr(
        flash,
        "_flash_zst_from_url",
        lambda _u, _t, **_kw: calls.append("zst-url"),
    )
    monkeypatch.setattr(
        flash,
        "_flash_xz_from_url",
        lambda _u, _t, **_kw: calls.append("xz-url"),
    )
    monkeypatch.setattr(
        flash,
        "_flash_gz_from_url",
        lambda _u, _t, **_kw: calls.append("gz-url"),
    )
    monkeypatch.setattr(
        flash,
        "_flash_bz2_from_url",
        lambda _u, _t, **_kw: calls.append("bz2-url"),
    )
    monkeypatch.setattr(
        flash,
        "_flash_qcow2_from_url",
        lambda _u, _t, **_kw: calls.append("qcow2-url"),
    )
    _stub_post_write(monkeypatch, calls)

    for fmt, expected in (
        ("img", "img-url"),
        ("img.zst", "zst-url"),
        ("img.xz", "xz-url"),
        ("img.gz", "gz-url"),
        ("img.bz2", "bz2-url"),
        ("qcow2", "qcow2-url"),
    ):
        calls.clear()
        plan = flash.make_plan(_img_url(fmt=fmt), _tgt())
        flash.execute_plan(plan)
        assert expected in calls
        for local_marker in (
            "img-local",
            "zst-local",
            "xz-local",
            "gz-local",
            "bz2-local",
            "qcow2-local",
        ):
            assert local_marker not in calls


def test_execute_plan_forwards_expected_sha_to_url_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan's ``image.expected_sha`` must reach the streaming writer
    as ``expected_sha`` -- otherwise declared-sha verification is wired
    up to a value the writer never sees."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(
        flash,
        "_flash_img_from_url",
        lambda _u, _t, **kw: seen.update(kw),
    )
    _stub_post_write(monkeypatch, [])

    digest = "sha256:" + "ab" * 32
    img = flash.ImageInfo(
        path=None,
        url="http://server.local/x.img",
        format="img",
        size_bytes=1024,
        virtual_size_bytes=1024,
        expected_sha=digest,
    )
    flash.execute_plan(flash.make_plan(img, _tgt()))
    assert seen.get("expected_sha") == digest


def test_execute_plan_refuses_when_target_no_longer_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race protection: target was a block device at plan time but isn't now."""

    def now_a_regular_file(path: Path) -> flash.TargetInfo:
        return flash.TargetInfo(
            path=path, exists=True, is_block_device=False, size_bytes=None, mountpoints=[]
        )

    monkeypatch.setattr(flash, "probe_target", now_a_regular_file)
    plan = flash.make_plan(_img(), _tgt())
    with pytest.raises(flash.FlashError, match="no longer a block device"):
        flash.execute_plan(plan)


def test_execute_plan_refuses_when_target_now_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target mounted at execute time is in use -> refuse (no
    auto-unmount). bty doesn't dd over a live filesystem even though it
    usually could unmount; the live env must not auto-mount the target
    (systemd.gpt_auto=0), so a mount here is a real one."""

    def now_mounted(path: Path) -> flash.TargetInfo:
        return flash.TargetInfo(
            path=path,
            exists=True,
            is_block_device=True,
            size_bytes=1024,
            mountpoints=["/mnt/oops"],
        )

    monkeypatch.setattr(flash, "probe_target", now_mounted)
    plan = flash.make_plan(_img(), _tgt())
    with pytest.raises(flash.FlashError, match="mounted partitions"):
        flash.execute_plan(plan)


# ----- _pump_dd_progress: parse dd's status=progress stderr stream ---------


def test_pump_dd_progress_emits_most_recent_progress_per_chunk() -> None:
    """Feed a synthetic dd-stderr stream into the pump. dd progress is
    monotonically increasing; if multiple progress lines arrive in a
    single read (terminal-style burst flush), the pump emits only the
    MOST RECENT to keep the consumer's render loop quiet. ``total_bytes``
    is carried through so the consumer can compute percent / ETA without
    holding state."""
    stream = io.StringIO(
        "1048576 bytes (1.0 MB, 1.0 MiB) copied, 0.5 s, 2.0 MB/s\r"
        "2097152 bytes (2.1 MB, 2.0 MiB) copied, 1.0 s, 2.1 MB/s\r"
    )
    events: list[flash.FlashProgress] = []
    flash._pump_dd_progress(stream, events.append, total_bytes=4194304)

    # Single emit with the latest byte count.
    assert [e.event for e in events] == ["writing_progress"]
    assert events[0].bytes_written == 2097152
    assert events[0].total_bytes == 4194304


def test_pump_dd_progress_skips_non_progress_dd_lines() -> None:
    """dd also emits ``records in/out`` summary lines and warnings;
    only lines matching the ``<int> bytes`` prefix should fire
    progress events."""
    stream = io.StringIO(
        "1+0 records in\n1+0 records out\n524288 bytes (524 kB, 512 KiB) copied, 0.1 s, 5.2 MB/s\r"
    )
    events: list[flash.FlashProgress] = []
    flash._pump_dd_progress(stream, events.append, total_bytes=None)

    assert len(events) == 1
    assert events[0].event == "writing_progress"
    assert events[0].bytes_written == 524288
    assert events[0].total_bytes is None  # caller didn't know


def test_pump_dd_progress_handles_empty_stream() -> None:
    """An empty stream (dd never emitted progress, e.g. immediate
    failure) should return without raising and without emitting."""
    events: list[flash.FlashProgress] = []
    flash._pump_dd_progress(io.StringIO(""), events.append, total_bytes=100)
    assert events == []


# --------------------------------------------------------------------------
# Cancel plumbing (FlashCancelled + watchdog)
# --------------------------------------------------------------------------


def test_cancel_watchdog_terminates_live_procs_on_True() -> None:
    """When ``cancel()`` returns True, every still-live subprocess
    gets terminated. Procs that exited naturally are left alone."""
    import subprocess as _sp

    # Long-lived: ``sleep 30`` -- the watchdog needs to kill it.
    alive = _sp.Popen(["sleep", "30"])
    # Already-finished: a no-op ``true`` that exits immediately. The
    # watchdog should NOT try to signal it (its handle is closed but
    # poll() returns 0, so the dead-procs branch wins).
    finished = _sp.Popen(["true"])
    finished.wait(timeout=2)
    cancelled = [False]

    def _cancel() -> bool:
        return cancelled[0]

    watchdog = flash._spawn_cancel_watchdog([alive, finished], _cancel)
    assert watchdog is not None
    # Trigger cancel; the watchdog (running at ~4Hz) should see it
    # within a tick and SIGTERM the live proc.
    cancelled[0] = True
    watchdog.join(timeout=5.0)
    assert not watchdog.is_alive(), "watchdog did not exit after cancel"
    # The alive proc was terminated.
    rc = alive.wait(timeout=2.0)
    assert rc != 0, f"expected terminated rc != 0, got {rc}"


def test_cancel_watchdog_exits_when_all_procs_finish_naturally() -> None:
    """No cancel ever fires -- the watchdog exits cleanly once every
    proc has finished. Otherwise it'd leak a daemon thread per flash."""
    import subprocess as _sp

    proc = _sp.Popen(["true"])
    proc.wait(timeout=2)
    watchdog = flash._spawn_cancel_watchdog([proc], lambda: False)
    assert watchdog is not None
    watchdog.join(timeout=5.0)
    assert not watchdog.is_alive(), "watchdog did not exit on natural completion"


def test_cancel_watchdog_returns_none_without_cancel_callback() -> None:
    """``cancel=None`` -> no thread spawned. Important: lets the
    overwhelming majority of flashes (no cancel handler provided)
    avoid the watchdog overhead entirely."""
    import subprocess as _sp

    proc = _sp.Popen(["true"])
    proc.wait(timeout=2)
    assert flash._spawn_cancel_watchdog([proc], None) is None


def test_flash_cancelled_subclasses_flash_error() -> None:
    """``except FlashError`` callers must still handle cancellation as
    a failure case. Subclassing preserves that contract; callers that
    want to distinguish catch FlashCancelled first."""
    assert issubclass(flash.FlashCancelled, flash.FlashError)


# ----- UEFI boot-entry registration (efibootmgr) -------------------------


def test_boot_entries_with_label_matches_label_only() -> None:
    out = (
        "BootOrder: 0000,0001\n"
        "Boot0000* bty flashed\tHD(1,GPT)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n"
        "Boot0001* UEFI: PXE IPv4\tMAC(aabbcc)\n"
        "Boot0002* bty flashed\tHD(1,GPT)\n"
    )
    assert flash._boot_entries_with_label(out, "bty flashed") == ["0000", "0002"]
    assert flash._boot_entries_with_label(out, "ubuntu") == []


def test_find_esp_partition_number_from_lsblk_partn() -> None:
    lsblk = (
        '{"blockdevices":[{"path":"/dev/sda","children":['
        f'{{"path":"/dev/sda1","parttype":"{flash._ESP_TYPE_GUID}","partn":1}},'
        '{"path":"/dev/sda2","parttype":"0fc63daf-8483-4772-8e79-3d69d8477de4","partn":2}'
        "]}]}"
    )
    with patch("bty.flash.subprocess.run", return_value=MagicMock(stdout=lsblk)):
        assert flash._find_esp_partition_number(Path("/dev/sda")) == 1


def test_find_esp_partition_number_falls_back_to_path_digits() -> None:
    # No PARTN column (older lsblk): parse the trailing digits of the path.
    lsblk = (
        '{"blockdevices":[{"path":"/dev/nvme0n1","children":['
        f'{{"path":"/dev/nvme0n1p1","parttype":"{flash._ESP_TYPE_GUID}"}}'
        "]}]}"
    )
    with patch("bty.flash.subprocess.run", return_value=MagicMock(stdout=lsblk)):
        assert flash._find_esp_partition_number(Path("/dev/nvme0n1")) == 1


def test_find_esp_partition_number_none_when_no_esp() -> None:
    # The case the operator hit: a single ext4 root, no ESP at all.
    lsblk = (
        '{"blockdevices":[{"path":"/dev/sda","children":['
        '{"path":"/dev/sda1","parttype":"0fc63daf-8483-4772-8e79-3d69d8477de4","partn":1}'
        "]}]}"
    )
    with patch("bty.flash.subprocess.run", return_value=MagicMock(stdout=lsblk)):
        assert flash._find_esp_partition_number(Path("/dev/sda")) is None


def test_register_uefi_boot_entry_skips_on_bios(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bty.flash.Path.is_dir", lambda self: False)
    msg = flash.register_uefi_boot_entry(Path("/dev/sda"))
    assert "not booted in UEFI" in msg


def test_register_uefi_boot_entry_skips_without_efibootmgr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bty.flash.Path.is_dir", lambda self: True)
    monkeypatch.setattr("bty.flash.shutil.which", lambda _x: None)
    msg = flash.register_uefi_boot_entry(Path("/dev/sda"))
    assert "efibootmgr not installed" in msg


def test_register_uefi_boot_entry_skips_when_no_esp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bty.flash.Path.is_dir", lambda self: True)
    monkeypatch.setattr("bty.flash.shutil.which", lambda _x: "/usr/sbin/efibootmgr")
    monkeypatch.setattr("bty.flash._find_esp_partition_number", lambda _d: None)
    msg = flash.register_uefi_boot_entry(Path("/dev/sda"))
    assert "no EFI System Partition" in msg


def test_register_uefi_boot_entry_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bty.flash.Path.is_dir", lambda self: True)
    monkeypatch.setattr("bty.flash.shutil.which", lambda _x: "/usr/sbin/efibootmgr")
    monkeypatch.setattr("bty.flash._find_esp_partition_number", lambda _d: 1)

    calls: list[list[str]] = []
    outputs = iter(
        [
            "BootOrder: 0001,0002\nBoot0001* UEFI PXE\nBoot0002* ubuntu\n",  # delete-scan (no bty)
            "",  # --create-only
            (  # label-scan -> finds the just-created entry
                "BootOrder: 0001,0002\nBoot0001* UEFI PXE\nBoot0002* ubuntu\n"
                "Boot0009* bty flashed\tHD\n"
            ),
            "",  # -n
        ]
    )

    def fake_run(args: list[str], **_kw: object) -> MagicMock:
        calls.append(args)
        return MagicMock(stdout=next(outputs))

    monkeypatch.setattr("bty.flash.subprocess.run", fake_run)
    msg = flash.register_uefi_boot_entry(Path("/dev/sda"))
    assert "Boot0009" in msg and "/dev/sda" in msg
    # Non-destructive: create-only (entry NOT added to BootOrder) + a
    # one-shot BootNext. BootOrder must NEVER be rewritten -- doing so
    # stranded an EPYC box out of its PXE entry.
    assert any("--create-only" in c for c in calls)
    assert ["efibootmgr", "-n", "0009"] in calls
    assert not any(c[:2] == ["efibootmgr", "-o"] for c in calls)


# ---------- Integrity: digest threading + verification -----------------------
#
# PR1 of issue #10: ``oras://`` references commit to a content digest, and
# the flash pipeline verifies it on the wire via a ``tee | sha256sum``
# splice (the bytes stay in the subprocess plane; Python only reads the
# final ~65-byte digest line). These cover the pure logic; the end-to-end
# pipeline behaviour lives in tests/test_flash_integration.py.


def test_curl_args_for_source_plain_url_carries_no_digest() -> None:
    argv, size, digest = flash._curl_args_for_source("https://example.test/x.img")
    assert argv == ["curl", "-fsSL", "https://example.test/x.img"]
    assert size is None
    # No digest for a plain URL -> the caller keeps its zero-copy path.
    assert digest is None


def test_curl_args_for_source_oras_threads_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = flash.oras.ResolvedBlob(
        blob_url="https://reg.test/v2/r/blobs/sha256:abc",
        headers={"Authorization": "Bearer t"},
        digest="sha256:" + "ab" * 32,
        size=4096,
        title="x.img.gz",
    )
    monkeypatch.setattr(flash.oras, "is_oras_url", lambda _u: True)
    monkeypatch.setattr(flash.oras, "resolve_ref", lambda _u: resolved)

    argv, size, digest = flash._curl_args_for_source("oras://reg.test/r:tag")
    assert argv[-1] == resolved.blob_url
    assert "-H" in argv and "Authorization: Bearer t" in argv
    assert size == 4096
    # The layer's frozen digest is threaded out for the streaming check.
    assert digest == resolved.digest


def test_verify_digest_match_is_silent() -> None:
    d = "sha256:" + "cd" * 32
    flash._verify_digest(d, d, "oras://x")  # must not raise


def test_verify_digest_mismatch_raises_integrity_error() -> None:
    with pytest.raises(flash.FlashIntegrityError) as ei:
        flash._verify_digest("sha256:" + "00" * 32, "sha256:" + "11" * 32, "oras://x")
    assert "integrity check failed" in str(ei.value)
    # Integrity failures are flash failures for plain ``except FlashError``.
    assert isinstance(ei.value, flash.FlashError)


def test_verify_digest_none_observed_is_silent() -> None:
    # When the tee wasn't spliced there is no observed digest; the caller
    # only verifies when it actually ran the hash, so this is a no-op.
    flash._verify_digest("sha256:" + "00" * 32, None, "oras://x")


def test_spawn_hash_tee_forwards_bytes_and_hashes(tmp_path: Path) -> None:
    import hashlib
    import shutil as _shutil

    if _shutil.which("tee") is None or _shutil.which("sha256sum") is None:
        pytest.skip("tee / sha256sum not available")

    # >64 KiB so the bytes cross a pipe buffer (proves both consumers
    # drain concurrently; a single-buffer test could hide a deadlock).
    payload = b"hash-tee-roundtrip\x00\x01\x02" * 4096
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)

    with src.open("rb") as fh:
        tee_proc, sha_proc = flash._spawn_hash_tee(fh)
        assert tee_proc.stdout is not None
        forwarded = tee_proc.stdout.read()
        tee_proc.stdout.close()
        observed = flash._read_observed_digest(sha_proc)
        assert tee_proc.wait() == 0
        assert sha_proc.wait() == 0

    # tee forwards every byte unchanged to the next stage...
    assert forwarded == payload
    # ...and sha256sum hashes the same bytes to the expected digest.
    assert observed == "sha256:" + hashlib.sha256(payload).hexdigest()


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    import hashlib
    import shutil as _shutil

    if _shutil.which("sha256sum") is None:
        pytest.skip("sha256sum not available")

    payload = b"qcow2-temp-file-hash" * 512
    f = tmp_path / "blob.qcow2"
    f.write_bytes(payload)
    assert flash._sha256_file(f) == "sha256:" + hashlib.sha256(payload).hexdigest()


# ---------- Integrity: declared-sha threading (issue #10 PR2) -----------------


def test_normalize_digest_variants() -> None:
    h = "ab" * 32
    assert flash._normalize_digest(None) is None
    assert flash._normalize_digest(h) == f"sha256:{h}"
    assert flash._normalize_digest(f"sha256:{h}") == f"sha256:{h}"
    # Catalog shas are lower-cased hex, but normalise defensively.
    assert flash._normalize_digest(h.upper()) == f"sha256:{h}"


class _FakeHeadResp:
    """Minimal stand-in for ``urlopen(...)`` used as a context manager."""

    headers: ClassVar[dict[str, str]] = {"Content-Length": "1024"}

    def __enter__(self) -> _FakeHeadResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def test_probe_image_url_stores_normalized_expected_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=30: _FakeHeadResp())
    h = "cd" * 32
    info = flash.probe_image_url("https://example.test/x.img", expected_sha=h)
    assert info.url == "https://example.test/x.img"
    assert info.expected_sha == f"sha256:{h}"


def test_probe_image_url_without_sha_leaves_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=30: _FakeHeadResp())
    info = flash.probe_image_url("https://example.test/x.img")
    assert info.expected_sha is None


def test_probe_image_url_oras_ignores_expected_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = flash.ImageInfo(
        path=None,
        url="oras://reg.test/r:tag",
        format="img.gz",
        size_bytes=0,
        virtual_size_bytes=None,
    )
    monkeypatch.setattr(flash, "_probe_image_url_oras", lambda _u: sentinel)
    out = flash.probe_image_url("oras://reg.test/r:tag", expected_sha="ab" * 32)
    # oras resolves its own digest; the http declared-sha is not applied.
    assert out is sentinel
    assert out.expected_sha is None


def test_redact_secrets_scrubs_bearer_tokens() -> None:
    # An oras flash injects a bearer; curl could echo the header. The
    # log pump must not leak the token to the progress UI / logs.
    assert (
        flash._redact_secrets("Authorization: Bearer abc.DEF-123_~+/=")
        == "Authorization: Bearer <redacted>"
    )
    assert (
        flash._redact_secrets("note: using bearer eyJ.foo_bar") == "note: using bearer <redacted>"
    )
    # Non-secret lines pass through untouched.
    assert flash._redact_secrets("curl: (22) HTTP 404 on blob") == "curl: (22) HTTP 404 on blob"


def test_probe_image_url_malformed_content_length_is_unknown_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadCLResp:
        headers: ClassVar[dict[str, str]] = {"Content-Length": "not-a-number"}

        def __enter__(self) -> _BadCLResp:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=30: _BadCLResp())
    info = flash.probe_image_url("https://example.test/x.img")
    # Malformed Content-Length folds to "unknown size" (0), no crash.
    assert info.size_bytes == 0
    assert info.virtual_size_bytes is None


def test_to_dict_includes_expected_sha() -> None:
    digest = "sha256:" + "ab" * 32
    img = flash.ImageInfo(
        path=None,
        url="https://example.test/i.img",
        format="img",
        size_bytes=1,
        virtual_size_bytes=1,
        expected_sha=digest,
    )
    plan = flash.make_plan(img, _tgt())
    assert plan.to_dict()["image"]["expected_sha"] == digest
