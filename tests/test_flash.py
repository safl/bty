"""Tests for bty.flash. Block-device probing + image-tooling are mocked."""

from __future__ import annotations

import io
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bty import flash


@pytest.fixture
def fake_image(tmp_path: Path) -> Path:
    img = tmp_path / "raw.img"
    img.write_bytes(b"\0" * 1024)
    return img


@pytest.fixture
def fake_qcow2(tmp_path: Path) -> Path:
    img = tmp_path / "image.qcow2"
    img.write_bytes(b"\0" * 256)
    return img


@pytest.fixture
def fake_zst(tmp_path: Path) -> Path:
    img = tmp_path / "image.img.zst"
    img.write_bytes(b"\0" * 128)
    return img


def _block_target(tmp_path: Path) -> Path:
    """Return a path that ``_probe_target`` will treat as a block device."""
    target = tmp_path / "block-fake"
    target.write_bytes(b"")
    return target


def _patch_block_probe(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    is_block: bool = True,
    size_bytes: int | None = 32 * 1024,
    mountpoints: list[str] | None = None,
) -> None:
    """Force ``_probe_target`` to return a deterministic shape for ``target``."""
    monkeypatch.setattr(
        flash,
        "_probe_target",
        lambda p: (
            (is_block, size_bytes, list(mountpoints or [])) if p == target else (False, None, [])
        ),
    )


def test_plan_flash_raw_img(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, size_bytes=10 * 1024 * 1024)
    plan = flash.plan_flash(fake_image, target, "none")
    assert plan.image_format == "img"
    assert plan.image_size_bytes == 1024
    assert plan.image_virtual_size_bytes == 1024
    assert plan.target_is_block_device
    assert plan.target_size_bytes == 10 * 1024 * 1024
    assert plan.provisioning_mode == "none"


def test_plan_flash_qcow2_uses_qemu_img(
    fake_qcow2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target)
    fake_proc = MagicMock(returncode=0, stdout='{"virtual-size": 4194304, "format": "qcow2"}')
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        plan = flash.plan_flash(fake_qcow2, target, "none")
    assert plan.image_format == "qcow2"
    assert plan.image_virtual_size_bytes == 4194304


def test_plan_flash_img_zst_parses_zstd_listing(
    fake_zst: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target)
    zstd_output = (
        "Frames Skips Compressed Uncompressed Ratio Check Filename\n"
        "     1     0    100.00 KiB    1.00 MiB  10.00 XXH64 image.img.zst\n"
    )
    fake_proc = MagicMock(returncode=0, stdout=zstd_output)
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        plan = flash.plan_flash(fake_zst, target, "none")
    assert plan.image_format == "img.zst"
    assert plan.image_virtual_size_bytes == 1024 * 1024


def test_plan_flash_missing_image_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        flash.plan_flash(tmp_path / "nope.img", tmp_path / "anything", "none")


def test_validate_ok_for_sane_plan(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, size_bytes=1024 * 1024)
    plan = flash.plan_flash(fake_image, target, "none")
    assert flash.validate_plan(plan) == []


def test_validate_target_not_block(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "not-a-disk.txt"
    target.write_text("hi")
    _patch_block_probe(monkeypatch, target, is_block=False, size_bytes=None)
    plan = flash.plan_flash(fake_image, target, "none")
    errors = flash.validate_plan(plan)
    assert any("not a block device" in e for e in errors)


def test_validate_target_missing(
    fake_image: Path,
    tmp_path: Path,
) -> None:
    plan = flash.plan_flash(fake_image, tmp_path / "missing-block", "none")
    errors = flash.validate_plan(plan)
    assert any("does not exist" in e for e in errors)


def test_validate_target_too_small(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, size_bytes=10)  # < image's 1024 bytes
    plan = flash.plan_flash(fake_image, target, "none")
    errors = flash.validate_plan(plan)
    assert any("larger than target" in e for e in errors)


def test_validate_target_mounted(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, mountpoints=["/", "/boot"])
    plan = flash.plan_flash(fake_image, target, "none")
    errors = flash.validate_plan(plan)
    assert any("mounted partitions" in e for e in errors)


def test_validate_unknown_provisioning_mode(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target)
    plan = flash.plan_flash(fake_image, target, "garbage")
    errors = flash.validate_plan(plan)
    assert any("unknown provisioning mode" in e for e in errors)


def test_validate_unknown_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = tmp_path / "weird.tar"
    img.write_bytes(b"\0")
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target)
    plan = flash.plan_flash(img, target, "none")
    errors = flash.validate_plan(plan)
    assert any("image format not recognised" in e for e in errors)


def test_validate_skips_size_check_when_virtual_unknown(
    fake_qcow2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qemu-img info failing -> virtual_size None -> note, no size error."""
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, size_bytes=10)
    fake_proc = MagicMock(returncode=1, stdout="", stderr="oh no")
    with patch("bty.flash.subprocess.run", return_value=fake_proc):
        plan = flash.plan_flash(fake_qcow2, target, "none")
    errors = flash.validate_plan(plan)
    assert all("larger than target" not in e for e in errors)
    assert any("virtual size could not be determined" in n for n in plan.notes)


def test_print_plan_renders_validation_status(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target)
    plan = flash.plan_flash(fake_image, target, "none")
    out = io.StringIO()
    flash.print_plan(plan, errors=[], file=out)
    text = out.getvalue()
    assert "Flash plan:" in text
    assert "Validation: OK" in text


def test_print_plan_lists_errors(
    fake_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _block_target(tmp_path)
    _patch_block_probe(monkeypatch, target, size_bytes=1)
    plan = flash.plan_flash(fake_image, target, "none")
    errors = flash.validate_plan(plan)
    out = io.StringIO()
    flash.print_plan(plan, errors=errors, file=out)
    text = out.getvalue()
    assert "Validation: FAILED" in text
    assert "larger than target" in text


def test_probe_target_block_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hit the real ``_probe_target`` once with stat() patched to look like a block device."""
    target = tmp_path / "block-real"
    target.write_bytes(b"")
    real_stat = target.stat()

    class _FakeStat:
        st_mode = stat.S_IFBLK | 0o600

    monkeypatch.setattr(Path, "stat", lambda self: _FakeStat() if self == target else real_stat)
    monkeypatch.setattr(flash, "_lsblk_target_size", lambda p: 12345 if p == target else None)
    monkeypatch.setattr(flash, "_lsblk_target_mountpoints", lambda _p: [])
    is_block, size, mounts = flash._probe_target(target)
    assert is_block is True
    assert size == 12345
    assert mounts == []
