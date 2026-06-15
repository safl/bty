# bty via bty-usbboot-rpi (Raspberry Pi flasher)

A USB-bootable arm64 image that runs the bty flash TUI on a Pi
itself. The headline use case is the **Compute Module 5 mounted
in a CM5 IO-board inside an enclosure**: with bty-usbboot-rpi the
operator no longer has to disassemble the case, set the eMMC
boot jumper, run `rpiboot` from a host PC, flash via Etcher,
and reassemble. Plug the USB stick into one of the IO-board's
USB ports, power on, run the wizard, done. The same image also
works on standalone Pi5 and Pi4 boards for ad-hoc reflashing.

Targets (when flashing from inside the bty TUI):

- **eMMC** (`/dev/mmcblk0` on the CM5 IO-board): the
  non-removable storage the OS will boot from. Headline target.
- **NVMe** (`/dev/nvme0n1` on a Pi5 / CM5 with a PCIe NVMe HAT):
  also non-removable in practice; second tier-1 target.
- SD card (`/dev/mmcblk1`): the wizard surfaces it too, but the
  operator can usually just pop the SD card out and flash it on
  another host, so it's not the workflow this tutorial is
  optimised for.

## Prerequisites

| You need | Notes |
|---|---|
| A **USB stick**, 4 GiB or larger | The image is ~600 MiB compressed. |
| A **Pi target**: CM5 on IO-board, Pi5, or Pi4 | Older Pi3 boards lack USB-boot support and aren't covered here. |
| The Pi's `BOOT_ORDER` set to try USB | CM5 default `0xf41` (NVMe, USB, SD, loop) USB-boots when NVMe is empty. Pi5 default tries SD first; set `BOOT_ORDER=0xf14` via `sudo rpi-eeprom-config --edit` or pop the SD card. Pi4 needs an EEPROM update done from any working Pi OS once. |
| A **target image** in the catalog | The default `safl/nosi` catalog ships rpios variants for arm64: `debian-13-headless`, `rpios-13-headless`, `rpios-13-desktop`, plus the Pi-flavoured nosi builds. |

## Step 1: Download the image

Each tagged release publishes the arm64 Pi flasher as a release
asset. `releases/latest/download/` always redirects to the newest
version:

```
https://github.com/safl/bty/releases/latest/download/bty-usbboot-rpi-arm64-vX.Y.Z.img.gz
https://github.com/safl/bty/releases/latest/download/bty-usbboot-rpi-arm64-vX.Y.Z.img.gz.sha256
```

Verify the sha256 before writing.

## Step 2: Write the image to a USB stick

```sh
gunzip -c bty-usbboot-rpi-arm64-vX.Y.Z.img.gz | sudo dd of=/dev/sdX bs=4M conv=fsync
sudo sync
```

Replace `/dev/sdX` with the USB stick's device (verify with
`lsblk` first; the wrong target overwrites itself happily).

## Step 3: Boot the Pi from the stick

1. Power off the target Pi.
2. Plug the USB stick into one of the Pi's USB ports (USB 3 on
   Pi4/Pi5; the IO-board's USB-A on a CM5 carrier).
3. (Pi5 / Pi4 only) make sure the BOOT_ORDER actually reaches
   USB. CM5 default works out of the box for a blank eMMC.
4. Power on.

You should see:

- The Pi rainbow splash (firmware OK).
- Kernel boot messages on HDMI + serial (UART on GPIO14/15
  if you have a USB-TTL adapter; the cmdline carries both).
- The bty TUI on tty1.

## Step 4: Flash a target

In the TUI:

1. **Select an image** from the catalog. The default catalog
   (`safl/nosi`) lists every arm64-suitable variant.
2. **Select a target disk**. The wizard shows:
   - `/dev/mmcblk0` (eMMC on a CM5 IO-board).
   - `/dev/nvme0n1` (NVMe HAT on Pi5 / CM5).
   - `/dev/mmcblk1` (the operator-removable SD card slot).
