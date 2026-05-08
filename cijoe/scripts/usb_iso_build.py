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
2. Run ``sudo env BTY_USB_ISO=1 lb clean --all && lb build``. The
   env var drives ``auto/config`` into iso-hybrid mode (binary
   images, bootloaders, kernel cmdline appendices); ``sudo env``
   is needed because sudo strips the environment by default. The
   var must be present at every lb invocation because ``lb build``
   internally re-runs ``lb config`` (which re-invokes
   ``auto/config``).
3. Publish the resulting hybrid ISO to ``publish.dir`` from the
   cijoe config, renamed to ``bty-usb-x86_64.iso``.
4. Append a writable BTY_IMAGES exFAT partition to the trailing
   edge of the artifact (sfdisk + losetup + mkfs.exfat) so the
   single dd-able file carries both the boot path and the
   operator's image catalog.
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
import json
import logging as log
import os
import re
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME = "bty-usb-x86_64.iso"
PUBLISH_ZST_BASENAME = "bty-usb-x86_64.iso.zst"

# Pre-allocate this much trailing space inside the cooked ISO for an
# exFAT partition labelled BTY_IMAGES. The legacy ``usb-x86``
# cloud-init bake carved a ``BTY_IMAGES`` exFAT at the same path; this
# is the live-build equivalent. Operators ``zstd -d`` the artifact and
# ``dd`` to a stick (or pipe in one step), drop ``*.img.zst`` files
# into the writable exFAT partition from any host OS, then boot.
# ``bty-grow-images-partition.service`` (M19 phase 4) extends this
# partition to fill the rest of the stick on first boot.
TRAILING_EXFAT_GIB = 4

# zstd compression level for the published .iso.zst. The trailing
# exFAT is sparse zeros so the compression ratio is huge (4.4 GiB
# raw -> few hundred MiB compressed). 19 is the maximum standard
# level; matches what ``img_zst_publish`` uses for the .img.zst
# variants.
ZSTD_LEVEL = 19


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
    # The hybrid ISO is MBR-only (live-build's ``--bootloaders
    # syslinux,grub-efi`` uses ``isohdpfx.bin`` for the System Area,
    # not GPT); we append a fresh MBR partition entry via sfdisk.
    # The front of the file stays byte-identical so the boot path
    # is unchanged. dd / Etcher / Rufus all do byte-for-byte writes
    # and handle the larger artifact.
    err = _extend_with_exfat(cijoe, dst)
    if err:
        return err

    # Compress to .iso.zst. The raw 4.4 GiB ISO exceeds GitHub's 2 GiB
    # per-release-asset upload limit, and most of the trailing exFAT
    # is zero-fill so compression brings it to a few hundred MiB. The
    # operator UX matches the existing .img.zst variants:
    #   zstd -d --stdout bty-usb-x86_64.iso.zst | sudo dd of=/dev/sdX bs=4M
    zst_dst = publish_dir / PUBLISH_ZST_BASENAME
    log.info(f"Compressing {dst} -> {zst_dst} (zstd -{ZSTD_LEVEL} -T0)")
    err, _ = cijoe.run_local(f"zstd -{ZSTD_LEVEL} -T0 -f {dst} -o {zst_dst}")
    if err:
        log.error(f"zstd -{ZSTD_LEVEL} {dst} -> {zst_dst} failed")
        return err
    # Drop the uncompressed file - it's too big to ship and we have
    # the .zst now. Local devs who want the .iso for inspection can
    # ``zstd -d`` it back.
    err, _ = cijoe.run_local(f"rm -f {dst}")
    if err:
        log.error(f"failed to remove uncompressed {dst}")
        return err

    sha256_path = publish_dir / "bty-usb-x86_64-iso-zst.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {PUBLISH_ZST_BASENAME} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {zst_dst}")

    return 0


