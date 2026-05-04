"""Tests for bty.flash.

Validation logic (``make_plan`` / ``validate_plan`` / ``print_plan``) is
exercised with hand-built ``ImageInfo`` / ``TargetInfo`` dataclasses -
no mocking. The probe functions, which actually shell out, get their
own targeted tests; subprocess calls are patched there because tests
can't (and shouldn't) actually run ``qemu-img`` / ``zstd`` / ``lsblk``.
"""

from __future__ import annotations

import io
import json
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
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img"), _tgt(), "none")
    flash.execute_plan(plan)

    assert calls == ["img", "sync", "partprobe"]


def test_execute_plan_dispatches_to_zst_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="img.zst"), _tgt(), "none")
    flash.execute_plan(plan)

    assert calls == ["zst", "sync", "partprobe"]


def test_execute_plan_dispatches_to_qcow2_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t: calls.append("img"))
    monkeypatch.setattr(flash, "_flash_zst", lambda _i, _t: calls.append("zst"))
    monkeypatch.setattr(flash, "_flash_qcow2", lambda _i, _t: calls.append("qcow2"))
    _stub_post_write(monkeypatch, calls)

    plan = flash.make_plan(_img(fmt="qcow2"), _tgt(), "none")
    flash.execute_plan(plan)

    assert calls == ["qcow2", "sync", "partprobe"]


def test_execute_plan_emits_lifecycle_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress callback receives started -> writing -> synced -> partprobed."""
    calls: list[str] = []
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)
    monkeypatch.setattr(flash, "_flash_img", lambda _i, _t: None)
    _stub_post_write(monkeypatch, calls)  # we don't care about call order here

    events: list[flash.FlashProgress] = []
    plan = flash.make_plan(_img(fmt="img", virtual=12345), _tgt(), "none")
    flash.execute_plan(plan, progress=events.append)

    names = [e.event for e in events]
    assert names == ["started", "writing", "synced", "partprobed"]
    assert events[0].total_bytes == 12345
    assert events[1].note == "img"


def test_execute_plan_emits_failed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any FlashError gets a 'failed' progress event before the re-raise."""
    monkeypatch.setattr(flash, "probe_target", _stub_block_target)

    def boom(_i: Path, _t: Path) -> None:
        raise flash.FlashError("simulated dd failure")

    monkeypatch.setattr(flash, "_flash_img", boom)

    events: list[flash.FlashProgress] = []
    plan = flash.make_plan(_img(fmt="img"), _tgt(), "none")
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan, progress=events.append)

    assert events[-1].event == "failed"
    assert "simulated dd failure" in events[-1].note


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


# ---------- apply_cloud_init: arg validation + helper logic ------------------


def test_apply_cloud_init_missing_user_data_raises(tmp_path: Path) -> None:
    with pytest.raises(flash.FlashError, match="user-data file not found"):
        flash.apply_cloud_init(Path("/dev/null"), tmp_path / "nope.yaml")


def test_apply_cloud_init_missing_meta_data_raises(tmp_path: Path) -> None:
    user = tmp_path / "user-data"
    user.write_text("#cloud-config\n")
    with pytest.raises(flash.FlashError, match="meta-data file not found"):
        flash.apply_cloud_init(Path("/dev/null"), user, tmp_path / "nope.yaml")


def test_default_meta_data_has_unique_instance_id() -> None:
    a = flash._default_meta_data()
    b = flash._default_meta_data()
    assert a != b
    assert a.startswith("instance-id: bty-")
    assert "local-hostname" in a


