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
   a fresh ``cijoe/_build/live/`` working dir.
2. Run ``sudo lb clean --all`` (idempotency) then ``sudo lb build``.
   live-build needs root for chroot operations; the build host (CI
   runner or local dev) must have passwordless sudo.
3. Publish ``binary/live/{vmlinuz,initrd.img,filesystem.squashfs}``
   to the ``publish.dir`` from the cijoe config, renamed to
   ``bty-live-x86_64.{vmlinuz,initrd,squashfs}``.
4. Write a single sha256 manifest covering all three artifacts.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "live"``.

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
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

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

    build_dir = cijoe_dir / "_build" / "live"
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

    # Locate the artefacts. live-build's netboot mode has shifted output
    # paths between versions ('binary/live/' historically; sometimes
    # tarballed; sometimes split across binary/live/ + a top-level
    # 'live-image-amd64.tar.xz'). Recursive globs find them wherever
    # they ended up. Filter ``vmlinuz*`` matches to skip the chroot/boot/
    # copy that lb leaves behind for caching.
    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    # Dump the build dir for diagnostics; turns out invaluable when
    # live-build's output layout changes again.
    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    kernels = sorted(p for p in build_dir.rglob("vmlinuz*") if _outside_chroot(p))
    initrds = sorted(p for p in build_dir.rglob("initrd.img*") if _outside_chroot(p))
    squashfses = sorted(p for p in build_dir.rglob("filesystem.squashfs") if _outside_chroot(p))

    if not kernels:
        log.error(f"no kernel matching vmlinuz* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'vmlinuz*' 2>/dev/null")
        return errno.ENOENT
    if not initrds:
        log.error(f"no initrd matching initrd.img* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'initrd.img*' 2>/dev/null")
        return errno.ENOENT
    if not squashfses:
        log.error(f"no filesystem.squashfs under {build_dir}")
        cijoe.run_local(f"sudo find {build_dir} -name '*.squashfs' 2>/dev/null")
        return errno.ENOENT
    squashfs = squashfses[0]

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
