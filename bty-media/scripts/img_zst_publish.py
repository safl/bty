"""
Publish a baked qcow2 as a dd-able .img.zst
============================================

Converts the qcow2 produced by ``diskimage_build`` to raw, zstd-compresses
the result, and writes a sha256sum alongside. The .img.zst is the final
artifact an operator pipes through ``zstd -d`` and ``dd`` (or any USB
flasher tool that handles zstd) onto a USB stick.

Reads the ``publish`` section of ``system-imaging.images.<image_name>``:

  publish.raw_path    output path for the intermediate raw image
  publish.zst_path    output path for the .img.zst artifact
  publish.zstd_level  compression level (1..19; 19 is the default)

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path


def add_args(parser: ArgumentParser):
    parser.add_argument(
        "--image_name",
        type=str,
        default=None,
        help="Override the system-imaging image to publish. Defaults to "
        "bty-<variant>-x86_64 (variant from [bty] in the cijoe config).",
    )


def main(args, cijoe):
    image_name = args.image_name or _default_image_name(cijoe)
    images = cijoe.getconf("system-imaging.images", {})
    image = images.get(image_name)
    if not image:
        log.error(f"Image '{image_name}' not found in config")
        return errno.EINVAL

    disk = image.get("disk", {})
    publish = image.get("publish", {})
    if not publish:
        log.error(f"Image '{image_name}' has no [publish] section")
        return errno.EINVAL

    qcow2_path = Path(disk["path"])
    raw_path = Path(publish["raw_path"])
    zst_path = Path(publish["zst_path"])
    level = int(publish.get("zstd_level", 19))

    if not qcow2_path.exists():
        log.error(f"Baked qcow2 not found: {qcow2_path}")
        return errno.ENOENT

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    zst_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(f"Converting {qcow2_path} -> {raw_path} (raw)")
    err, _ = cijoe.run_local(f"qemu-img convert -O raw {qcow2_path} {raw_path}")
    if err:
        log.error("Failed converting qcow2 to raw")
        return err

    log.info(f"Compressing {raw_path} -> {zst_path} (zstd -{level})")
    err, _ = cijoe.run_local(f"zstd -{level} -T0 -f {raw_path} -o {zst_path}")
    if err:
        log.error("Failed zstd-compressing raw image")
        return err

    err, _ = cijoe.run_local(f"sha256sum {zst_path} > {zst_path}.sha256")
    if err:
        log.error("Failed computing sha256sum")
        return err

    cijoe.run_local(f"ls -la {zst_path}")
    cijoe.run_local(f"cat {zst_path}.sha256")

    return 0


def _default_image_name(cijoe) -> str:
    bty = cijoe.getconf("bty", {})
    variant = bty.get("variant", "usb")
    return f"bty-{variant}-x86_64"
