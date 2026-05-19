# bty-media

Source content for the bty appliance images. Four variants:

- **USB live image** (`VARIANT=usb-x86`) - bootable USB carrying the
  bty runtime + a writable exFAT `BTY_IMAGES` partition for pre-built
  images. Built via Debian's live-build (`iso-hybrid` output);
  shipped gzip-compressed as `bty-usb-x86_64.iso.gz` (Etcher / Rufus
  / Raspberry Pi Imager all decompress `.gz` natively; xz tripped
  Etcher's bundled handler regardless of preset).
- **Server image, x86_64** (`VARIANT=server-x86`) - installable disk
  image for the bty server appliance (`bty-web` + PXE boot stack).
  Cloud-init bake in QEMU.
- **Server image, Raspberry Pi 4/5** (`VARIANT=server-rpi`) - same
  appliance role on arm64 for SD-card delivery to a Pi. Built via
  losetup-mount + chroot in `qemu-aarch64-static`.
- **Network-flash live env** (`VARIANT=netboot-x86`) - kernel + initrd +
  squashfs that PXE clients chain into. Built via live-build
  (`netboot` output). Carries the bty runtime plus a
  `bty-on-tty1.service` unit that reads `bty.server` + `bty.mac`
  from `/proc/cmdline` and exec's `bty --server X --mac Y`; ``bty``
  then GETs `<server>/pxe/<mac>/plan` and dispatches (auto-flash,
  interactive wizard, or no-op).

This directory holds the **content** baked into the images: cloud-init
base templates (server only), rootfs trees that live-build /
cloud-init fold in, and the live-build config tree. The cijoe
**orchestration** (configs, tasks, scripts) that consumes this
content lives at `cijoe/` at the repo root.

Operators drive everything via the top-level Makefile:
`make build VARIANT=usb-x86|server-x86|server-rpi|netboot-x86`.

## Layout

- `auxiliary/cloudinit-base-server.user` - cloud-init base template
  for the server bake. (usb-x86 uses live-build, no cloud-init.)
- `auxiliary/cloudinit-metadata.meta` - shared cloud-init metadata.
- `rootfs/common/` - files baked into every disk-image variant.
- `rootfs/server/` - files baked into the server image. Each file
  becomes a cloud-init `write_files` entry whose `path` mirrors the
  file's path under the role subdirectory. Binary files (anything
  that is not valid UTF-8) are emitted with `encoding: b64`.
- `live-build/` - live-build config tree shared by `usb-x86` (which
  uses `iso-hybrid` output) and `netboot-x86` (which uses `netboot`
  output). The `BTY_USB_ISO=1` env var switches `auto/config`
  between the two modes.

## Pipeline

From the repo root:

```
make build VARIANT=usb-x86|server-x86|server-rpi|netboot-x86
```

dispatches to one of four cijoe task files. The Makefile picks the
right one based on the variant:

- `server-x86` -> `cijoe tasks/build.yaml` (cloud-init bake of a
  Debian cloud image inside QEMU). Steps:

  1. **`bty_wheel_stage`** - builds a `bty-lab` wheel from the
     parent repo via `uv build` and stages it under
     `rootfs/server/opt/bty/`. The wheel is base64-inlined into
     cloud-init by the next step and `pip install`ed into a system
     venv at `/opt/bty/venv` during the bake.
  2. **`gen_userdata`** - assembles the cloud-init userdata file by
     inlining files under `rootfs/common/` and `rootfs/server/` as
     `write_files` entries on top of
     `auxiliary/cloudinit-base-server.user`.
  3. **`diskimage_build`** - downloads the Debian 13 cloud image,
     resizes the qcow2 boot disk, builds the cloud-init seed.iso,
     and boots QEMU. cloud-init provisions the system and powers
     off; the baked qcow2 is compacted via `qemu-img convert -c`.
  4. **`img_gz_publish`** - converts the qcow2 to raw and
     gzip-compresses the result into a `dd`-able `.img.gz`,
     alongside a sha256sum.

- `usb-x86` -> `cijoe tasks/usb.yaml`. Drives Debian's `live-build`
  with `BTY_USB_ISO=1` selecting `iso-hybrid` output, then post-
  processes the pre-built ISO to append a writable exFAT `BTY_IMAGES`
  partition (`sfdisk --append`, `losetup -fP`, `mkfs.exfat`) and
  gzip-compresses it. Output is `bty-usb-x86_64.iso.gz`. No QEMU
  full-system bake.

- `server-rpi` -> `cijoe tasks/build-rpi.yaml`. Customises Raspberry
  Pi OS Lite arm64 in place: download upstream image, grow + losetup-
  mount, drop the `rootfs/server/` overlay, chroot via
  `qemu-aarch64-static` to install packages + create users + install
  the bty-lab venv, then re-compress to `.img.gz`. Two steps:
  `bty_wheel_stage` then `rpi_image_customize`.

- `netboot-x86` -> `cijoe tasks/netboot.yaml`. Drives Debian's `live-build`
  (debootstrap + mksquashfs + mkinitramfs) directly on the build
  host - no QEMU, no cloud-init. Output is the kernel + initrd +
  squashfs trio for PXE chain-boot.

## Build prerequisites

