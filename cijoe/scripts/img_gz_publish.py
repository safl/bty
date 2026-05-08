"""
Publish a baked qcow2 as a dd-able .img.gz
===========================================

Converts the qcow2 produced by ``diskimage_build`` to raw,
gzip-compresses the result, and writes a sha256sum alongside.
The .img.gz is the final artifact an operator pipes through
``gunzip -d`` and ``dd`` (or any flasher tool that handles
gzip - all of them) onto a target disk.

Why gzip rather than zstd: bty-shipped appliance images are
flashed once during operator setup, not on a per-job CI hot
path. The flash-time decompression-speed argument that
originally drove .zst applies to operator-supplied target
images (which the bty flash code still accepts in any of
.img.{zst,xz,gz,bz2}); for the appliance itself, gzip's
universal flasher / OS / tooling support wins over zstd's
marginal speed advantage on a one-shot setup. Same rationale
that drove the .iso.xz -> .iso.gz switch in v0.5.4.

Reads the ``publish`` section of ``system-imaging.images.<image_name>``:

  publish.raw_path    output path for the intermediate raw image
  publish.gz_path     output path for the .img.gz artifact
  publish.gzip_level  compression level (1..9; 9 is the default)

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
    gz_path = Path(publish["gz_path"])
    level = int(publish.get("gzip_level", 9))

    if not qcow2_path.exists():
        log.error(f"Baked qcow2 not found: {qcow2_path}")
        return errno.ENOENT

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    gz_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(f"Converting {qcow2_path} -> {raw_path} (raw)")
    err, _ = cijoe.run_local(f"qemu-img convert -O raw {qcow2_path} {raw_path}")
    if err:
        log.error("Failed converting qcow2 to raw")
        return err

    # ``gzip -<level> -c`` writes to stdout; ``-f`` overwrites the
    # destination. Single-stream output is what every flasher / OS
    # tooling handles uniformly (the lesson from v0.5.4's .iso.gz
    # switch carried over).
    log.info(f"Compressing {raw_path} -> {gz_path} (gzip -{level})")
    err, _ = cijoe.run_local(f"sh -c 'gzip -{level} -c {raw_path} > {gz_path}'")
    if err:
        log.error("Failed gzip-compressing raw image")
        return err

    err, _ = cijoe.run_local(f"sha256sum {gz_path} > {gz_path}.sha256")
    if err:
        log.error("Failed computing sha256sum")
        return err

    cijoe.run_local(f"ls -la {gz_path}")
    cijoe.run_local(f"cat {gz_path}.sha256")

    return 0


def _default_image_name(cijoe) -> str:
    bty = cijoe.getconf("bty", {})
    variant = bty.get("variant", "server-x86")
    # Strip arch suffix to derive the role; image config keys stay
    # role-named (``bty-server-x86_64``) so published download URLs
    # don't churn when variant strings change.
    role = variant.split("-")[0]
    return f"bty-{role}-x86_64"
