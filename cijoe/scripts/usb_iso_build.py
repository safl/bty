"""
Build the bty USB live env (iso-hybrid) via live-build
=======================================================

Drives Debian's live-build to produce a hybrid ISO image that boots
both from CD media and from a USB stick (BIOS + UEFI). Reuses the
same live-build chroot config tree that ``live_build`` uses for the
network-flash artifacts; the only difference is the binary-images
target (``iso-hybrid`` vs ``netboot``) and the bootloader selection.

Workflow:

1. Copy ``bty-media/live-build/`` (the live-build config tree) into
   a fresh ``cijoe/_build/usb-iso/`` working dir.
2. Re-run ``./auto/config`` with ``--binary-images iso-hybrid`` and
   ``--bootloaders syslinux,grub-efi`` to override the netboot
   defaults baked into the script for the ``live-x86`` variant.
   ``auto/config`` forwards extra args to its trailing ``lb config
   noauto ... "${@}"``, so later flags override earlier ones.
3. Run ``sudo lb clean --all`` (idempotency) then ``sudo lb build``.
   live-build needs root for chroot operations; the build host (CI
   runner or local dev) must have passwordless sudo.
4. Publish the resulting hybrid ISO to ``publish.dir`` from the
   cijoe config, renamed to ``bty-usb-x86_64.iso``.
5. Write a sha256 manifest covering the ISO.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "usb-iso"``.

Skipped for any variant whose role isn't ``usb``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME = "bty-usb-x86_64.iso"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    role = variant.split("-")[0]
    if role != "usb":
        log.info(f"Skipping usb_iso_build (variant={variant!r}; only the 'usb' role runs lb iso-hybrid)")
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("bty-usb-x86_64-iso")
    if not image:
        log.error("missing system-imaging.images.bty-usb-x86_64-iso in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.bty-usb-x86_64-iso.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = cijoe_dir / "_build" / "usb-iso"
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

    # Re-run auto/config with iso-hybrid overrides. The script's
    # trailing "${@}" forwards the extras into ``lb config noauto``,
    # and lb config processes args left-to-right with last-wins
    # semantics for repeated options.
    log.info(f"Reconfiguring live-build for iso-hybrid in {build_dir}")
    err, _ = cijoe.run_local(
        f"sh -c 'cd {build_dir} && sudo ./auto/config "
        "--binary-images iso-hybrid "
        "--bootloaders syslinux,grub-efi'"
    )
    if err:
        log.error("auto/config (iso-hybrid override) failed")
        return err

    log.info(f"Running lb build in {build_dir}")
    err, _ = cijoe.run_local(f"sh -c 'cd {build_dir} && sudo lb clean --all && sudo lb build'")
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    # Locate the artifact. live-build's iso-hybrid output naming has
    # drifted between releases: historically ``binary.hybrid.iso``,
    # later ``live-image-amd64.hybrid.iso``. Recursive glob picks up
    # whichever it ended up as. Filter chroot/ matches to skip cache
    # copies lb leaves behind.
    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    # Dump the build dir for diagnostics; turns out invaluable when
    # live-build's output layout changes again.
    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    isos = sorted(p for p in build_dir.rglob("*.hybrid.iso") if _outside_chroot(p))
    if not isos:
        # Fallback for older / non-hybrid output names.
        isos = sorted(p for p in build_dir.rglob("live-image-*.iso") if _outside_chroot(p))
    if not isos:
        log.error(f"no hybrid ISO under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name '*.iso' 2>/dev/null")
        return errno.ENOENT
    iso = isos[0]

    # Publish under the user's uid/gid so subsequent steps don't
    # need privileges. ISO is owned by root (lb wrote it under sudo).
    uid, gid = os.geteuid(), os.getegid()
    dst = publish_dir / PUBLISH_BASENAME
    err, _ = cijoe.run_local(f"sudo cp {iso} {dst}")
    if err:
        log.error(f"failed to publish {iso} -> {dst}")
        return err
    cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
    log.info(f"published {dst}")

    sha256_path = publish_dir / "bty-usb-x86_64-iso.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {PUBLISH_BASENAME} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {dst}")

    return 0
