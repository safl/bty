"""
Stage the test disk for the USB auto-grow QEMU test
====================================================

Copies the freshly-baked ``bty-usb-x86_64-v*.iso`` into
``{qemu.guests.usb-grow.path}/disk.img`` and truncates the file to
4 GiB so ``bty-usb-grow.service`` (running inside the live env once
the guest boots) sees ~3 GiB of free space behind BTY_IMAGES and
grows the partition into it.

The host path is fixed by ``qemu.guests.usb-grow.path`` in the
cijoe config; the same path is referenced by ``system_args.raw`` in
the QEMU launch (``-drive file=.../disk.img,format=raw,if=ide``).
``qemu.guest_start`` doesn't manage this disk (cijoe's auto-boot
expects qcow2 at ``boot.img``); we keep cijoe's wrapper out of the
disk path entirely and let it just manage the QEMU process.

Retargetable: False (host-side staging on the cijoe initiator)
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME_GLOB = "bty-usb-x86_64-v*.iso"
TEST_DISK_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB target stick


def add_args(parser: ArgumentParser):
    del parser  # config-driven; no flags


def main(args, cijoe):
    del args
    iso_dir = Path(
        cijoe.getconf("test.usb_grow.iso_dir") or (Path.home() / "system_imaging" / "disk")
    )
    candidates = sorted(iso_dir.glob(ISO_BASENAME_GLOB))
    if not candidates:
        log.error(f"no {ISO_BASENAME_GLOB} found in {iso_dir} (did the usb-x86 build run?)")
        return errno.ENOENT
    src_iso = candidates[-1]  # sorted: highest-version .iso wins
    log.info(f"using {src_iso.name} ({src_iso.stat().st_size} bytes)")

    guest_path_raw = cijoe.getconf("qemu.guests.usb-grow.path")
    if not guest_path_raw:
        log.error("missing qemu.guests.usb-grow.path in cijoe config")
        return errno.EINVAL
    guest_path = Path(guest_path_raw)
    guest_path.mkdir(parents=True, exist_ok=True)
    disk = guest_path / "disk.img"

    log.info(f"staging {src_iso} -> {disk}")
    shutil.copy2(src_iso, disk)
    log.info(f"extending {disk} to {TEST_DISK_BYTES} bytes (4 GiB)")
    with disk.open("r+b") as fh:
        fh.truncate(TEST_DISK_BYTES)
    return 0
