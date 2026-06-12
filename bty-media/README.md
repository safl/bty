# bty-media

Source content for the bty media images. Three variants:

- **USB live image** (`VARIANT=usb-x86`) - bootable USB carrying the
  bty runtime + a writable exFAT `BTY_IMAGES` partition for pre-built
  images. Built via Debian's live-build (`iso-hybrid` output);
  shipped gzip-compressed as `bty-usb-x86_64.iso.gz` (Etcher / Rufus
  / Raspberry Pi Imager all decompress `.gz` natively; xz tripped
  Etcher's bundled handler regardless of preset).
- **Network-flash live env** (`VARIANT=netboot-x86`) - kernel + initrd +
  squashfs that PXE clients chain into. Built via live-build
  (`netboot` output). Carries the bty runtime plus a
  `bty-on-tty1.service` unit that reads `bty.server` + `bty.mac`
  from `/proc/cmdline` and exec's `bty --server X --mac Y`; ``bty``
  then GETs `<server>/pxe/<mac>/plan` and dispatches (auto-flash,
  interactive wizard, or no-op).
- **Raspberry-Pi USB flasher** (`VARIANT=usb-rpi`) - arm64 image that
  boots a CM5 / Pi5 / Pi4 from a USB stick into the same bty TUI as
  `usb-x86`, sized for in-situ flashing of local eMMC / NVMe.
  Built via live-build (`--architectures arm64 --binary-images tar`)
  then wrapped into a Pi-bootable raw image by
  `scripts/pack_rpi_img.py`. Shipped gzip-compressed as
  `bty-usb-rpi-arm64.img.gz`.

This directory holds the **content** baked into the images: the rootfs
trees that live-build folds in and the live-build config tree. The cijoe
**orchestration** (configs, tasks, scripts) that consumes this
content lives at `cijoe/` at the repo root.

Operators drive everything via the top-level Makefile:
`make build VARIANT=usb-x86|netboot-x86|usb-rpi`.

## Layout

- `auxiliary/cloudinit-metadata.meta` - shared cloud-init metadata.
- `rootfs/common/` - files baked into every variant.
- `live-build/` - live-build config tree shared across every
  variant. The ``BTY_VARIANT`` env var selects the shape:
  ``usb-x86`` -> amd64 iso-hybrid; ``netboot-x86`` -> amd64 netboot
  trio; ``usb-rpi`` -> arm64 netboot trio (wrapped into a
  Pi-bootable raw image by ``scripts/pack_rpi_img.py`` after lb).

## Pipeline

From the repo root:

```
make build VARIANT=usb-x86|netboot-x86|usb-rpi
```

dispatches to one of three cijoe task files. The Makefile picks the
right one based on the variant:

- `usb-x86` -> `cijoe tasks/usb.yaml`. Drives Debian's `live-build`
  with `BTY_VARIANT=usb-x86` selecting `iso-hybrid` output, then
  post-processes the pre-built ISO to append a writable exFAT
  `BTY_IMAGES` partition (`sfdisk --append`, `losetup -fP`,
  `mkfs.exfat`) and gzip-compresses it. Output is
  `bty-usb-x86_64.iso.gz`. No QEMU full-system bake.

- `netboot-x86` -> `cijoe tasks/netboot.yaml`. Drives Debian's
  `live-build` (debootstrap + mksquashfs + mkinitramfs) directly
  on the build host: no QEMU, no cloud-init. Output is the kernel
  + initrd + squashfs trio for PXE chain-boot.

- `usb-rpi` -> `cijoe tasks/usb-rpi.yaml`. Drives `live-build`
  with `BTY_VARIANT=usb-rpi` (`--architectures arm64
  --binary-images tar`) on a native arm64 builder, then
  `scripts/pack_rpi_img.py` assembles a Pi-bootable raw disk
  image: FAT32 partition with `raspi-firmware` blobs + kernel
  + initrd + `config.txt` + `cmdline.txt`, an ext4 partition
  carrying the squashfs as `/live/filesystem.squashfs`, and a
  small exFAT `BTY_IMAGES` partition. Output is
  `bty-usb-rpi-arm64.img.gz`. Operator dd's it to a USB stick
  and boots a CM5 / Pi5 / Pi4 from it.

All three variants stage the bty-lab wheel via `bty_wheel_stage`
into the live-build chroot includes, then drive live-build via
`live_build`; usb-x86 additionally runs `usb_iso_build` for the
exFAT `BTY_IMAGES` post-processing, and usb-rpi runs
`usb_rpi_build` to wrap the lb output into a Pi-bootable image.

## Build prerequisites

All three variants (live-build):
- `live-build` (`sudo apt install live-build`)
- `debootstrap`, `squashfs-tools`, `xorriso` (pulled in by
  `live-build`'s recommends, or install explicitly)
- `exfatprogs` for the usb-x86 post-build BTY_IMAGES exFAT step
  (`mkfs.exfat`)
- `xz-utils` for compressing the final usb-x86 artifact (always
  present on Ubuntu/Debian; listed for completeness)
- `uv` for `bty_wheel_stage` to build the bty-lab wheel; install
  with `pipx install uv` if needed
- Passwordless `sudo` - live-build's chroot operations are
  privileged; CI runners have NOPASSWD by default

All variants:
- `cijoe` (install via `make media-deps`, which runs `pipx install cijoe`)

## Output

usb-x86:
- `~/system_imaging/disk/bty-usb-x86_64.iso.gz` - final artifact.
  Open in Balena Etcher / Raspberry Pi Imager / Rufus DD-mode
  (those tools decompress `.gz` natively), or pipe via CLI:
  `gunzip -d --stdout bty-usb-x86_64.iso.gz | sudo dd of=/dev/sdX bs=4M`.
  Decompress to `.iso` first (`gunzip ...`) before dropping onto a
  Ventoy stick; Ventoy doesn't auto-decompress.

netboot-x86:
- `~/system_imaging/disk/bty-netboot-x86_64.vmlinuz` - kernel
- `~/system_imaging/disk/bty-netboot-x86_64.initrd` - initramfs
- `~/system_imaging/disk/bty-netboot-x86_64.squashfs` - overlay rootfs
- `~/system_imaging/disk/bty-netboot-x86_64.sha256` - manifest

## Status

Both variants ship on every tagged release at
[the GitHub releases page](https://github.com/safl/bty/releases).
The end-to-end PXE chain test (``make test-pxe``) gates each release
on usb-x86 and netboot-x86 building cleanly and the chain working end
to end. Most operators never run this build pipeline themselves -
``bty-media/`` exists for contributors who want to modify the image.

- **usb-x86.** The `.iso.gz` decompresses to a hybrid ISO
  that boots into a Debian live environment with the `bty` wizard
  installed into `/opt/bty/venv`, and an exFAT `BTY_IMAGES`
  partition for pre-built images. live-boot's SquashFS + tmpfs
  overlay provides the ephemeral rootfs (no `overlayroot`
  package needed). End-to-end use case in
  [Walkthrough: USB](../docs/src/walkthrough-usb.md).
- **netboot-x86.** Kernel + initrd + squashfs trio used by PXE clients.
  The chroot ships `bty-on-tty1.service` (after
  `network-online.target`); it reads `bty.server=` + `bty.mac=`
  from `/proc/cmdline` and exec's `bty --server X --mac Y`. ``bty``
  then GETs `<server>/pxe/<mac>/plan` and dispatches: `mode=auto`
  downloads + flashes + reboots, `mode=interactive` drops the
  operator into the wizard, `mode=local` prints a notice and
  exits. Without `bty.mac` on the cmdline (e.g. USB-local boot),
  ``bty`` falls back to scanning the local image-root directory.

  The end-to-end PXE chain (server hands a per-MAC iPXE plan, client
  loads the live trio, flashes a target disk, signals done) is
  exercised by `make test-pxe` and runs in CI on every push.

## Running bty-web

The supported way to stand up a long-running bty-web is the
container deploy (`deploy/compose.yml` / `deploy/quadlet/`); see
[`deploy/README.md`](../deploy/README.md) and
[walkthrough-server-docker.md](../docs/src/walkthrough-server-docker.md).