def _extend_with_exfat(cijoe, iso_path: Path) -> int:
    """Append a trailing exFAT partition labelled BTY_IMAGES to ``iso_path``.

    The live-build hybrid ISO uses ``isohdpfx.bin`` for its System
    Area, producing an MBR-only isohybrid layout (no GPT). This is
    the consequence of ``--bootloaders syslinux,grub-efi``: syslinux
    needs the legacy MBR boot path. So we work the MBR via
    ``sfdisk`` rather than ``sgdisk``.

    Steps:

    1. ``truncate -s +<N>G`` extends the file by ``TRAILING_EXFAT_GIB``
       gigabytes; the new bytes are sparse zeros until exFAT writes to
       them.
    2. ``sfdisk --append`` adds a new MBR partition entry covering the
       freshly-added space. Spec ``,,07`` -> default start, default
       size (fill remaining), type 0x07 ("NTFS/exFAT/HPFS" MBR
       partition type code).
    3. ``losetup -fP`` attaches the file as a loopback block device
       and scans the partition table so ``${LOOP}p<N>`` exists.
    4. ``mkfs.exfat -L BTY_IMAGES`` formats the new partition. The
       label is set on the FILESYSTEM (read by udev/blkid for the
       ``/dev/disk/by-label/BTY_IMAGES`` symlink); MBR has no
       partition labels of its own.
    5. ``losetup -d`` detaches.

    The new partition number is whatever sfdisk auto-assigned. MBR
    isohybrid layout from live-build is typically:
      - p1: ISO9660 data (the squashfs etc.)
      - p2: EFI System partition
      - p3: BTY_IMAGES (added here)
    We assume p3 with a sanity check via ``sfdisk --json``.
    """
    log.info(f"Extending {iso_path} with +{TRAILING_EXFAT_GIB} GiB BTY_IMAGES exFAT")

    err, _ = cijoe.run_local(f"truncate -s +{TRAILING_EXFAT_GIB}G {iso_path}")
    if err:
        log.error(f"truncate +{TRAILING_EXFAT_GIB}G failed on {iso_path}")
        return err

    # sfdisk --append reads the partition spec from stdin. ``,,07`` =
    # default start, default size (fill remaining), type 0x07.
    err, _ = cijoe.run_local(f"sh -c 'echo \",,07\" | sudo sfdisk --append {iso_path}'")
    if err:
        log.error("sfdisk --append BTY_IMAGES failed")
        return err

    # Sanity check + partition number resolution. ``sfdisk --json``
    # emits a parseable structure under ``.partitiontable.partitions``.
    # ``cijoe.run_local`` returns ``(err, CommandState)``; the captured
    # stdout/stderr is exposed via ``CommandState.output()`` (a method
    # that reads back the Tee'd output file).
    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed")
        return err
    out = state.output()
    try:
        table = json.loads(out)
        partitions = table["partitiontable"]["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json output: {exc}\n{out}")
        return errno.EIO

    # The newest entry is the one we just appended. sfdisk numbers
    # by 1-based index in the partition table. Match by partition
    # type 0x07 (we just added the only one).
    part_num = None
    for p in partitions:
        # ``type`` is the hex string e.g. "7" or "07" (sfdisk emits
        # without leading zero).
        if str(p.get("type", "")).lstrip("0").lower() == "7":
            # Extract the trailing run of digits at the end of the
            # node path. The naive "all digits anywhere" approach
            # breaks on paths like ``bty-usb-x86_64.iso3`` where the
            # ``86_64`` portion contributes spurious digits and would
            # yield part 86643 instead of 3.
            node = p.get("node", "")
            m = re.search(r"(\d+)$", node)
            if m:
                part_num = m.group(1)
                break
    if part_num is None:
        log.error(f"could not locate BTY_IMAGES (type 07) in sfdisk --json output:\n{out}")
        return errno.EIO
    log.info(f"BTY_IMAGES is partition #{part_num}")

    err, state = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error(f"losetup -fP {iso_path} failed")
        return err
    out = state.output()
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
