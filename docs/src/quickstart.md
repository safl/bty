# Quickstart

A walk-through of what bty can do today. Each step is tagged with the
milestone it lands in so it is clear what is functional, what is
scaffolded, and what is forward-looking.

## Build the USB live image

*Functional from milestone 2.*

Prerequisites on the build host: `qemu-system-x86_64`, `qemu-img`,
`mkisofs` (Debian package: `genisoimage`), `zstd`, `cijoe`
(`pipx install cijoe`), and KVM acceleration.

From the repo root:

```bash
cd bty-media
make deps      # one-time: pipx install cijoe
make build     # 15-25 min with KVM
```

The build downloads the Debian 13 cloud image, drives cloud-init in
QEMU to bake the rootfs, partitions the disk (3 GB Debian root + 9 GB
exFAT `BTY_IMAGES`), and emits:

- `~/system_imaging/disk/bty-usb-x86_64.qcow2` — intermediate qcow2
  (useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-usb-x86_64.img.zst` — distributable
  artifact (the file you `dd` to a USB stick).
- `~/system_imaging/disk/bty-usb-x86_64.img.zst.sha256` — checksum.

## Flash a USB stick

```bash
# Identify the USB device first - this is destructive.
lsblk

# /dev/sdX is the USB stick (NOT your local system disk).
zstd -d --stdout ~/system_imaging/disk/bty-usb-x86_64.img.zst | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

The stick now has the bty Debian rootfs partition plus an empty exFAT
partition labelled `BTY_IMAGES`.

## Drop images onto the stick

Mount the `BTY_IMAGES` partition on any Linux / macOS / Windows box
(exFAT is universally readable) and copy your cooked images into it:

```bash
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt
sudo cp /path/to/my-image.qcow2 /mnt/
sudo umount /mnt
```

See the [Disk layout](concepts.md#disk-layout-usb-live) section in
Concepts for the convention bty expects.

## Boot a target machine

Insert the USB stick into the target machine and boot from it. The bty
live env auto-logins as root on `tty1` and shows a placeholder banner
(milestone 2 scaffolding; the real `bty-tui` lands in milestone 10).

The rootfs is mounted read-only with a tmpfs overlay (`overlayroot`),
so anything you change in the live env vanishes on reboot. The
`BTY_IMAGES` partition is *not* overlaid — files you copied there
persist.

## What you can do today

*Functional from milestones 3+4.*

Inside the live env (or on any Linux box where `bty` is installed):

```bash
# List interesting block devices on the system
bty list disks

# List images available under /var/lib/bty/images (or BTY_IMAGE_ROOT)
bty list images

# Inspect a specific image in detail
bty inspect image /var/lib/bty/images/my-image.qcow2

# Each leaf command also accepts --json
bty list disks --json
bty inspect image --json /var/lib/bty/images/my-image.qcow2
```

*Functional from milestones 5+6.*

```bash
# 1. Validate that an image can be flashed to a target without writing.
bty flash --image /var/lib/bty/images/my-image.qcow2 \
          --target /dev/sdX \
          --provision none \
          --dry-run

# 2. Once the plan looks right, run for real (requires root):
sudo bty flash --image /var/lib/bty/images/my-image.qcow2 \
               --target /dev/sdX \
               --provision none \
               --yes
```

`--dry-run` prints a plan and validates without writing. `--yes` is
the explicit consent token for the destructive write — `bty flash`
refuses to do anything without one or the other.

*Functional from milestone 8.* Cloud-init seeding after the flash:

```bash
sudo bty flash --image /var/lib/bty/images/debian.qcow2 \
               --target /dev/sdX \
               --provision cloud-init \
               --user-data ./userdata.yaml \
               --yes
```

`bty` mounts the cloud-init-enabled rootfs partition on the target,
drops `user-data` (and a synthesised `meta-data` if `--meta-data` is
not supplied) under `/var/lib/cloud/seed/nocloud-net/`, and unmounts.
On first boot the OS picks up the seed via cloud-init's NoCloud
datasource.

See [Reference > CLI](reference.md#cli) for the full surface.

## What is coming

| Milestone | Capability |
|-----------|------------|
| 9         | Provisioning: `cijoe` (offline mode) |
| 10        | `bty-tui` interactive UI in the live env |
| 11-12     | `bty-web` server + browser UI |
| 13        | `bty-media` server image |
| 14        | Network-flash end-to-end over iPXE |
| 15        | `cijoe` online provisioning |

See [`PLAN.md`](https://github.com/safl/bty/blob/main/PLAN.md) for the
roadmap detail.