server-x86 (cloud-init bake):
- `qemu-system-x86_64` and `qemu-img` (Debian package
  `qemu-system-x86` and `qemu-utils`)
- `mkisofs` (Debian package `genisoimage`)
- `zstd`
- KVM acceleration (configured in `configs/server-x86.toml`); without
  it the cloud-init bake step is impractically slow
- `uv` for `bty_wheel_stage` to build the bty-lab wheel; install
  with `pipx install uv` if needed

usb-x86 + netboot-x86 (live-build):
- `live-build` (`sudo apt install live-build`)
- `debootstrap`, `squashfs-tools`, `xorriso` (pulled in by
  `live-build`'s recommends, or install explicitly)
- `exfatprogs` for the usb-x86 post-build BTY_IMAGES exFAT step
  (`mkfs.exfat`)
- `xz-utils` for compressing the final usb-x86 artifact (always
  present on Ubuntu/Debian; listed for completeness)
- Passwordless `sudo` - live-build's chroot operations are
  privileged; CI runners have NOPASSWD by default

server-rpi (chroot in qemu-user):
- `qemu-user-static` + `binfmt-support` (registers the
  `qemu-aarch64` handler so amd64 hosts can chroot into arm64
  rootfs)
- `xz-utils`, `parted`, `e2fsprogs`, `zstd` for image extraction,
  loopback growth, and final compression

All variants:
- `cijoe` (install via `make media-deps`, which runs `pipx install cijoe`)

## Output

server-x86:
- `~/system_imaging/disk/bty-server-x86_64.qcow2` - baked, compacted
  qcow2 (intermediate; useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-server-x86_64.img.gz` - final
  artifact. Decompress with `gunzip` and pipe to `dd` (or feed
  directly to Etcher / Raspberry Pi Imager / Rufus DD-mode).

usb-x86:
- `~/system_imaging/disk/bty-usb-x86_64.iso.gz` - final artifact.
  Open in Balena Etcher / Raspberry Pi Imager / Rufus DD-mode
  (those tools decompress `.gz` natively), or pipe via CLI:
  `gunzip -d --stdout bty-usb-x86_64.iso.gz | sudo dd of=/dev/sdX bs=4M`.
  Decompress to `.iso` first (`gunzip ...`) before dropping onto a
  Ventoy stick; Ventoy doesn't auto-decompress.

server-rpi:
- `~/system_imaging/disk/bty-server-rpi-arm64.img.gz` - final
  artifact for `dd` to an SD card.

netboot-x86:
- `~/system_imaging/disk/bty-netboot-x86_64.vmlinuz` - kernel
- `~/system_imaging/disk/bty-netboot-x86_64.initrd` - initramfs
- `~/system_imaging/disk/bty-netboot-x86_64.squashfs` - overlay rootfs
- `~/system_imaging/disk/bty-netboot-x86_64.sha256` - manifest

## Status

All four variants ship on every tagged release at
[the GitHub releases page](https://github.com/safl/bty/releases).
The end-to-end PXE chain test (``make test-pxe``) gates each release
on usb-x86, server-x86, and netboot-x86 building cleanly and the
chain working end to end. server-rpi (Raspberry Pi 4 / 5) builds in
the same matrix but isn't covered by the PXE chain test (which is
amd64-only); first-boot smoke-testing happens out-of-band on real
hardware. Most operators never run this build pipeline themselves -
``bty-media/`` exists for contributors who want to modify the image.

- **usb-x86.** The `.iso.gz` decompresses to a hybrid ISO
  that boots into a Debian live environment with the `bty` CLI +
  TUI installed into `/opt/bty/venv`, and an exFAT `BTY_IMAGES`
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
- **server-x86.** Bootable Debian cloud-image hosting `bty-web` with
  single-tenant PAM auth: the `bty` service user is the sole
  principal, default credential `bty / bty`, rotated on the appliance
  with `sudo passwd bty`. An `odus` admin user is also baked in (with
  passwordless sudo) for SSH-side maintenance. The server image's
  `bty-web-init.service` oneshot creates `BTY_STATE_DIR`, initialises
  the SQLite schema, and rewrites `/etc/issue` to point operators at
  `http://<ip>:8080/ui`. The dnsmasq PXE block is shipped commented
  out and activated from the browser UI's Settings page (no shell
  edits required).
- **server-rpi.** Same appliance role on arm64, delivered as an SD-card
  image for Raspberry Pi 4 / 5. Built by mounting the upstream
  Raspberry Pi OS Lite arm64 image via losetup and customising it in a
  qemu-aarch64-static chroot (no QEMU full-system bake): apt install,
  bty + odus user creation, bty-lab venv install, service enables.
  Same `bty / bty` PAM credential and `odus / odus` SSH admin as the
  x86 server image; same `bty-web-init.service` first-boot.

  ### Operator first-boot

  1. Write the `.img.gz` to the server's disk (or attach as a VM
     disk). Pre-built artifacts at
     <https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz>.
  2. Boot. The login prompt's banner shows the browser UI URL.
  3. Log in to `/ui/login` with `bty / bty` (or rotate first via
     `sudo passwd bty` on the appliance).
  4. From `/ui/settings`, pick the interface + subnet for PXE and
     click Activate. dnsmasq restarts; PXE clients on that segment
     will now chain through bty-web.
