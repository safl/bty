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
5. Compress the cooked ISO with ``gzip -9`` to get under GitHub's
   2 GiB per-release-asset upload limit. gzip is chosen over xz
   because Etcher's bundled xz decompressor fails on our output
   regardless of how the .iso.xz is shaped (single-stream,
   single-block, lower preset, none of it helps); gzip is
   universally supported by Etcher / Rufus / Raspberry Pi Imager
   / dd. Cost: ~50-100 MB larger output, no operator-side
   downside.
6. Write a sha256 manifest covering the .iso.gz.

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
PUBLISH_GZ_BASENAME = "bty-usb-x86_64.iso.gz"

# Pre-allocate this much trailing space inside the cooked ISO for
# an exFAT partition labelled BTY_IMAGES. Operators ``dd`` the
# cooked artifact to a stick (Etcher / RPi Imager / Rufus DD-mode
# read .iso.gz natively, no decompress step needed), drop
# ``*.img.zst`` files into the writable exFAT partition from any
# host OS, then boot.
#
# Sized at 4 GiB to keep the dd-to-stick step fast: the .iso file
# is mostly the BTY_IMAGES region, so each pre-allocated GiB is a
# GiB of bytes the operator's host has to actually write to the
# stick (BalenaEtcher / dd / Rufus do not sparsify -- the xz
# decompressor produces real zero bytes that get streamed to USB
# at the stick's full write speed). 4 GiB writes in ~50-100 sec
# on a typical USB-3 stick; the previous 14 GiB target took ~5
# minutes per stick and operators flash sticks frequently.
#
# Why 4 GiB specifically: the dominant bty use case is flashing
# the bty-server appliance from a freshly written stick. A
# zstd-compressed bty-server image is ~1.0-1.5 GiB; 4 GiB leaves
# room for the server image plus 1-2 additional images (e.g. a
# workstation variant) with comfortable headroom. Operators who
# need more can grow the partition on their host with gparted
# after dd-ing the stick -- no need to rebuild the .iso. For the
# bty-server flash use case this is rarely needed.
#
# bty-server first-boot grows its rootfs to fill the operator's
# real disk via ``bty-grow-rootfs.service``. The bty-usb stick
# is intentionally "what you dd is what you get": the operator
# may drop image files onto BTY_IMAGES before ever booting the
# stick, so we can't depend on a first-boot grow step.
TRAILING_EXFAT_GIB = 4

# Compress the cooked ISO with gzip. We tried xz first (zstd lacks
# GUI flasher support) but Etcher's bundled xz decompressor failed
# on our output even after dropping ``-T0`` for single-stream /
# single-block layout, and even after lowering preset levels. The
# specific error -- "if a compressed image, please check that the
# archive is not corrupted" -- fires regardless of how the .iso.xz
# is shaped, so xz is effectively unsupported by Etcher in our
# environment.
#
# Gzip is universally supported by Etcher / Rufus / Raspberry Pi
# Imager / dd / Windows / macOS -- there's no Electron-bundled JS
# gzip impl that misbehaves the way the xz one does. Trade-off vs
# xz on our 4.4 GiB sparse-zero file: gzip output is ~50-100 MB
# larger (~150 MB vs ~80 MB) but compatibility approaches 100%.
# Bake time is comparable (~1-2 min on a CI runner).
GZ_FLAGS = ["-9"]


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

    # Stamp the bty version into every ``__BTY_VERSION__`` placeholder
    # in the copied tree before ``lb build`` runs. Files that pick up
    # the stamp: ``auto/config`` (kernel cmdline), the binary-stage
    # bootloader hook (syslinux + grub menu titles), ``/etc/issue``
    # (login banner), ``/etc/motd`` (post-login), and
    # ``/etc/profile.d/bty-version.sh`` (interactive shell). Operators
    # see the version in at least one of these at every boot moment
    # -- bootloader menu, kernel boot, login, shell -- so the cooked
    # stick can always be matched back to a release.
    bty_version = _read_bty_version(cijoe_dir)
    log.info(f"Stamping bty version {bty_version} into live-build tree")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __BTY_VERSION__ {build_dir} | "
        f"xargs --no-run-if-empty sed -i s/__BTY_VERSION__/{bty_version}/g'"
    )
    if err:
        log.error("__BTY_VERSION__ substitution failed")
        return err

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

    # Verify the binary-stage hook actually ran + the bootloader
    # menus are suppressed. Catches the "hook silently doesn't
    # execute" class of bugs (the dir-mismatch saga that bit
    # v0.5.2..v0.5.9 before the hook was moved from
    # ``config/hooks/binary/`` to ``config/hooks/normal/``).
    err = _verify_bootloader_suppression(cijoe, build_dir)
    if err:
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

    # Linux-side post-bake verification. Catches structural
    # regressions in the cooked ISO (partition count / types /
    # overlap / BTY_IMAGES label / exFAT mountability) before
    # we waste CI cycles on the gzip step. Necessary but not
    # sufficient -- doesn't catch host-OS handler bugs (e.g.
    # Etcher's xz decompressor through v0.4.1-v0.5.3); those
    # need hardware verification per
    # ``feedback_verify_flasher_compat`` discipline.
    err = _verify_iso(cijoe, dst)
    if err:
        return err

    # Compress to .iso.gz. The raw 4.4 GiB ISO exceeds GitHub's 2 GiB
    # per-release-asset upload limit; gzip on our zero-heavy file
    # (4 GiB sparse exFAT region) brings it to a few hundred MiB.
    # Operator UX: Etcher / RPi Imager / Rufus DD-mode read .iso.gz
    # directly (no decompress step). For CLI:
    #   gunzip -d --stdout bty-usb-x86_64.iso.gz | sudo dd of=/dev/sdX bs=4M
    gz_dst = publish_dir / PUBLISH_GZ_BASENAME
    gz_args = " ".join(GZ_FLAGS)
    log.info(f"Compressing {dst} -> {gz_dst} (gzip {gz_args})")
    # gzip writes ``<dst>.gz`` and removes ``<dst>`` on success;
    # ``-f`` overwrites any pre-existing ``<dst>.gz``.
    err, _ = cijoe.run_local(f"gzip {gz_args} -f {dst}")
    if err:
        log.error(f"gzip {gz_args} {dst} failed")
        return err

    sha256_path = publish_dir / "bty-usb-x86_64-iso-gz.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {PUBLISH_GZ_BASENAME} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {gz_dst}")

    return 0


