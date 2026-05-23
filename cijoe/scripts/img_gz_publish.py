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
path. zstd's flash-time decompression-speed advantage matters
for operator-supplied target images (which :mod:`bty.flash`
still accepts in any of .img.{zst,xz,gz,bz2}); for the
appliance itself, gzip's universal flasher / OS / tooling
support wins over zstd's marginal speed advantage on a one-
shot setup.

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

    # Splice -v<version> before .img.gz so the published filename
    # self-identifies (e.g. bty-server-x86_64-v0.25.5.img.gz). Mirrors
    # usb_iso_build.py's PUBLISH_BASENAME_FMT convention.
    bty_version = _read_bty_version(Path.cwd().parent)
    if gz_path.name.endswith(".img.gz"):
        versioned = gz_path.name[: -len(".img.gz")] + f"-v{bty_version}.img.gz"
        gz_path = gz_path.with_name(versioned)
        log.info(f"versioned gz_path: {gz_path}")

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

    # ``gzip -<level> -c`` writes to stdout. Single-stream output is
    # what every flasher / OS tooling handles uniformly.
    log.info(f"Compressing {raw_path} -> {gz_path} (gzip -{level})")
    err, _ = cijoe.run_local(f"sh -c 'gzip -{level} -c {raw_path} > {gz_path}'")
    if err:
        log.error("Failed gzip-compressing raw image")
        return err

    # ``cd`` first so the sidecar records the BASENAME, not the
    # absolute build-host path -- otherwise an operator's
    # ``sha256sum -c bty-server-x86_64.img.gz.sha256`` looks for a
    # nonexistent ``/home/runner/.../bty-...img.gz`` and fails.
    # Matches the netboot (live_build) / usb (usb_iso_build) / rpi
    # convention.
    err, _ = cijoe.run_local(
        f"sh -c 'cd {gz_path.parent} && sha256sum {gz_path.name} > {gz_path.name}.sha256'"
    )
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


def _read_bty_version(repo_root: Path) -> str:
    """Read the bty-lab version from the repo's top-level pyproject.toml.

    Mirrors :func:`usb_iso_build._read_bty_version`. Kept as a small
    local duplicate rather than a shared helper module so the cijoe
    scripts stay drop-in-runnable without a sys.path tweak.
    """
    pyproject = repo_root / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")
