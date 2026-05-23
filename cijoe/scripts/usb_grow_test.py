"""
USB auto-grow end-to-end test
=============================

Verifies ``bty-usb-grow.service`` extends the BTY_IMAGES exFAT
partition from its 32 MiB bake-time minimum to fill the underlying
disk on first boot. Boots the freshly-built .iso in QEMU (KVM) on a
4 GiB raw disk, lets the live env's first-boot grow service run, then
``parted``-s the disk image to assert the partition grew.

Output discipline: every shell call goes through ``cijoe.run_local``
so its stdout + stderr land in the cijoe report. The QEMU run wraps
in ``timeout`` so the command terminates cleanly (no Popen / DEVNULL
plumbing that silently eats failure modes), and the serial log is
``cat``'d to the report after QEMU exits regardless of outcome --
"why didn't it grow?" answers itself.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import re
import shutil
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME_GLOB = "bty-usb-x86_64-v*.iso"
TEST_DISK_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB target stick
# Boot + grow + idle window. Cold-boot in QEMU (KVM): ~30s; grow service:
# seconds; we leave a wide buffer for slow GHA runners. ``timeout`` SIGKILLs
# QEMU at the deadline; ``|| true`` keeps the shell exit clean.
BOOT_WINDOW_SEC = 240
# 32 MiB at bake -> ~3.5 GiB after grow on a 4 GiB stick. 1 GiB is a
# comfortable floor that distinguishes "grew" from "did not grow" without
# tying the test to the exact final size.
MIN_GROWN_BYTES = 1 * 1024 * 1024 * 1024


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.usb_grow", {})
    iso_dir = Path(cfg.get("iso_dir") or (Path.home() / "system_imaging" / "disk"))
    candidates = sorted(iso_dir.glob(ISO_BASENAME_GLOB))
    if not candidates:
        log.error(f"no {ISO_BASENAME_GLOB} found in {iso_dir} (did the usb-x86 build run?)")
        return errno.ENOENT
    src_iso = candidates[-1]  # sorted: highest-version .iso wins
    log.info(f"using {src_iso.name} ({src_iso.stat().st_size} bytes)")

    workspace = Path.cwd() / "_build" / "test-usb-grow"
    workspace.mkdir(parents=True, exist_ok=True)
    test_disk = workspace / "usb-grow-test.img"
    serial_log = workspace / "qemu.serial.log"

    log.info(f"staging {src_iso.name} -> {test_disk}")
    shutil.copy2(src_iso, test_disk)
    log.info(f"extending {test_disk} to {TEST_DISK_BYTES} bytes (4 GiB)")
    with test_disk.open("r+b") as fh:
        fh.truncate(TEST_DISK_BYTES)

    # Sanity-check the inputs by attaching the file as a loop device with
    # partition discovery (``losetup -fP``) -- that's the same view of the
    # disk the kernel inside the QEMU VM will get, and also the view
    # parted understands (parted reading the iso-hybrid file directly
    # trips on the 2048-vs-512 sector mismatch + reports the table as
    # ``unknown``; via a loop device the kernel does the sector math
    # before parted sees it).
    log.info("pre-boot inspection via loop device:")
    err, out = cijoe.run_local(f"sudo losetup -fP --show {test_disk}")
    if err:
        log.error(f"losetup -fP failed (err={err})")
        return errno.EIO
    loop_dev = out.output().strip().splitlines()[-1].strip() if hasattr(out, "output") else ""
    if not loop_dev.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO
    log.info(f"loop device: {loop_dev}")
    try:
        cijoe.run_local("sudo udevadm settle --timeout=10")
        cijoe.run_local(f"sudo parted -ms {loop_dev} unit B print")
    finally:
        cijoe.run_local(f"sudo losetup -d {loop_dev}")

    # Single QEMU run, wall-clocked by ``timeout``. ``-enable-kvm`` requires
    # /dev/kvm; the release.yml job installs cpu-checker + drops the udev
    # rule so the runner user can open it. ``-cpu host`` passes through the
    # runner's feature set so virtio + most modern paths work. Serial goes
    # to a file we cat afterwards so the kernel boot log is in the cijoe
    # report whether the test passes or fails.
    log.info(f"booting QEMU (BOOT_WINDOW_SEC={BOOT_WINDOW_SEC})")
    qemu_cmd = (
        f"timeout --kill-after=10 {BOOT_WINDOW_SEC} "
        f"qemu-system-x86_64 "
        f"-enable-kvm -cpu host -smp 2 -m 1G "
        f"-drive file={test_disk},format=raw,if=virtio "
        f"-nographic -serial file:{serial_log} "
        f"-nic none -no-reboot "
        f"|| true"  # timeout's 124 + qemu's variable exit codes both OK.
    )
    err, _ = cijoe.run_local(qemu_cmd)
    log.info(f"qemu exit handling done (cijoe err={err})")

    # Always dump the serial log to the report. The bty-usb-grow service
    # writes to /run/bty-usb-grow.status + syslog via ``logger -s``; the
    # ``-s`` flag mirrors to stderr, which (with systemd's default forward
    # rules) lands on the kernel console -> serial -> this file.
    if serial_log.is_file():
        log.info(f"serial log size: {serial_log.stat().st_size} bytes")
        cijoe.run_local(f"cat {serial_log}")
    else:
        log.error(f"serial log NOT created at {serial_log} -- qemu likely never started")

    # Post-boot inspection via a fresh loop device. bty-usb-grow inside
    # the VM rewrites the partition table via parted resizepart +
    # mkfs.exfat; the kernel ack/sync flushes the new table to disk
    # before the unit reports success, so a parted read here (through
    # losetup -P so the kernel does the geometry) is authoritative
    # even though the VM is gone.
    log.info("post-boot inspection via loop device:")
    err, out = cijoe.run_local(f"sudo losetup -fP --show {test_disk}")
    if err:
        log.error(f"post-boot losetup -fP failed (err={err})")
        return errno.EIO
    loop_dev = out.output().strip().splitlines()[-1].strip() if hasattr(out, "output") else ""
    if not loop_dev.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO
    log.info(f"loop device: {loop_dev}")
    try:
        cijoe.run_local("sudo udevadm settle --timeout=10")
        err, parted_out = cijoe.run_local(f"sudo parted -ms {loop_dev} unit B print")
    finally:
        cijoe.run_local(f"sudo losetup -d {loop_dev}")
    if err:
        log.error(f"post-boot parted failed (err={err})")
        return errno.EIO

    parted_text = parted_out.output() if hasattr(parted_out, "output") else str(parted_out)

    # parted -ms output: ``<num>:<start>B:<end>B:<size>B:<fs>:<name>:<flags>;``
    # one line per partition (after the BYT;\n<disk>;\n preamble).
    largest = 0
    for line in parted_text.splitlines():
        m = re.match(r"^(\d+):(\d+)B:(\d+)B:(\d+)B:", line)
        if not m:
            continue
        size = int(m.group(4))
        if size > largest:
            largest = size
    log.info(f"largest partition on disk image: {largest} bytes ({largest / (1 << 20):.1f} MiB)")

    if largest < MIN_GROWN_BYTES:
        log.error(
            f"FAIL: BTY_IMAGES did not grow. Largest partition is "
            f"{largest} bytes ({largest / (1 << 20):.1f} MiB); expected "
            f">= {MIN_GROWN_BYTES} bytes ({MIN_GROWN_BYTES / (1 << 30):.1f} GiB). "
            f"Inspect the serial-log dump above for the bty-usb-grow service's "
            f"trace + any kernel / systemd errors from the live env boot."
        )
        return errno.EPROTO

    log.info(
        f"PASS: BTY_IMAGES grew to {largest / (1 << 30):.2f} GiB on first boot "
        f"(bake-time min was 32 MiB; target stick was 4 GiB)."
    )
    return 0