def _verify_bootloader_suppression(cijoe, build_dir: Path) -> int:
    """Assert the binary-stage hook ran + bootloader menus are
    suppressed in the cooked binary tree.

    Catches the "hook silently doesn't execute" class of failures
    (v0.5.2..v0.5.9 saga: hook lived at the wrong path, never ran,
    every "fix the bootloader menu" iteration was a no-op).
    Runs against ``_build/usb-x86/binary/`` after ``lb build``
    completes so we fail the bake locally instead of waiting for
    a hardware test to surface the issue.

    Checks (each fails the bake with a specific error message):

    1. ``binary/.bty-bootloader-hook-ran`` sentinel exists. Hook
       writes this on entry; missing means live-build didn't
       discover the hook (path / suffix wrong) or the hook
       errored before reaching ``touch``.
    2. No ``gfxboot.c32`` / ``vesamenu.c32`` / ``bootlogo`` files
       under ``binary/``. The hook deletes them; presence means
       the deletion didn't happen.
    3. ``binary/isolinux/isolinux.cfg`` (if present) has
       ``timeout 1`` (BIOS path). Default is ``timeout 0`` =
       wait-forever in syslinux.
    4. ``binary/boot/grub/grub.cfg`` (if present) has
       ``set timeout=0`` and ``set timeout_style=hidden`` (UEFI
       path).
    """
    binary_dir = build_dir / "binary"

    # 1. Sentinel.
    sentinel = binary_dir / ".bty-bootloader-hook-ran"
    err, _ = cijoe.run_local(f"sudo test -f {sentinel}")
    if err:
        log.error(
            f"BOOTLOADER VERIFY: hook sentinel missing ({sentinel}); "
            "the binary-stage hook didn't execute. Check that the "
            "hook lives at ``config/hooks/normal/*.binary`` (NOT "
            "``config/hooks/binary/...``)."
        )
        return errno.EIO

    # 2. No graphical-menu binaries left.
    err, state = cijoe.run_local(
        f"sudo find {binary_dir} -type f "
        r"\( -name 'gfxboot.c32' -o -name 'vesamenu.c32' -o -name 'bootlogo*' \) "
        "2>/dev/null"
    )
    leftovers = state.output().strip() if not err else ""
    if leftovers:
        log.error(f"BOOTLOADER VERIFY: graphical menu binaries not deleted:\n{leftovers}")
        return errno.EIO

    # 3. isolinux.cfg timeout.
    iso_cfg = binary_dir / "isolinux" / "isolinux.cfg"
    err, _ = cijoe.run_local(f"sudo test -f {iso_cfg}")
    if not err:
        err, state = cijoe.run_local(f"sudo cat {iso_cfg}")
        body = state.output() if not err else ""
        if "timeout 0" in body.lower() or "timeout 30" in body.lower():
            log.error(
                f"BOOTLOADER VERIFY: {iso_cfg} still has a non-suppressed "
                f"timeout (lines below).\n{body[:500]}"
            )
            return errno.EIO

    # 4. grub.cfg suppression.
    grub_cfg = binary_dir / "boot" / "grub" / "grub.cfg"
    err, _ = cijoe.run_local(f"sudo test -f {grub_cfg}")
    if not err:
        err, state = cijoe.run_local(f"sudo cat {grub_cfg}")
        body = state.output() if not err else ""
        if "set timeout=0" not in body:
            log.error(
                f"BOOTLOADER VERIFY: {grub_cfg} missing 'set timeout=0' "
                f"(first 500 chars):\n{body[:500]}"
            )
            return errno.EIO
        if "set timeout_style=hidden" not in body:
            log.error(
                f"BOOTLOADER VERIFY: {grub_cfg} missing "
                f"'set timeout_style=hidden' (first 500 chars):\n{body[:500]}"
            )
            return errno.EIO

    log.info("BOOTLOADER VERIFY: hook ran, gfxboot/vesamenu deleted, timeouts suppressed.")
    return 0


