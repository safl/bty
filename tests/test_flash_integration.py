"""Integration tests for ``bty.flash`` against a real loop device.

These tests actually invoke ``losetup``, ``dd``, ``zstd``, ``qemu-img``,
and ``partprobe`` — the same external tools the production code calls.
They are gated on root and the availability of those binaries because
``losetup`` and writing to ``/dev/loopN`` require privileges that
ordinary contributor checkouts won't have.

CI runs them via ``sudo -E uv run pytest -m integration`` in a
dedicated job; ``uv run pytest`` (the default) excludes them so dev
loops stay fast.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from bty import flash

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _require_integration_environment() -> None:
    """Skip when prereqs are missing locally, but fail loudly in CI.

    CI sets ``CI=true``; if integration prereqs are missing there, that is a
    misconfiguration we want to see, not silently skip. Local contributors
    without root just get a normal skip.
    """
    missing: list[str] = []
    if os.geteuid() != 0:
        missing.append("not running as root")
    if shutil.which("losetup") is None:
        missing.append("losetup not on PATH")
    if shutil.which("dd") is None:
        missing.append("dd not on PATH")

    if not missing:
        return

    reason = "; ".join(missing)
    if os.environ.get("CI"):
        pytest.fail(f"integration prerequisites missing in CI: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture
def loop_device(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Yield ``(loop_dev_path, backing_file_path)`` and clean up after."""
    backing = tmp_path / "backing.img"
    subprocess.run(["truncate", "-s", "8M", str(backing)], check=True)

    setup = subprocess.run(
        ["losetup", "-f", "--show", str(backing)],
        capture_output=True,
        text=True,
        check=True,
    )
    loop_dev = Path(setup.stdout.strip())
    try:
        yield loop_dev, backing
    finally:
        subprocess.run(["losetup", "-d", str(loop_dev)], check=False)


def test_flash_raw_img_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    loop_dev, backing = loop_device

    payload = b"BTYTEST!" * 1024  # 8 KiB of recognisable bytes
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)

    image_info = flash.probe_image(ref)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info, "none")
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


def test_flash_qcow2_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img not available")
    loop_dev, backing = loop_device

    # Build a tiny qcow2 holding a known byte pattern.
    raw = tmp_path / "ref.raw"
    payload = b"QCOW2TST" * 1024  # 8 KiB
    raw.write_bytes(payload)

    qcow2 = tmp_path / "ref.qcow2"
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2)],
        check=True,
    )

    image_info = flash.probe_image(qcow2)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info, "none")
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


def test_flash_zst_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    if shutil.which("zstd") is None:
        pytest.skip("zstd not available")
    loop_dev, backing = loop_device

    raw = tmp_path / "ref.img"
    payload = b"ZSTDTEST" * 1024
    raw.write_bytes(payload)

    zst = tmp_path / "ref.img.zst"
    subprocess.run(["zstd", "-q", "-f", "-o", str(zst), str(raw)], check=True)

    image_info = flash.probe_image(zst)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info, "none")
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


# ---------- cloud-init application -------------------------------------------


@pytest.fixture
def cloud_init_loop_device(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A loop-device with one ext4 partition and an ``/etc/cloud/`` marker.

    Yields ``(loop_dev_path, partition_dev_path)``. apply_cloud_init's
    rootfs probe should match the partition because it carries the
    cloud-init marker.
    """
    for tool in ("sgdisk", "mkfs.ext4", "mount", "umount", "partprobe", "udevadm"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")

    backing = tmp_path / "backing-ci.img"
    subprocess.run(["truncate", "-s", "64M", str(backing)], check=True)

    setup = subprocess.run(
        ["losetup", "-f", "--show", "-P", str(backing)],
        capture_output=True,
        text=True,
        check=True,
    )
    loop_dev = Path(setup.stdout.strip())

    try:
        # GPT with a single partition filling the device.
        subprocess.run(
            ["sgdisk", "--new=1:0:0", "--typecode=1:8300", str(loop_dev)],
            check=True,
            capture_output=True,
        )
        subprocess.run(["partprobe", str(loop_dev)], check=False, capture_output=True)
        # Wait for udev to populate the new partition device node so
        # subsequent ``lsblk`` calls see it.
        subprocess.run(["udevadm", "settle"], check=False)
        partition = Path(f"{loop_dev}p1")

        # Plant ext4 + a /etc/cloud/ marker so apply_cloud_init can find us.
        subprocess.run(["mkfs.ext4", "-q", str(partition)], check=True)
        with tempfile.TemporaryDirectory(prefix="bty-test-mount-") as mp:
            subprocess.run(["mount", str(partition), mp], check=True)
            try:
                (Path(mp) / "etc" / "cloud").mkdir(parents=True)
            finally:
                subprocess.run(["umount", mp], check=True)

        yield loop_dev, partition
    finally:
        subprocess.run(["losetup", "-d", str(loop_dev)], check=False)


def test_apply_cloud_init_writes_seed_files(
    tmp_path: Path,
    cloud_init_loop_device: tuple[Path, Path],
) -> None:
    loop_dev, partition = cloud_init_loop_device

    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\nhostname: bty-integration-test\n")
    meta_data = tmp_path / "meta-data"
    meta_data.write_text("instance-id: bty-test-001\nlocal-hostname: bty-it\n")

    flash.apply_cloud_init(loop_dev, user_data, meta_data)

    # Mount and verify the seed landed at the canonical NoCloud path.
    with tempfile.TemporaryDirectory(prefix="bty-verify-") as mp:
        subprocess.run(["mount", str(partition), mp], check=True)
        try:
            seed_dir = Path(mp) / "var" / "lib" / "cloud" / "seed" / "nocloud-net"
            assert (seed_dir / "user-data").read_text() == user_data.read_text()
            assert (seed_dir / "meta-data").read_text() == meta_data.read_text()
        finally:
            subprocess.run(["umount", mp], check=True)


def test_apply_cloud_init_synthesises_meta_data(
    tmp_path: Path,
    cloud_init_loop_device: tuple[Path, Path],
) -> None:
    loop_dev, partition = cloud_init_loop_device

    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")

    flash.apply_cloud_init(loop_dev, user_data, meta_data=None)

    with tempfile.TemporaryDirectory(prefix="bty-verify-") as mp:
        subprocess.run(["mount", str(partition), mp], check=True)
        try:
            meta = (
                Path(mp) / "var" / "lib" / "cloud" / "seed" / "nocloud-net" / "meta-data"
            ).read_text()
            assert meta.startswith("instance-id: bty-")
            assert "local-hostname" in meta
        finally:
            subprocess.run(["umount", mp], check=True)


def test_apply_cloud_init_errors_on_image_without_marker(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    """A bare loop device (no /etc/cloud/) is rejected as not cloud-init-enabled."""
    loop_dev, _ = loop_device
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")

    with pytest.raises(flash.FlashError, match="cloud-init"):
        flash.apply_cloud_init(loop_dev, user_data)
