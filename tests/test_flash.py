"""Tests for bty.flash.

Validation logic (``make_plan`` / ``validate_plan`` / ``print_plan``) is
exercised with hand-built ``ImageInfo`` / ``TargetInfo`` dataclasses —
no mocking. The probe functions, which actually shell out, get their
own targeted tests; subprocess calls are patched there because tests
can't (and shouldn't) actually run ``qemu-img`` / ``zstd`` / ``lsblk``.
"""

from __future__ import annotations

import io
from pathlib import Path
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
    plan = flash.make_plan(_img(), _tgt(), "none")
    assert plan.notes == []


def test_make_plan_notes_unknown_virtual_size() -> None:
    plan = flash.make_plan(_img(virtual=None), _tgt(), "none")
    assert any("size-fits-target check skipped" in n for n in plan.notes)


def test_make_plan_skips_note_when_format_is_unrecognised() -> None:
    """Unrecognised format already yields a validation error;
    we don't double-report it as a 'virtual size unknown' note."""
    plan = flash.make_plan(_img(fmt=None, virtual=None), _tgt(), "none")
    assert all("size-fits-target check skipped" not in n for n in plan.notes)


def test_validate_ok_for_sane_plan() -> None:
    plan = flash.make_plan(_img(virtual=1024), _tgt(size=1024 * 1024), "none")
    assert flash.validate_plan(plan) == []


def test_validate_unknown_format() -> None:
    plan = flash.make_plan(_img(fmt=None), _tgt(), "none")
    errors = flash.validate_plan(plan)
    assert any("image format not recognised" in e for e in errors)


def test_validate_target_missing() -> None:
    plan = flash.make_plan(_img(), _tgt(exists=False, is_block=False, size=None), "none")
    errors = flash.validate_plan(plan)
    assert any("does not exist" in e for e in errors)


def test_validate_target_not_block() -> None:
    plan = flash.make_plan(_img(), _tgt(is_block=False, size=None), "none")
    errors = flash.validate_plan(plan)
    assert any("not a block device" in e for e in errors)


def test_validate_target_too_small() -> None:
    plan = flash.make_plan(_img(virtual=10_000), _tgt(size=1_000), "none")
    errors = flash.validate_plan(plan)
    assert any("larger than target" in e for e in errors)


def test_validate_target_mounted() -> None:
    plan = flash.make_plan(_img(), _tgt(mountpoints=["/", "/boot"]), "none")
    errors = flash.validate_plan(plan)
    assert any("mounted partitions" in e for e in errors)


def test_validate_unknown_provisioning_mode() -> None:
    plan = flash.make_plan(_img(), _tgt(), "garbage")
    errors = flash.validate_plan(plan)
    assert any("unknown provisioning mode" in e for e in errors)


def test_validate_skips_size_check_when_virtual_unknown() -> None:
    plan = flash.make_plan(_img(virtual=None), _tgt(size=1), "none")
    errors = flash.validate_plan(plan)
    assert all("larger than target" not in e for e in errors)


def test_print_plan_renders_validation_status() -> None:
    plan = flash.make_plan(_img(), _tgt(), "none")
    out = io.StringIO()
    flash.print_plan(plan, errors=[], file=out)
    text = out.getvalue()
    assert "Flash plan:" in text
    assert "Validation: OK" in text


def test_print_plan_lists_errors() -> None:
    plan = flash.make_plan(_img(virtual=10_000), _tgt(size=1), "none")
    errors = flash.validate_plan(plan)
    out = io.StringIO()
    flash.print_plan(plan, errors=errors, file=out)
    text = out.getvalue()
    assert "Validation: FAILED" in text
    assert "larger than target" in text


def test_print_plan_renders_notes() -> None:
    plan = flash.make_plan(_img(virtual=None), _tgt(), "none")
    out = io.StringIO()
    flash.print_plan(plan, errors=[], file=out)
    text = out.getvalue()
    assert "Notes:" in text
    assert "size-fits-target check skipped" in text


def test_to_dict_round_trips_plain_types() -> None:
    plan = flash.make_plan(_img(), _tgt(mountpoints=["/"]), "none")
    payload = plan.to_dict()
    assert payload["image"]["format"] == "img"
    assert payload["target"]["mountpoints"] == ["/"]
    assert payload["provisioning_mode"] == "none"


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
    """A regular file is not a block device — covered without any patching."""
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


def test_execute_plan_dispatches_to_img_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path, Path]] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda i, t: calls.append(("img", i, t)))
    monkeypatch.setattr(flash, "_flash_zst", lambda i, t: calls.append(("zst", i, t)))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda i, t: calls.append(("qcow2", i, t)))
    monkeypatch.setattr(flash, "_sync_and_partprobe", lambda t: calls.append(("sync", t, t)))

    plan = flash.make_plan(_img(fmt="img"), _tgt(), "none")
    flash.execute_plan(plan)

    formats = [c[0] for c in calls]
    assert formats == ["img", "sync"]


def test_execute_plan_dispatches_to_zst_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda i, t: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda i, t: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda i, t: calls.append("qcow2"))
    monkeypatch.setattr(flash, "_sync_and_partprobe", lambda t: calls.append("sync"))

    plan = flash.make_plan(_img(fmt="img.zst"), _tgt(), "none")
    flash.execute_plan(plan)

    assert calls == ["zst", "sync"]


def test_execute_plan_dispatches_to_qcow2_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda i, t: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda i, t: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda i, t: calls.append("qcow2"))
    monkeypatch.setattr(flash, "_sync_and_partprobe", lambda t: calls.append("sync"))

    plan = flash.make_plan(_img(fmt="qcow2"), _tgt(), "none")
    flash.execute_plan(plan)

    assert calls == ["qcow2", "sync"]


def test_execute_plan_refuses_unknown_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    plan = flash.make_plan(_img(fmt=None), _tgt(), "none")
    with pytest.raises(flash.FlashError, match="cannot flash image of format"):
        flash.execute_plan(plan)


def test_execute_plan_refuses_when_target_no_longer_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race protection: target was a block device at plan time but isn't now."""

    def now_a_regular_file(path: Path) -> flash.TargetInfo:
        return flash.TargetInfo(
            path=path, exists=True, is_block_device=False, size_bytes=None, mountpoints=[]
        )

    monkeypatch.setattr(flash, "probe_target", now_a_regular_file)
    plan = flash.make_plan(_img(), _tgt(), "none")
    with pytest.raises(flash.FlashError, match="no longer a block device"):
        flash.execute_plan(plan)


def test_execute_plan_refuses_when_target_now_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def now_mounted(path: Path) -> flash.TargetInfo:
        return flash.TargetInfo(
            path=path,
            exists=True,
            is_block_device=True,
            size_bytes=1024,
            mountpoints=["/mnt/oops"],
        )

    monkeypatch.setattr(flash, "probe_target", now_mounted)
    plan = flash.make_plan(_img(), _tgt(), "none")
    with pytest.raises(flash.FlashError, match="now has mounted partitions"):
        flash.execute_plan(plan)