def _verify_iso(cijoe, iso_path: Path) -> int:
    """Linux-side post-bake structural checks on the cooked ISO.

    Catches the layout regressions we've broken before:

    - 3 partitions in the MBR (was 2 before M19 phase 7's relocation;
      regressed silently from v0.4.1 onward when xz-related churn
      moved attention away from layout testing).
    - Non-overlapping byte ranges (M19 phase 7 invariant; Windows
      enumeration breaks if violated).
    - p1 type 0 + bootable flag (live-build's iso-hybrid + isohdpfx.bin).
    - p2 type ef (EFI ESP).
    - p3 type 07 (exFAT) labeled BTY_IMAGES, mountable as exFAT on
      Linux (proves mkfs.exfat completed and the FAT/bitmap/root are
      coherent).

    Necessary but not sufficient. Doesn't catch host-OS handler
    bugs -- Etcher's xz decompressor failed for ~4 releases despite
    every Linux-side check passing. Hardware verification on a real
    flasher is still required before tagging any publish-format
    change (see ``feedback_verify_flasher_compat`` in memory).
    """
    log.info(f"Verifying cooked ISO structure: {iso_path}")

    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed during verification")
        return err
    try:
        table = json.loads(state.output())
        partitions = table["partitiontable"]["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json: {exc}")
        return errno.EIO

    if len(partitions) != 3:
        log.error(f"expected 3 partitions, found {len(partitions)}")
        return errno.EIO

    expected = [
        ("0", True, "ISO9660"),
        ("ef", False, "EFI ESP"),
        ("7", False, "BTY_IMAGES exFAT"),
    ]
    for i, (p, (etype, ebootable, name)) in enumerate(
        zip(partitions, expected, strict=True), start=1
    ):
        # Normalize: sfdisk emits MBR types as bare hex without
        # leading zeros, so "0", "00", "ef", "7", "07" are all
        # in play. Strip leading zeros for comparison.
        ptype = str(p.get("type", "")).lower().lstrip("0") or "0"
        if ptype != etype:
            log.error(f"p{i} ({name}): expected type {etype}, got {p.get('type')!r}")
            return errno.EIO
        actual_bootable = bool(p.get("bootable", False))
        if actual_bootable != ebootable:
            log.error(f"p{i} ({name}): expected bootable={ebootable}, got {actual_bootable}")
            return errno.EIO

    for i in range(len(partitions)):
        for j in range(i + 1, len(partitions)):
            pa, pb = partitions[i], partitions[j]
            a_start, a_end = pa["start"], pa["start"] + pa["size"]
            b_start, b_end = pb["start"], pb["start"] + pb["size"]
            if a_start < b_end and b_start < a_end:
                log.error(f"p{i + 1} [{a_start}..{a_end}) overlaps p{j + 1} [{b_start}..{b_end})")
                return errno.EIO

    err, state = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error("losetup -fP failed during verification")
        return err
    loop = state.output().strip().splitlines()[-1].strip()
    cijoe.run_local("sudo udevadm settle")

    # blkid recognizes the exFAT signature and reports the label
    # without needing the kernel to mount it -- crucial for CI
    # runners that ship ``exfatprogs`` (for mkfs.exfat) but lack
    # the ``exfat`` kernel module / FUSE driver. An actual ``mount
    # -t exfat`` here would fail on every GHA build despite the
    # filesystem being structurally fine.
    err, state = cijoe.run_local(f"sudo blkid -o value -s LABEL {loop}p3")
    label = state.output().strip() if not err else ""
    cijoe.run_local(f"sudo losetup -d {loop}")
    if err or label != "BTY_IMAGES":
        log.error(f"p3 label expected BTY_IMAGES, got {label!r}")
        return errno.EIO

    log.info("ISO structure OK: 3 non-overlapping partitions, p3 labeled BTY_IMAGES")
    return 0


