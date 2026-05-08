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

# Pre-allocate this much trailing space inside the cooked ISO for an
# exFAT partition labelled BTY_IMAGES. The legacy ``usb-x86``
# cloud-init bake carved a ``BTY_IMAGES`` exFAT at the same path; this
# is the live-build equivalent. Operators dd the ISO to a stick, drop
# ``*.img.zst`` files into the writable exFAT partition from any host
# OS, then boot. ``bty-grow-images-partition.service`` (M19 phase 4)
# extends this partition to fill the rest of the stick on first boot.
TRAILING_EXFAT_GIB = 4


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    role = variant.split("-")[0]
    if role != "usb":
        log.info(
            f"Skipping usb_iso_build (variant={variant!r}; only the 'usb' role runs lb iso-hybrid)"
        )
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

    # Drive auto/config into iso-hybrid mode via the ``BTY_USB_ISO``
    # env var (``BTY_USB_ISO=1`` selects iso-hybrid + syslinux,grub-efi
    # + ``bty.mode=interactive`` on the kernel cmdline; unset selects
    # netboot for live-x86). The env var has to be set in the
    # invocation environment of every ``lb`` call, because ``lb build``
    # re-runs ``lb config`` (which re-runs ``auto/config``) during its
    # own setup; flag-based overrides at the initial config call get
    # clobbered by that re-run.
    #
    # ``bty.mode=interactive`` fires ``bty-tui-on-tty1.service`` (its
    # ``ConditionKernelCommandLine`` is keyed on this), which is the
    # same unit the PXE-tui flow uses. With no ``bty.server`` /
    # ``bty.mac`` on the cmdline the wrapper script forwards no flags
    # and ``bty-tui`` falls back to scanning the local image-root -
    # the offline USB-boot mode (M19 phase 2).
    # ``bty-flash-on-boot.service`` short-circuits cleanly when it
    # sees ``bty.mode=interactive``, so the two services don't race
    # over tty1.
    #
    # ``sudo env`` is used (instead of ``sudo`` with shell variable
    # assignment) because sudo strips environment by default; ``env``
    # ensures BTY_USB_ISO is in the invoked process's environment
    # under root rather than the caller's.
    log.info(f"Running lb build in {build_dir} (BTY_USB_ISO=1)")
    err, _ = cijoe.run_local(
        f"sh -c 'cd {build_dir} && "
        "sudo env BTY_USB_ISO=1 lb clean --all && "
        "sudo env BTY_USB_ISO=1 lb build'"
    )
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

    # Extend the published ISO with a trailing exFAT partition so the
    # cooked image is dd-ready WITH a writable image-catalog area.
    # The hybrid ISO carries a GPT (with a protective MBR); we just
    # append a partition entry, the front of the file stays byte-
    # identical so the boot path is unchanged. dd / Etcher / Rufus
    # all do byte-for-byte writes and handle the larger artifact.
    err = _extend_with_exfat(cijoe, dst)
    if err:
        return err

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


def _extend_with_exfat(cijoe, iso_path: Path) -> int:
    """Append a trailing exFAT partition labelled BTY_IMAGES to ``iso_path``.

    Steps:

    1. ``truncate -s +<N>G`` extends the file by ``TRAILING_EXFAT_GIB``
       gigabytes; the new bytes are sparse zeros until exFAT writes to
       them.
    2. ``sgdisk --move-second-header`` rewrites the GPT secondary
       header at the new EOF (live-build's ISO has it at the original
       end; without this sgdisk would refuse to add a partition past
       the secondary header).
    3. ``sgdisk --new=0:0:0`` adds a partition spanning the freshly-
       added space (typecode 0700 = "Microsoft basic data", the right
       code for exFAT/FAT/NTFS), labelled ``BTY_IMAGES`` to match
       what the legacy cloud-init bake used.
    4. ``losetup -fP`` attaches the file as a loopback block device
       and scans the partition table so ``${LOOP}p<N>`` exists.
    5. ``mkfs.exfat`` formats the new partition.
    6. ``losetup -d`` detaches.

    The new partition number is whatever sgdisk picked (typically 3
    after live-build's existing data + ESP partitions); we read it
    back from ``sgdisk --print``.
    """
    log.info(f"Extending {iso_path} with +{TRAILING_EXFAT_GIB} GiB BTY_IMAGES exFAT")

    err, _ = cijoe.run_local(f"truncate -s +{TRAILING_EXFAT_GIB}G {iso_path}")
    if err:
        log.error(f"truncate +{TRAILING_EXFAT_GIB}G failed on {iso_path}")
        return err

    err, _ = cijoe.run_local(f"sudo sgdisk --move-second-header {iso_path}")
    if err:
        log.error("sgdisk --move-second-header failed")
        return err

    err, _ = cijoe.run_local(
        f"sudo sgdisk --new=0:0:0 --typecode=0:0700 --change-name=0:BTY_IMAGES {iso_path}"
    )
    if err:
        log.error("sgdisk --new BTY_IMAGES failed")
        return err

    # Find which partition number sgdisk assigned. ``sgdisk --print``
    # output has columns: Number Start End Size Code Name. Match by
    # name so we don't depend on hybrid-ISO partition layout.
    err, out = cijoe.run_local(f"sudo sgdisk --print {iso_path}")
    if err:
        log.error("sgdisk --print failed")
        return err
    part_num = None
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[-1] == "BTY_IMAGES" and fields[0].isdigit():
            part_num = fields[0]
            break
    if part_num is None:
        log.error(f"could not locate BTY_IMAGES partition in sgdisk --print output:\n{out}")
        return errno.EIO
    log.info(f"BTY_IMAGES is partition #{part_num}")

    err, out = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error(f"losetup -fP {iso_path} failed")
        return err
    loop = out.strip().splitlines()[-1].strip()
    if not loop.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO

    # Loop device partitions follow the ``loopNpM`` naming
    # (no nvme-style boundary case here).
    part_dev = f"{loop}p{part_num}"
    cijoe.run_local("sudo udevadm settle")
    err, _ = cijoe.run_local(f"sudo mkfs.exfat -L BTY_IMAGES {part_dev}")
    if err:
        cijoe.run_local(f"sudo losetup -d {loop}")
        log.error(f"mkfs.exfat {part_dev} failed")
        return err

    err, _ = cijoe.run_local(f"sudo losetup -d {loop}")
    if err:
        log.error(f"losetup -d {loop} failed")
        return err

    log.info(f"Extended {iso_path} with BTY_IMAGES exFAT partition (p{part_num})")
    return 0
