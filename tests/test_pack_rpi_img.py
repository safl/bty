"""Unit tests for the pure pieces of ``bty-media/scripts/pack_rpi_img.py``.

The script's filesystem side (sfdisk + losetup + mkfs.{vfat,ext4,exfat}
+ mcopy) needs root + system tools and runs in CI only as part of the
``build-usb-rpi`` job. These tests cover the pure pieces that can
silently rot between releases without anyone noticing on hardware:

* The Pi firmware ``config.txt`` invariants (arm_64bit=1,
  dtparam=pciex1 for Pi5/CM5 NVMe, kernel=vmlinuz, initramfs line)
  -- a typo in any one of these silently produces an image that
  doesn't boot on real hardware, with no CI signal until the
  release-time hardware test.
* The kernel ``cmdline.txt`` format (boot=live, console=tty1 +
  serial0, root=LABEL=BTY_LIVE, ``bty.version=`` interpolation).
* ``_locate_lb_output``'s preference for ``binary/live/`` over a
  bare rglob -- the rglob match order is filesystem-dependent, so
  a stale leftover under chroot/boot/ or binary/EFI/ could
  otherwise win silently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_pack_rpi_img() -> Any:
    """Load bty-media/scripts/pack_rpi_img.py as a module without
    requiring it to be on sys.path (it lives outside the
    ``src/bty/`` tree because it's a build-time helper, not a
    runtime module)."""
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "bty-media" / "scripts" / "pack_rpi_img.py"
    spec = importlib.util.spec_from_file_location("pack_rpi_img", script_path)
    assert spec is not None and spec.loader is not None, script_path
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pack_rpi_img"] = mod
    spec.loader.exec_module(mod)
    return mod


pack_rpi_img = _load_pack_rpi_img()


def test_config_txt_carries_pi5_invariants() -> None:
    """The Pi firmware reads config.txt from FAT32 partition 1 before
    handing off to the kernel. Three lines load-bearing for the bty
    Pi flasher:

    * ``arm_64bit=1`` puts the firmware into 64-bit mode (needed for
      arm64 kernels on CM5 / Pi5 / Pi4-64).
    * ``dtparam=pciex1`` enables the Pi5 / CM5 PCIe lane so the NVMe
      HAT enumerates -- without it the NVMe target shows up as
      "no disks" in the wizard.
    * ``kernel=vmlinuz`` points the firmware at the kernel we drop
      onto p1; the default ``kernel8.img`` would not match.
    * ``initramfs initrd.img followkernel`` tells the firmware to
      load the initrd too.
    """
    cfg = pack_rpi_img._CONFIG_TXT
    assert "arm_64bit=1" in cfg
    assert "dtparam=pciex1" in cfg
    assert "kernel=vmlinuz" in cfg
    assert "initramfs initrd.img followkernel" in cfg


def test_cmdline_format_interpolates_version_and_keeps_pi_consoles() -> None:
    """cmdline.txt is a single line consumed by the kernel verbatim.
    Format invariants the wizard depends on:

    * ``boot=live components`` so live-boot mounts the squashfs.
    * ``root=LABEL=BTY_LIVE`` so live-boot finds the squashfs
      partition by label rather than a hard-coded /dev/ path.
    * ``rootwait`` so initrd waits for the USB stick to enumerate
      before failing the mount.
    * ``console=tty1 console=serial0,115200`` -- the firmware
      overlays ``serial0`` onto the right ``ttyAMAx`` per board
      revision (Pi4 / Pi5 / CM5) so a single cmdline boots all
      supported boards. Order matters: the LAST console= is
      systemd's default stdout sink, so serial last captures the
      early-boot stream when serial is the only diagnostic path.
    * ``plymouth.enable=0`` + ``systemd.gpt_auto=0`` -- belt + braces
      against pulled-in transitive deps surfacing the splash or
      auto-mounting partitions we want bty to own.
    * The ``{version}`` placeholder is the bty release string so
      ``bty.version=`` ends up on /proc/cmdline (the live env's
      banner reads it).
    """
    cmdline = pack_rpi_img._CMDLINE_TXT_FMT.format(version="0.49.0")
    assert cmdline.startswith("boot=live components ")
    assert " root=LABEL=BTY_LIVE " in cmdline
    assert cmdline.endswith(" rootwait")
    assert " console=tty1 console=serial0,115200 " in cmdline
    assert " plymouth.enable=0 " in cmdline
    assert " systemd.gpt_auto=0 " in cmdline
    assert " bty.version=0.49.0 " in cmdline


def test_partition_constants_sane() -> None:
    """The fixed partition sizes need to add up to something the live
    env can grow into without surprise:

    * FAT32 RPIBOOT large enough to hold firmware blobs + kernel +
      initrd (raspi-firmware ~30 MiB, kernel ~60 MiB, initrd ~30 MiB).
    * exFAT BTY_IMAGES small enough at bake-time that the live env's
      bty-usb-grow.service has room to expand it to the rest of the
      stick.
    * ext4 BTY_LIVE slack big enough to hold inode metadata + the
      squashfs without running out (the script adds the squashfs's
      actual size on top of EXT4_SLACK_MIB at runtime).
    """
    assert pack_rpi_img.FAT_SIZE_MIB >= 128  # firmware + kernel + initrd
    assert pack_rpi_img.FAT_SIZE_MIB <= 512  # leave room on the stick
    assert pack_rpi_img.EXFAT_SIZE_MIB <= 64  # let bty-usb-grow grow it
    assert pack_rpi_img.EXT4_SLACK_MIB >= 32  # mkfs.ext4 inode overhead


def test_locate_lb_output_prefers_binary_live(tmp_path: Path) -> None:
    """``_locate_lb_output`` must pick the kernel + initrd + squashfs
    that live-build cooked into ``binary/live/``, NOT a stale leftover
    elsewhere (binary/EFI/, an aborted prior bake). Plain rglob would
    return whichever the filesystem listed first; the explicit
    ``binary/live/`` preference pins the right one regardless of dir
    order.
    """
    binary = tmp_path / "binary"
    (binary / "live").mkdir(parents=True)
    (binary / "EFI").mkdir(parents=True)

    # The right kernel + initrd live under binary/live/.
    right_vmlinuz = binary / "live" / "vmlinuz"
    right_vmlinuz.write_bytes(b"correct kernel")
    right_initrd = binary / "live" / "initrd.img"
    right_initrd.write_bytes(b"correct initrd")
    right_squashfs = binary / "live" / "filesystem.squashfs"
    right_squashfs.write_bytes(b"squashed rootfs")

    # Stale leftovers that rglob could otherwise prefer in arbitrary
    # filesystem order.
    (binary / "EFI" / "vmlinuz.efi").write_bytes(b"wrong kernel")
    (binary / "EFI" / "initrd-stale.img").write_bytes(b"wrong initrd")

    vmlinuz, initrd, squashfs = pack_rpi_img._locate_lb_output(binary)
    assert vmlinuz == right_vmlinuz
    assert initrd == right_initrd
    assert squashfs == right_squashfs


def test_locate_lb_output_returns_none_when_missing(tmp_path: Path) -> None:
    """If lb didn't actually emit anything, the function should
    surface that as None / None / None rather than picking up a
    random match from a sibling directory."""
    empty_binary = tmp_path / "binary"
    empty_binary.mkdir()

    vmlinuz, initrd, squashfs = pack_rpi_img._locate_lb_output(empty_binary)
    assert vmlinuz is None
    assert initrd is None
    assert squashfs is None