def _read_bty_version(cijoe_dir: Path) -> str:
    """Read the bty-lab version from the repo's top-level pyproject.toml.

    The cooked live env stamps this string into the bootloader menu,
    kernel cmdline, login banner, motd, and shell-startup file so
    operators can read the version at every boot moment. Reading
    pyproject.toml directly (rather than ``importlib.metadata``)
    keeps the bake script independent of whether bty-lab is
    installed in the cijoe runner's env.
    """
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


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

    # Drop a default ``.bri`` into the exFAT partition pointing at
    # the latest bty-server release on GitHub. This is the only
    # bootstrap entry visible from a host OS browsing the BTY_IMAGES
    # partition (the rootfs ``/usr/share/bty/bri/*.bri`` siblings are
    # buried inside the squashfs and only surface inside the live
    # env). Operators can edit / delete / replace the .bri freely
    # from any host with exFAT support.
    #
    # Best-effort: if exfat mount fails on this build runner (some
    # minimal CI images lack the kernel module + fuse fallback), we
    # log a warning and ship the partition empty -- functionally the
    # same as v0.7.x baked sticks, with the rootfs-shipped descriptors
    # still merging into the catalog at runtime.
    _populate_bty_images_partition(cijoe, part_dev)

    err, _ = cijoe.run_local(f"sudo losetup -d {loop}")
    if err:
        log.error(f"losetup -d {loop} failed")
        return err

    log.info(f"Extended {iso_path} with BTY_IMAGES exFAT partition (p{part_num})")
    return 0


def _populate_bty_images_partition(cijoe, part_dev: str) -> None:
    """Mount the freshly-mkfs'd BTY_IMAGES exFAT partition and drop
    a default ``bty-server-x86_64.bri`` into it. Best-effort: a mount
    failure logs a warning and returns; the partition stays empty
    (same as pre-v0.7.9 sticks)."""
    # ``mktemp -d`` over a hardcoded path so parallel bake runs (CI
    # matrix, two operators on one box, etc.) don't collide on the
    # mountpoint. Collision would manifest as the second run mounting
    # over the first's still-mounted partition; rare but ugly.
    err, state = cijoe.run_local("mktemp -d")
    if err:
        log.warning("mktemp -d failed; BTY_IMAGES partition will ship empty")
        return
    mount_dir = state.output().strip().splitlines()[-1].strip()
    err, _ = cijoe.run_local(f"sudo mount -t exfat {part_dev} {mount_dir}")
    if err:
        log.warning(
            f"could not mount {part_dev} as exfat ({mount_dir}); "
            f"BTY_IMAGES partition will ship empty"
        )
        cijoe.run_local(f"rmdir {mount_dir} 2>/dev/null || true")
        return
    try:
        # Match the rootfs-shipped pointer at the bty-server release.
        # Same URL pattern (releases/latest/download) so a stick built
        # today still hands the operator a current image months later.
        bri_body = (
            "# bty Remote Image (.bri) descriptor.\n"
            "#\n"
            "# Drop your own .bri files alongside this one to advertise\n"
            "# remote flashable images via bty's catalog. Format is\n"
            "# minimal TOML: ``url`` is the only required field.\n"
            "# See ``bty inspect image <path>.bri`` for full syntax.\n"
            "\n"
            'name = "bty-server (x86_64, latest)"\n'
            'url = "https://github.com/safl/bty/releases/latest/download/'
            'bty-server-x86_64.img.gz"\n'
            'format = "img.gz"\n'
            'description = "Latest published bty-server appliance for x86_64"\n'
        )
        bri_path = f"{mount_dir}/bty-server-x86_64.bri"
        # ``tee`` is the simplest sudo-write idiom; here-string keeps
        # quoting straight without a temp file.
        err, _ = cijoe.run_local(f"sudo tee {bri_path} > /dev/null <<'EOF'\n{bri_body}EOF\n")
        if err:
            log.warning(f"writing {bri_path} failed; partition kept empty")
            return
        log.info(f"Wrote bootstrap .bri to BTY_IMAGES partition: {bri_path}")
    finally:
        cijoe.run_local(f"sudo umount {mount_dir}")
        cijoe.run_local(f"rmdir {mount_dir} 2>/dev/null || true")
