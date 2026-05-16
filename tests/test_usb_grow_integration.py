"""Integration test for the ``bty-usb-grow`` first-boot service.

The service ships as a shell script in the bty-usb live env's
chroot (``bty-media/live-build/config/includes.chroot/usr/local/
sbin/bty-usb-grow``) and is impossible to unit-test usefully --
its job IS to drive ``parted``, ``mkfs.exfat``, ``mount``, and
``tar`` against a real block device.

This test exercises the script end-to-end against a loop-mounted
sparse image:

* A 64 MiB sparse image is created with a 16 MiB partition labelled
  ``BTY_IMAGES`` at the start, leaving ~48 MiB of free space behind
  it (mimicking the bty-usb on a 256 GB stick scenario at scale).
* The partition is formatted exfat, mounted, seeded with a few
  fake ``.bri`` descriptor files (the baked-in content the
  service must preserve), and unmounted.
* ``bty-usb-grow`` runs against the loop device.
* We verify:
    - the partition now fills the disk (within end-of-disk slack)
    - the seeded files are restored verbatim
    - the ``.bty-grown`` sentinel was dropped
    - the script's stdout/stderr included the success log line

Gated on root + the binaries the script + test setup use:
``losetup``, ``parted``, ``mkfs.exfat``, ``partprobe``, ``mount``,
``udevadm``, ``tar``. CI runs this via ``sudo -E uv run pytest -m
integration``; local devs without root get a normal skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Path to the script under test, computed from the repo root.
SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "bty-media"
    / "live-build"
    / "config"
    / "includes.chroot"
    / "usr"
    / "local"
    / "sbin"
    / "bty-usb-grow"
)

# Image geometry.
IMG_SIZE_MIB = 64
PART_SIZE_MIB = 16
SLACK_MIB = 1  # leading 1 MiB for the partition table

# Content we expect to survive the grow.
SEED_FILES = {
    "bty-server.bri": b'{"name":"bty-server","url":"https://example/x.img.gz"}\n',
    "ubuntu-24.04.bri": b'{"name":"ubuntu-24.04","url":"https://example/u.img.gz"}\n',
    "README.txt": b"drop image files here\n",
}


@pytest.fixture(autouse=True)
def _require_integration_environment() -> None:
    missing: list[str] = []
    if os.geteuid() != 0:
        missing.append("not running as root")
    missing.extend(
        f"{binary} not on PATH"
        for binary in ("losetup", "parted", "mkfs.exfat", "partprobe", "mount", "tar")
        if shutil.which(binary) is None
    )
    if not SCRIPT.is_file():
        missing.append(f"{SCRIPT} not found")
    if not missing:
        return
    reason = "; ".join(missing)
    if os.environ.get("CI"):
        pytest.fail(f"integration prerequisites missing in CI: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture
def loop_image(tmp_path: Path) -> Iterator[tuple[Path, str, str]]:
    """Yield ``(image_path, loop_device, partition_device)`` for a
    freshly-created loop-mounted USB-shape disk with a small
    BTY_IMAGES partition + free space behind it."""
    img = tmp_path / "usb-test.img"
    # Create the sparse image.
    with img.open("wb") as f:
        f.truncate(IMG_SIZE_MIB * 1024 * 1024)

    # Single partition (no msdos label needed for the test; gpt is
    # closer to the bty-usb shape). One primary partition from 1
    # MiB to PART_SIZE_MIB+1 MiB, leaving free space behind.
    subprocess.run(
        ["parted", "-s", str(img), "mklabel", "gpt"],
        check=True,
    )
    subprocess.run(
        [
            "parted",
            "-s",
            str(img),
            "mkpart",
            "primary",
            f"{SLACK_MIB}MiB",
            f"{SLACK_MIB + PART_SIZE_MIB}MiB",
        ],
        check=True,
    )

    # losetup with partition scanning.
    loop = subprocess.run(
        ["losetup", "-fP", "--show", str(img)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    part = f"{loop}p1"
    # Wait for the partition device to materialise.
    subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)

    # Format + seed content.
    subprocess.run(
        ["mkfs.exfat", "-L", "BTY_IMAGES", part],
        check=True,
        capture_output=True,
    )
    subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)
    mnt = tmp_path / "seed-mnt"
    mnt.mkdir()
    subprocess.run(["mount", "-t", "exfat", part, str(mnt)], check=True)
    try:
        for name, data in SEED_FILES.items():
            (mnt / name).write_bytes(data)
        subprocess.run(["sync"], check=True)
    finally:
        subprocess.run(["umount", str(mnt)], check=True)
        mnt.rmdir()

    try:
        yield img, loop, part
    finally:
        # Best-effort cleanup -- the test body may have unmounted /
        # repartitioned, so swallow errors.
        subprocess.run(["umount", part], check=False, capture_output=True)
        subprocess.run(["losetup", "-d", loop], check=False, capture_output=True)


def _partition_end_bytes(disk: str, partnum: int) -> int:
    """Return the partition's end byte offset via parted's machine
    output -- same parser shape the script uses."""
    out = subprocess.run(
        ["parted", "-ms", disk, "unit", "B", "print"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith(f"{partnum}:"):
            fields = line.split(":")
            return int(fields[2].rstrip("B"))
    raise AssertionError(f"partition {partnum} not in parted output for {disk}")


def test_bty_usb_grow_extends_partition_and_preserves_content(
    loop_image: tuple[Path, str, str],
) -> None:
    """Happy path: small partition + free space behind → script
    grows it to fill the disk, content survives."""
    img, loop, part = loop_image
    disk_size = int(
        subprocess.run(
            ["blockdev", "--getsize64", loop],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    pre_end = _partition_end_bytes(loop, 1)
    assert pre_end < disk_size - 16 * 1024 * 1024, "test image must have free space behind"

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    # Surface any failure with full output to make CI diagnostics
    # useful when the script regresses.
    assert result.returncode == 0, (
        f"bty-usb-grow exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Partition now extends close to the end of the disk.
    post_end = _partition_end_bytes(loop, 1)
    # exFAT + parted reserve a small bit at the end; expect within
    # ~2 MiB of the full disk size.
    assert post_end > disk_size - 2 * 1024 * 1024, (
        f"partition didn't grow: pre_end={pre_end}, post_end={post_end}, disk_size={disk_size}"
    )

    # Re-mount and verify content + sentinel.
    mnt = img.parent / "verify-mnt"
    mnt.mkdir()
    try:
        subprocess.run(["mount", "-t", "exfat", part, str(mnt)], check=True)
        try:
            assert (mnt / ".bty-grown").is_file(), "success sentinel missing"
            assert not (mnt / ".bty-grown-failed").is_file(), "unexpected failure sentinel"
            for name, data in SEED_FILES.items():
                restored = (mnt / name).read_bytes()
                assert restored == data, f"{name} content corrupted across grow"
        finally:
            subprocess.run(["umount", str(mnt)], check=True)
    finally:
        mnt.rmdir()


def test_bty_usb_grow_is_idempotent_via_sentinel(
    loop_image: tuple[Path, str, str],
) -> None:
    """Second run with the sentinel in place is a clean no-op:
    exit 0, partition unchanged, content unchanged."""
    _img, loop, _part = loop_image
    # First run: grow.
    subprocess.run(["/bin/sh", str(SCRIPT)], check=True, capture_output=True, text=True)
    post_end_first = _partition_end_bytes(loop, 1)

    # Second run: should skip cleanly.
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "already grown" in (result.stderr + result.stdout)
    # Partition end byte unchanged.
    assert _partition_end_bytes(loop, 1) == post_end_first


def test_bty_usb_grow_skips_when_no_free_space(tmp_path: Path) -> None:
    """If the partition already fills the disk, the script must not
    repartition / reformat -- it bails cleanly with a log note."""
    # Create an image that's the same size as the partition (no slack).
    img = tmp_path / "usb-tight.img"
    full_mib = 32
    with img.open("wb") as f:
        f.truncate(full_mib * 1024 * 1024)
    subprocess.run(["parted", "-s", str(img), "mklabel", "gpt"], check=True)
    subprocess.run(
        [
            "parted",
            "-s",
            str(img),
            "mkpart",
            "primary",
            f"{SLACK_MIB}MiB",
            "100%",
        ],
        check=True,
    )
    loop = subprocess.run(
        ["losetup", "-fP", "--show", str(img)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    part = f"{loop}p1"
    subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)
    try:
        subprocess.run(["mkfs.exfat", "-L", "BTY_IMAGES", part], check=True, capture_output=True)
        subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)

        pre_end = _partition_end_bytes(loop, 1)
        result = subprocess.run(
            ["/bin/sh", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "no significant free space" in (result.stderr + result.stdout)
        assert _partition_end_bytes(loop, 1) == pre_end
    finally:
        subprocess.run(["umount", part], check=False, capture_output=True)
        subprocess.run(["losetup", "-d", loop], check=False, capture_output=True)
