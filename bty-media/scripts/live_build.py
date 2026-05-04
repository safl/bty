"""
Build the bty network-flash live env via live-build
====================================================

Drives Debian's live-build to produce kernel + initrd + squashfs
artifacts that bty-server hosts over HTTP for PXE clients to chain
into. Structurally different from ``diskimage_build`` (which bakes a
.img.zst via QEMU + cloud-init) - live-build runs debootstrap,
mksquashfs, and mkinitramfs directly on the build host, no QEMU.

Workflow:

1. Copy ``bty-media/live-build/`` (the live-build config tree) into
   a fresh ``_build/live/`` working dir.
2. Run ``sudo lb clean --all`` (idempotency) then ``sudo lb build``.
   live-build needs root for chroot operations; the build host (CI
   runner or local dev) must have passwordless sudo.
3. Publish ``binary/live/{vmlinuz,initrd.img,filesystem.squashfs}``
   to the ``publish.dir`` from the cijoe config, renamed to
   ``bty-live-x86_64.{vmlinuz,initrd,squashfs}``.
4. Write a single sha256 manifest covering all three artifacts.

Skipped for any variant other than ``live``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAMES = (
    "bty-live-x86_64.vmlinuz",
    "bty-live-x86_64.initrd",
    "bty-live-x86_64.squashfs",
)


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    bty_media = Path.cwd()

    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != "live":
        log.info(f"Skipping live_build (variant={variant!r}; only 'live' runs lb build)")
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("bty-live-x86_64")
    if not image:
        log.error("missing system-imaging.images.bty-live-x86_64 in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.bty-live-x86_64.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = bty_media / "_build" / "live"
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

    log.info(f"Running lb build in {build_dir}")
    err, _ = cijoe.run_local(f"sh -c 'cd {build_dir} && sudo lb clean --all && sudo lb build'")
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    # Publish.
    binary_live = build_dir / "binary" / "live"
    if not binary_live.exists():
        log.error(f"expected live-build output dir missing: {binary_live}")
        return errno.ENOENT

    kernels = sorted(binary_live.glob("vmlinuz*"))
    initrds = sorted(binary_live.glob("initrd.img*"))
    squashfs = binary_live / "filesystem.squashfs"

    if not kernels:
        log.error(f"no kernel matching vmlinuz* under {binary_live}")
        return errno.ENOENT
    if not initrds:
        log.error(f"no initrd matching initrd.img* under {binary_live}")
        return errno.ENOENT
    if not squashfs.exists():
        log.error(f"missing squashfs at {squashfs}")
        return errno.ENOENT

    publish_map = (
        (kernels[0], publish_dir / PUBLISH_BASENAMES[0]),
        (initrds[0], publish_dir / PUBLISH_BASENAMES[1]),
        (squashfs, publish_dir / PUBLISH_BASENAMES[2]),
    )

    # The artifacts are owned by root (live-build wrote them under sudo);
    # use ``sudo cp`` then ``sudo chown`` to land them under the user's
    # publish dir with the user's uid/gid so subsequent steps don't need
    # privileges.
    uid, gid = os.geteuid(), os.getegid()
    for src, dst in publish_map:
        err, _ = cijoe.run_local(f"sudo cp {src} {dst}")
        if err:
            log.error(f"failed to publish {src} -> {dst}")
            return err
        cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
        log.info(f"published {dst}")

    sha256_path = publish_dir / "bty-live-x86_64.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {' '.join(PUBLISH_BASENAMES)} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {publish_dir}/bty-live-x86_64.*")

    return 0
