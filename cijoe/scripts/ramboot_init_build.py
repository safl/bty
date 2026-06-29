"""
Build the bty ramboot-init live env via live-build
==================================================

Companion to ``live_build`` (which produces the netboot-pc trio).
This script targets the ``ramboot-init`` variant: a slim kernel +
initrd used by bty's ``ramboot`` boot mode to mount a catalog
image over NBD (served by ``nbdmux``) and pivot into it with an
overlayfs over tmpfs for writes.

Same live-build infrastructure as ``live_build``; differences:

* ``BTY_VARIANT=ramboot-init`` in the build env (set by the cijoe
  task via the variant config), which makes ``auto/config`` pick
  the netboot binary-images shape + the ``boot=ramboot`` kernel
  cmdline.
* Publishes only ``vmlinuz`` + ``initrd``. The ``filesystem.squashfs``
  live-build still builds is dropped here: the ramboot ``/init``
  driver never reaches live-boot's squashfs-mount stage, so the
  squashfs would just bloat the artifact set + the operator's
  ``/boot/`` upload.
* Different publish-basename family:
  ``bty-ramboot-init-x86_64-v<version>.{vmlinuz,initrd,sha256}``.
* Different build scratch dir: ``cijoe/_build/ramboot-init/`` so
  netboot-pc and ramboot-init can be baked in the same checkout
  without stomping each other.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "ramboot-init"``.

Skipped for any variant other than ``ramboot-init``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

# Reuse the version reader from the USB iso build script. Same
# pyproject.toml lookup, same placeholder convention.
from usb_iso_build import _read_bty_version

PUBLISH_BASENAME_FMTS = (
    "bty-ramboot-init-x86_64-v{version}.vmlinuz",
    "bty-ramboot-init-x86_64-v{version}.initrd",
)


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != "ramboot-init":
        log.info(f"Skipping ramboot_init_build (variant={variant!r}; only 'ramboot-init' runs)")
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("bty-ramboot-init-x86_64")
    if not image:
        log.error("missing system-imaging.images.bty-ramboot-init-x86_64 in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.bty-ramboot-init-x86_64.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = cijoe_dir / "_build" / "ramboot-init"
    if build_dir.exists():
        # ``lb`` writes a chroot tree owned by root; rm needs sudo.
        err, _ = cijoe.run_local(f"sudo rm -rf {build_dir}")
        if err:
            log.error(f"failed to remove stale build dir {build_dir}")
            return err
    build_dir.mkdir(parents=True)

    # Copy the live-build config tree into the working dir.
    config_src = bty_media / "live-build"
    if not config_src.exists():
        log.error(f"live-build config tree missing: {config_src}")
        return errno.ENOENT
    for entry in config_src.iterdir():
        dest = build_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, symlinks=True)
        else:
            shutil.copy2(entry, dest)

    # Stamp the bty version into every ``__BTY_VERSION__`` placeholder
    # in the copied tree. Same flow as live_build.py.
    bty_version = _read_bty_version(cijoe_dir)
    publish_basenames = tuple(fmt.format(version=bty_version) for fmt in PUBLISH_BASENAME_FMTS)
    sha256_basename = f"bty-ramboot-init-x86_64-v{bty_version}.sha256"
    log.info(f"Stamping bty version {bty_version} into live-build tree")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __BTY_VERSION__ {build_dir} | "
        f"xargs --no-run-if-empty sed -i s/__BTY_VERSION__/{bty_version}/g'"
    )
    if err:
        log.error("__BTY_VERSION__ substitution failed")
        return err

    log.info(f"Running lb build in {build_dir}")
    # BTY_VARIANT=ramboot-init triggers the ramboot dispatch in
    # bty-media/live-build/auto/config. ``sudo env`` because sudo
    # strips env by default.
    err, _ = cijoe.run_local(
        f"sh -c 'cd {build_dir} && sudo env BTY_VARIANT=ramboot-init lb clean --all && "
        f"sudo env BTY_VARIANT=ramboot-init lb build'"
    )
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    kernels = sorted(p for p in build_dir.rglob("vmlinuz*") if _outside_chroot(p))
    initrds = sorted(p for p in build_dir.rglob("initrd.img*") if _outside_chroot(p))

    if not kernels:
        log.error(f"no kernel matching vmlinuz* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'vmlinuz*' 2>/dev/null")
        return errno.ENOENT
    if not initrds:
        log.error(f"no initrd matching initrd.img* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'initrd.img*' 2>/dev/null")
        return errno.ENOENT

    # The squashfs that lb builds is intentionally NOT published.
    # The ramboot ``/init`` driver pivots into the nbd-mounted root
    # before the live-boot squashfs-mount stage runs, so the file
    # would only inflate the artifact set + the operator's /boot/
    # upload (~hundreds of MiB) for no boot-time benefit.

    publish_map = (
        (kernels[0], publish_dir / publish_basenames[0]),
        (initrds[0], publish_dir / publish_basenames[1]),
    )

    uid, gid = os.geteuid(), os.getegid()
    for src, dst in publish_map:
        err, _ = cijoe.run_local(f"sudo cp {src} {dst}")
        if err:
            log.error(f"failed to publish {src} -> {dst}")
            return err
        cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
        log.info(f"published {dst}")

    sha256_path = publish_dir / sha256_basename
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {' '.join(publish_basenames)} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {publish_dir}/bty-ramboot-init-x86_64.*")

    return 0