3. **Confirm** and let `bty` stream the image through.

When the flash finishes, power off, **remove the USB stick**,
and power back on. The Pi falls through the BOOT_ORDER to the
target you just flashed (eMMC or NVMe), and the freshly-imaged
OS comes up.

## Reflashing: keep USB ahead of eMMC / NVMe

Once the first flash succeeds, internal storage is bootable, and the
Pi's default boot order prefers it -- the next power-on boots the
freshly-flashed eMMC (or NVMe) instead of re-entering the USB flasher.
To reflash on demand, bias the bootloader to try USB first. Set it in
the EEPROM from any working Pi OS (or the target you just flashed):

```bash
sudo rpi-eeprom-config --edit
```

```ini
# Read right-to-left: 4=USB-MSD, 1=SD/eMMC, 6=NVMe, f=restart (loop).
# So: try USB first, then eMMC, then NVMe, then loop.
BOOT_ORDER=0xf614
# NVMe-over-PCIe only: needed on a Pi5 with a non-HAT+ adapter or on a
# CM4/CM5 carrier board. Omit it on a Pi4 (no PCIe) or if you only
# target eMMC/SD/USB.
PCIE_PROBE=1
```

`BOOT_ORDER` is the same mechanism on **Pi4, Pi5, CM4, and CM5** -- one
EEPROM bootloader, identical nibble meanings -- so the USB-first value
above works on all of them (the `6` NVMe nibble is simply inert on a
Pi4, which has no PCIe). Only `PCIE_PROBE` is hardware-specific, as
noted above.

With USB first, the flasher always wins while the stick is inserted,
and the target still boots normally once you remove it -- no jumper or
SD-card dance between flashes.

Alternatives if you'd rather not change the EEPROM (or you keep eMMC
ahead of USB, e.g. `BOOT_ORDER=0xf421`):

- Pull the USB stick before each flash run; the BOOT_ORDER
  falls through to USB only when prior targets fail.
- Hold the IO-board's `nRPIBOOT` jumper (or the dedicated
  "boot select" switch on some carriers) on power-on; the Pi
  enters firmware-recovery mode and prefers USB.

## Local verification with QEMU (developer-only)

Real Pi firmware bootflow is best verified on hardware; QEMU's
`-M raspi4b` model is incomplete and doesn't match real Pi
behaviour for USB / NVMe / PCIe enumeration. For a quick
"does the squashfs even boot" sanity check on a developer host
(extracts the lb kernel + initrd from the image's FAT partition
and boots a generic arm64 VM):

```sh
mkdir -p /tmp/rpi-extract && cd /tmp/rpi-extract
# Substitute the version you downloaded. Latest is
# https://github.com/safl/bty/releases/latest/download/bty-usbboot-rpi-arm64.img.gz
gunzip -c ~/Downloads/bty-usbboot-rpi-arm64-v*.img.gz > image.img
mcopy -i image.img@@1M ::/vmlinuz .
mcopy -i image.img@@1M ::/initrd.img .

# Stand in for an eMMC / NVMe target the wizard can flash to.
qemu-img create -f qcow2 fake-emmc.qcow2 8G

qemu-system-aarch64 \
    -M virt -cpu cortex-a72 -m 2G -smp 2 \
    -kernel vmlinuz -initrd initrd.img \
    -append "boot=live components console=ttyAMA0,115200 root=LABEL=BTY_LIVE rootwait" \
    -drive if=none,id=stick,format=raw,file=image.img \
    -device virtio-blk-device,drive=stick \
    -drive if=none,id=target,format=qcow2,file=fake-emmc.qcow2 \
    -device virtio-blk-device,drive=target \
    -nographic -serial mon:stdio
```

The TUI banner should reach the serial console within ~30 s;
`lsblk` inside the VM should show both virtio-blk devices. Pi
firmware bootflow + real GPU output are not exercised by this
path; that's hardware-test only.
