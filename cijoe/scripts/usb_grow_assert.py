"""
Assert BTY_IMAGES grew after the auto-grow QEMU boot
=====================================================

Final step of ``tasks/test-usb-grow.yaml``: loop-mounts the disk
image left behind by the QEMU run (``{qemu.guests.usb-grow.path}/
disk.img``, kernel-flushed by ``systemctl poweroff -i`` from the
in-VM ``core.cmdrunner`` step), reads the partition table via
``lsblk`` (which uses sysfs and is therefore agnostic to the
iso-hybrid MBR parted struggles with), and verifies the largest
partition is past the 1 GiB floor.

Pre-grow the trailing BTY_IMAGES partition is 32 MiB; on a 4 GiB
target stick it should land at ~3.5 GiB after grow. 1 GiB is a
comfortable threshold that distinguishes "grew" from "didn't"
without tying the test to the exact final size.

Retargetable: False (host-side parted/losetup on the cijoe initiator)
"""

from __future__ import annotations

import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path

MIN_GROWN_BYTES = 1 * 1024 * 1024 * 1024


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    guest_path_raw = cijoe.getconf("qemu.guests.usb-grow.path")
    if not guest_path_raw:
        log.error("missing qemu.guests.usb-grow.path in cijoe config")
        return errno.EINVAL
    disk = Path(guest_path_raw) / "disk.img"
    if not disk.is_file():
        log.error(f"disk image missing: {disk}")
        return errno.ENOENT

    log.info(f"loop-mount + lsblk on {disk}")
    err, out = cijoe.run_local(f"sudo losetup -fP --show {disk}")
    if err:
        log.error(f"losetup -fP failed (err={err})")
        return errno.EIO
    loop_dev = out.output().strip().splitlines()[-1].strip() if hasattr(out, "output") else ""
    if not loop_dev.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO
    log.info(f"loop device: {loop_dev}")

    largest = 0
    try:
        cijoe.run_local("sudo udevadm settle --timeout=10")
        err, lsblk_out = cijoe.run_local(f"sudo lsblk -bno NAME,SIZE,TYPE,LABEL {loop_dev}")
        if err:
            log.error(f"lsblk failed (err={err})")
            return errno.EIO
        text = lsblk_out.output() if hasattr(lsblk_out, "output") else str(lsblk_out)
        log.info(f"lsblk output:\n{text}")
        # lsblk -bno NAME,SIZE,TYPE,LABEL: one row per device.
        # The largest ``part`` is BTY_IMAGES (post-grow it dominates).
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                size = int(parts[1])
            except ValueError:
                continue
            if parts[2] != "part":
                continue
            if size > largest:
                largest = size
    finally:
        cijoe.run_local(f"sudo losetup -d {loop_dev}")

    log.info(f"largest partition: {largest} bytes ({largest / (1 << 20):.1f} MiB)")
    if largest < MIN_GROWN_BYTES:
        log.error(
            f"FAIL: BTY_IMAGES did not grow. Largest partition is "
            f"{largest} bytes ({largest / (1 << 20):.1f} MiB); expected "
            f">= {MIN_GROWN_BYTES} bytes ({MIN_GROWN_BYTES / (1 << 30):.1f} GiB). "
            f"Inspect the in-VM diagnostics from the preceding step "
            f"(systemctl + journalctl + /run/bty-usb-grow.status)."
        )
        return errno.EPROTO
    log.info(
        f"PASS: BTY_IMAGES grew to {largest / (1 << 30):.2f} GiB on first boot "
        f"(bake-time min was 32 MiB; target stick was 4 GiB)."
    )
    return 0