def test_find_cloud_init_rootfs_returns_partition_with_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First child partition that has /etc/cloud/ wins."""
    fake_lsblk = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "blockdevices": [
                    {
                        "path": "/dev/loopX",
                        "type": "disk",
                        "children": [
                            {"path": "/dev/loopXp1", "type": "part"},
                            {"path": "/dev/loopXp2", "type": "part"},
                        ],
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **kw: fake_lsblk)

    seen: list[Path] = []

    def fake_marker(part: Path) -> bool:
        seen.append(part)
        return part == Path("/dev/loopXp2")

    monkeypatch.setattr(flash, "_partition_has_cloud_init", fake_marker)
    rootfs = flash._find_cloud_init_rootfs(Path("/dev/loopX"))
    assert rootfs == Path("/dev/loopXp2")
    assert seen == [Path("/dev/loopXp1"), Path("/dev/loopXp2")]


def test_find_cloud_init_rootfs_raises_when_no_partition_has_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_lsblk = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "blockdevices": [
                    {
                        "path": "/dev/loopX",
                        "type": "disk",
                        "children": [{"path": "/dev/loopXp1", "type": "part"}],
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **kw: fake_lsblk)
    monkeypatch.setattr(flash, "_partition_has_cloud_init", lambda _p: False)

    with pytest.raises(flash.FlashError, match=r"no partition.*cloud-init installed"):
        flash._find_cloud_init_rootfs(Path("/dev/loopX"))


def test_find_cloud_init_rootfs_handles_flat_lsblk_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some ``lsblk`` versions return the disk and its partitions as siblings
    at the top level of ``blockdevices`` instead of nesting partitions under
    ``children``. Verify we cope with that shape too.
    """
    fake_lsblk = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "blockdevices": [
                    {"path": "/dev/loopX", "type": "loop"},
                    {"path": "/dev/loopXp1", "type": "part"},
                ]
            }
        ),
    )
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **kw: fake_lsblk)
    monkeypatch.setattr(flash, "_partition_has_cloud_init", lambda p: p == Path("/dev/loopXp1"))

    rootfs = flash._find_cloud_init_rootfs(Path("/dev/loopX"))
    assert rootfs == Path("/dev/loopXp1")


# ---------- apply_cijoe: arg validation + helpers ----------------------------


def test_apply_cijoe_missing_workflow_raises(tmp_path: Path) -> None:
    with pytest.raises(flash.FlashError, match="cijoe workflow not found"):
        flash.apply_cijoe(Path("/dev/null"), tmp_path / "missing.yaml")


def test_apply_cijoe_missing_config_raises(tmp_path: Path) -> None:
    wf = tmp_path / "wf.yaml"
    wf.write_text("steps: []\n")
    with pytest.raises(flash.FlashError, match="cijoe config not found"):
        flash.apply_cijoe(Path("/dev/null"), wf, tmp_path / "missing.toml")


def test_apply_cijoe_missing_cijoe_binary_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = tmp_path / "wf.yaml"
    wf.write_text("steps: []\n")
    monkeypatch.setattr(flash.shutil, "which", lambda _name: None)
    with pytest.raises(flash.FlashError, match="cijoe is not installed"):
        flash.apply_cijoe(Path("/dev/null"), wf)


def test_find_largest_partition_picks_biggest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_lsblk = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "blockdevices": [
                    {"path": "/dev/loopX", "type": "loop", "size": 0},
                    {"path": "/dev/loopXp1", "type": "part", "size": 1024},
                    {"path": "/dev/loopXp2", "type": "part", "size": 8192},
                    {"path": "/dev/loopXp3", "type": "part", "size": 4096},
                ]
            }
        ),
    )
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **kw: fake_lsblk)
    chosen = flash._find_largest_partition(Path("/dev/loopX"))
    assert chosen == Path("/dev/loopXp2")


def test_find_largest_partition_raises_when_none_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_lsblk = MagicMock(
        returncode=0,
        stdout=json.dumps({"blockdevices": [{"path": "/dev/loopX", "type": "loop", "size": 0}]}),
    )
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **kw: fake_lsblk)
    with pytest.raises(flash.FlashError, match="no partitions found"):
        flash._find_largest_partition(Path("/dev/loopX"))
