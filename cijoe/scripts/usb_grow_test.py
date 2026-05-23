"""
USB auto-grow end-to-end test
=============================

Verifies ``bty-usb-grow.service`` extends the BTY_IMAGES exFAT
partition from its 1 MiB bake-time minimum to fill the underlying disk
on first boot.

Approach:

1. Copy the built ``bty-usb-x86_64.iso`` into a 4 GiB raw file
   (``truncate`` extends the file with zeros; the trailing space
   simulates the empty tail of a larger USB stick).
2. Boot the file in QEMU, headless, with serial captured.
3. Wait a fixed boot+grow window, then power-cycle the VM.
4. Inspect the post-boot partition table with ``parted``: assert the
   trailing (BTY_IMAGES) partition grew well past 1 MiB.

Doesn't depend on a serial marker -- bty-usb-grow's success log goes to
the systemd journal, which doesn't auto-forward to ``/dev/console``.
The disk-image inspection is the durable check: the resized partition
table is persisted before the unit reports success, so a post-boot
``parted`` read is authoritative.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import re
import shutil
import subprocess
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME = "bty-usb-x86_64.iso"
# 4 GiB stick is plenty of headroom over the ~200 MiB ISO + 1 MiB
# BTY_IMAGES, and small enough to keep the runner's disk + boot time
# bounded.
TEST_DISK_BYTES = 4 * 1024 * 1024 * 1024
# Boot + grow window. Live env cold-boot in QEMU lands in the ~30-60s
# range; bty-usb-grow itself is seconds (parted + mkfs.exfat on a
# nearly-empty partition + tar restore of the ~few KB starter .bri
# set). 180s is generous and bounded by the GHA job timeout.
BOOT_WINDOW_SEC = 180
# The 1 MiB bake size grown to a 4 GiB disk should land at >= ~3.5
# GiB (the live env's CD-ROM partition + some metadata slack are
# what's not BTY_IMAGES). 1 GiB is a comfortable floor that
# distinguishes "grew" from "didn't grow" without needing to know
# the exact final size.
MIN_GROWN_BYTES = 1024 * 1024 * 1024


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.usb_grow", {})
    iso_dir = Path(cfg.get("iso_dir") or (Path.home() / "system_imaging" / "disk"))
    src_iso = iso_dir / ISO_BASENAME
    if not src_iso.is_file():
        log.error(f"ISO missing: {src_iso} (did the usb-x86 build run?)")
        return errno.ENOENT

    workspace = Path.cwd() / "_build" / "test-usb-grow"
    workspace.mkdir(parents=True, exist_ok=True)
    test_disk = workspace / "usb-grow-test.img"
    serial_log = workspace / "qemu.serial.log"

    log.info(f"Staging {src_iso} ({src_iso.stat().st_size} bytes) -> {test_disk}")
    shutil.copy2(src_iso, test_disk)
    log.info(f"Extending {test_disk} to {TEST_DISK_BYTES} bytes (4 GiB)")
    with test_disk.open("r+b") as fh:
        fh.truncate(TEST_DISK_BYTES)

    qemu_cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "1G",
        "-drive",
        f"file={test_disk},format=raw,if=virtio",
        "-nographic",
        "-serial",
        f"file:{serial_log}",
        # bty-usb-grow runs entirely on local block devs; no network
        # needed. The default NIC also slows boot by ~5s waiting for
        # DHCP timeouts.
        "-nic",
        "none",
        # ``-no-reboot``: when the live env's bty-on-tty1 wizard quits
        # (or systemd reboots for any reason), QEMU exits instead of
        # cycling. The test would otherwise hang past BOOT_WINDOW_SEC.
        "-no-reboot",
    ]
    log.info(f"Booting QEMU (window={BOOT_WINDOW_SEC}s, serial -> {serial_log})")
    proc = subprocess.Popen(
        qemu_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            proc.wait(timeout=BOOT_WINDOW_SEC)
            log.info("QEMU exited on its own (likely -no-reboot fired)")
        except subprocess.TimeoutExpired:
            log.info(f"Boot window elapsed ({BOOT_WINDOW_SEC}s); terminating QEMU")
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except Exception as exc:
        log.error(f"QEMU launch / wait failed: {exc}")
        proc.kill()
        return errno.EIO

    # Inspect the post-boot partition table. parted's machine-readable
    # output is one row per partition:
    #   <num>:<start>B:<end>B:<size>B:<fs>:<name>:<flags>
    result = subprocess.run(
        ["parted", "-ms", str(test_disk), "unit", "B", "print"],
        capture_output=True,
        text=True,
        check=False,
    )
    log.info(f"parted output (rc={result.returncode}):\n{result.stdout}")
    if result.returncode != 0:
        log.error(f"parted stderr: {result.stderr}")
        _dump_serial_tail(serial_log)
        return errno.EIO

    largest = 0
    for line in result.stdout.splitlines():
        m = re.match(r"^(\d+):(\d+)B:(\d+)B:(\d+)B:", line)
        if not m:
            continue
        size = int(m.group(4))
        if size > largest:
            largest = size

    log.info(f"Largest partition on the test disk: {largest} bytes ({largest / (1 << 20):.1f} MiB)")
    if largest < MIN_GROWN_BYTES:
        log.error(
            f"FAIL: BTY_IMAGES did not grow. Largest partition is "
            f"{largest} bytes ({largest / (1 << 20):.1f} MiB); expected "
            f">= {MIN_GROWN_BYTES} ({MIN_GROWN_BYTES / (1 << 30):.1f} GiB)."
        )
        _dump_serial_tail(serial_log)
        return errno.EPROTO

    log.info(
        f"PASS: BTY_IMAGES grew from 1 MiB (bake) to "
        f"{largest / (1 << 30):.2f} GiB (parted-observed) on first boot"
    )
    return 0


def _dump_serial_tail(path: Path, lines: int = 200) -> None:
    if not path.is_file():
        log.error(f"{path}: serial log missing")
        return
    body = path.read_text(encoding="utf-8", errors="replace")
    log.error(f"--- last {lines} serial lines from {path} ---")
    for line in body.splitlines()[-lines:]:
        log.error(line)
