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
   a fresh ``cijoe/_build/usb-x86/`` working dir.
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
5. Compress the cooked ISO with ``xz -9 --extreme -T0`` to get
   under GitHub's 2 GiB per-release-asset upload limit. xz is
   chosen over zstd because Etcher / Rufus / Raspberry Pi Imager
   all decompress .xz natively (no extra step for GUI flashers);
   zstd lacks that ecosystem support today.
6. Write a sha256 manifest covering the .iso.xz.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "usb-x86"``.

Skipped for any variant other than ``usb-x86``.

Retargetable: False
"""

from __future__ import annotations

import errno
import json
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME = "bty-usb-x86_64.iso"
PUBLISH_XZ_BASENAME = "bty-usb-x86_64.iso.xz"

# Pre-allocate this much trailing space inside the cooked ISO for
# an exFAT partition labelled BTY_IMAGES. Operators ``dd`` the
# cooked artifact to a stick (Etcher / RPi Imager / Rufus DD-mode
# read .iso.xz natively, no decompress step needed), drop
# ``*.img.zst`` files into the writable exFAT partition from any
# host OS, then boot. ``bty-grow-images-partition.service`` (M19
# phase 4) extends this partition to fill the rest of the stick
# on first boot.
#
# Sized at 14 GiB so the cooked artifact fits on a 16 GB stick
# (~14.9 GiB usable): 14 GiB BTY_IMAGES + ~400 MB ISO front-matter
# = ~14.4 GiB total. exFAT overhead (~50 MiB) leaves ~13.95 GiB
# usable inside BTY_IMAGES -- room for a 6 GiB server image (the
# default DISK_SIZE in diskimage_build.py post-shrink) with ~8 GiB
# slack for additional images. 32 GB+ sticks unaffected:
# ``bty-grow-images-partition.service`` (M19 phase 4) still
# expands BTY_IMAGES to fill on first boot. The compressed
# .iso.xz is barely affected by this size (the trailing space is
# sparse zeros that xz crushes to a few MB).
TRAILING_EXFAT_GIB = 14

# Compress the cooked ISO with xz instead of zstd: Etcher / Rufus /
# RPi Imager all decompress .xz natively but NOT .zstd, so .iso.xz
# lets operators flash directly without a manual decompress step.
# Compression ratio on our zero-heavy file (4 GiB sparse exFAT
# region) is comparable to zstd-19 or slightly better; xz's
# decompression is slower (~50-100 MB/s) but that's fine for a
# one-shot write. ``-9 --extreme`` is max compression; ``-T0``
# uses all build-host cores. Compress takes ~1-2 min on a CI
# runner; the operator-side savings (no decompress step) compound
# every download.
XZ_FLAGS = ["-9", "--extreme", "-T0"]


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != "usb-x86":
        log.info(f"Skipping usb_iso_build (variant={variant!r}; only 'usb-x86' runs lb iso-hybrid)")
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

    build_dir = cijoe_dir / "_build" / "usb-x86"
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

    # Compress to .iso.xz. The raw 4.4 GiB ISO exceeds GitHub's 2 GiB
    # per-release-asset upload limit; most of the trailing exFAT is
    # zero-fill so xz -9 --extreme brings it to a few hundred MiB.
    # Operator UX: Etcher / RPi Imager / Rufus DD-mode read .iso.xz
    # directly (no decompress step). For CLI:
    #   xz -d --stdout bty-usb-x86_64.iso.xz | sudo dd of=/dev/sdX bs=4M
    xz_dst = publish_dir / PUBLISH_XZ_BASENAME
    xz_args = " ".join(XZ_FLAGS)
    log.info(f"Compressing {dst} -> {xz_dst} (xz {xz_args})")
    # xz writes ``<dst>.xz`` and removes ``<dst>`` on success (no
    # ``--keep``); ``-f`` overwrites any pre-existing ``<dst>.xz``.
    err, _ = cijoe.run_local(f"xz {xz_args} -f {dst}")
    if err:
        log.error(f"xz {xz_args} {dst} failed")
        return err

    sha256_path = publish_dir / "bty-usb-x86_64-iso-xz.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {PUBLISH_XZ_BASENAME} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {xz_dst}")

    return 0


def _extend_with_exfat(cijoe, iso_path: Path) -> int:
    """Relocate the EFI partition out of the iso-hybrid overlap, then
    append a trailing exFAT partition labelled BTY_IMAGES (M19 phase 7).

    live-build's iso-hybrid output puts the EFI partition entry
    *inside* the ISO9660 partition's byte range (the EFI FAT image is
    embedded in the ISO9660 stream, and the MBR partition entry just
    points at where it lives). Linux handles overlapping MBR entries
    fine, but Windows refuses to enumerate partitions past the
    overlap, so the BTY_IMAGES partition we append is invisible to
    Windows operators.

    Fix: copy the EFI FAT bytes to a non-overlapping location after
    the ISO9660 partition, then rewrite the MBR with three
    non-overlapping partitions:

      - p1: ISO9660 (covers live-build's ISO9660 portion, unchanged)
      - p2: EFI ESP, relocated to the byte range right after p1
      - p3: BTY_IMAGES exFAT, fills the rest of the file

    The El Torito catalog inside the ISO9660 still has its embedded
    EFI image for CD-style UEFI boot; the relocated MBR partition
    entry handles USB-style UEFI boot. BIOS boot via ``isohdpfx.bin``
    in MBR sectors 0..432 is untouched (sfdisk only edits the
    partition-table area at offsets 446..510).

    Workflow:

    1. ``truncate -s +<N>G`` extends the file with sparse zeros.
    2. Read the existing MBR via ``sfdisk --json``; locate the
       ISO9660 (type 0) and EFI (type ef) entries.
    3. ``dd`` the EFI FAT bytes from the current overlapping
       location to a non-overlapping position right after the
       ISO9660 partition (8-sector aligned).
    4. Rewrite the MBR partition table via ``sfdisk`` stdin form so
       all three entries land at non-overlapping byte ranges in
       a single atomic operation. Bootable flag preserved on p1.
    5. ``losetup -fP`` + ``mkfs.exfat -L BTY_IMAGES`` on p3.
    6. ``losetup -d``.
    """
    log.info(f"Extending {iso_path} with +{TRAILING_EXFAT_GIB} GiB BTY_IMAGES exFAT")
    log.info("Layout: ISO9660 + relocated EFI + BTY_IMAGES (non-overlapping for Windows)")

    err, _ = cijoe.run_local(f"truncate -s +{TRAILING_EXFAT_GIB}G {iso_path}")
    if err:
        log.error(f"truncate +{TRAILING_EXFAT_GIB}G failed on {iso_path}")
        return err

    # Read the current MBR.
    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed")
        return err
    try:
        table = json.loads(state.output())
        partitions = table["partitiontable"]["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json output: {exc}")
        return errno.EIO

    iso_part = None
    efi_part = None
    for p in partitions:
        # sfdisk emits MBR types as bare hex without leading zeros:
        # ISO9660 partition typically registers as type "0" or "00";
        # EFI System partition is "ef".
        ptype = str(p.get("type", "")).lower()
        if ptype.lstrip("0") == "" or ptype.lstrip("0") == "0":
            iso_part = p
        elif ptype == "ef":
            efi_part = p
    if iso_part is None:
        log.error("could not find ISO9660 partition (type 0) in MBR")
        return errno.EIO
    if efi_part is None:
        log.error("could not find EFI partition (type ef) in MBR")
        return errno.EIO
    log.info(f"ISO9660 at sectors {iso_part['start']}..{iso_part['start'] + iso_part['size'] - 1}")
    log.info(
        f"EFI currently at sectors {efi_part['start']}..{efi_part['start'] + efi_part['size'] - 1} "
        f"(overlapping ISO9660)"
    )

    # Compute new non-overlapping layout.
    iso_end = iso_part["start"] + iso_part["size"]  # next sector after p1
    efi_size = efi_part["size"]
    # Align to 8-sector (4 KiB) boundary.
    new_efi_start = ((iso_end + 7) // 8) * 8
    new_bty_start = ((new_efi_start + efi_size + 7) // 8) * 8

    # File size in sectors.
    err, state = cijoe.run_local(f"stat -c %s {iso_path}")
    if err:
        log.error("stat failed on iso file")
        return err
    file_bytes = int(state.output().strip())
    file_sectors = file_bytes // 512
    new_bty_size = file_sectors - new_bty_start
    if new_bty_size <= 0:
        log.error(
            f"no room for BTY_IMAGES: file_sectors={file_sectors}, new_bty_start={new_bty_start}"
        )
        return errno.EIO

    log.info(f"Relocating EFI to sectors {new_efi_start}..{new_efi_start + efi_size - 1}")
    log.info(f"BTY_IMAGES at sectors {new_bty_start}..{new_bty_start + new_bty_size - 1}")

    # Copy EFI FAT bytes from old overlapping location to new
    # non-overlapping location. The new region is currently sparse
    # zeros (truncate just extended the file); writing the FAT image
    # populates it. ``conv=notrunc`` keeps the rest of the file
    # untouched; ``conv=fsync`` flushes before sfdisk writes the new
    # MBR (defensive against reordering).
    err, _ = cijoe.run_local(
        f"sudo dd if={iso_path} of={iso_path} bs=512 "
        f"skip={efi_part['start']} seek={new_efi_start} count={efi_size} "
        f"conv=notrunc,fsync 2>&1"
    )
    if err:
        log.error("dd EFI FAT image to new location failed")
        return err

    # Rewrite the partition table with three non-overlapping entries.
    # sfdisk's stdin form replaces the entire table in one shot.
    # The ``bootable`` flag on p1 is what isohdpfx.bin's BIOS code
    # looks for; preserve it.
    sfdisk_script = iso_path.parent / "_mbr.sfdisk"
    sfdisk_script.write_text(
        f"label: dos\n"
        f"unit: sectors\n"
        f"\n"
        f"start={iso_part['start']}, size={iso_part['size']}, type=0, bootable\n"
        f"start={new_efi_start}, size={efi_size}, type=ef\n"
        f"start={new_bty_start}, size={new_bty_size}, type=07\n"
    )
    err, _ = cijoe.run_local(f"sh -c 'sudo sfdisk {iso_path} < {sfdisk_script}'")
    sfdisk_script.unlink(missing_ok=True)
    if err:
        log.error("sfdisk partition-table rewrite failed")
        return err

    part_num = "3"  # BTY_IMAGES is partition 3 in the rewritten table
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
