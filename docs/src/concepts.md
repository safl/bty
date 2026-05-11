# Concepts

The vocabulary used throughout the rest of the documentation.

## Image

A pre-built system image: the bytes that go on the target disk. bty
treats images as sealed artifacts and never authors their contents.
Supported formats: `.qcow2`, `.img`, `.img.zst`, `.img.xz`,
`.img.gz`, `.img.bz2`. Tarballs (`.tar.gz` etc.) are not flashable
directly; extract first. Images live under a configured *image
root*.

## Target

The block device on the machine being flashed. For the direct-flash
flow this is the local disk seen by the live environment (typically
`/dev/nvme0n1` or `/dev/sda`). For the network-flash flow it is the
target machine's primary disk, selected by the live environment.

## No post-flash provisioning

bty has no provisioning surface at all -- after the bytes land,
bty is done. The target reboots into whatever the cooked image
brings up by itself.

First-boot bring-up (users, network, packages, hostnames) is the
image cooker's job, baked in at build time via cloud-init /
NoCloud user-data. Post-boot config management is whatever you
run from the target itself (ansible, cijoe over SSH from your
workstation, hand-edits) -- not from bty-web. The flasher never
holds credentials against the machines it flashes.

## Disk layout (USB live)

When the bty USB live image is `dd`-ed to a stick, the stick carries
three partitions in an MBR isohybrid layout:

- **ISO9660 partition** (~400 MB). Holds the bty live env (kernel,
  initrd, squashfs). Read-only by definition; live-boot uses a tmpfs
  overlay, so operator changes vanish on reboot. The image on the
  stick is never mutated by use.
- **EFI ESP** (~3 MB). UEFI bootloader; relocated to a non-overlapping
  region so Windows hosts enumerate the stick correctly.
- **`BTY_IMAGES` partition** (2.1 GiB, exFAT, MBR label `BTY_IMAGES`).
  Holds cooked images the operator wants to flash onto target disks
  -- room for a fleet of small `.bri` descriptors plus one large
  `.img.gz` or a few smaller ones. Sized to play nicely with
  Ventoy (which hosts the blobs on its own data partition) and
  KVM-over-IP shims like piKVM / JetKVM (which rely on `.bri`
  pointers rather than bundled blobs). Grow with gparted on your
  host if you need more.

bty auto-mounts `/dev/disk/by-label/BTY_IMAGES` at `/var/lib/bty/images`
on boot. The `bty list images` and `bty inspect image` commands read
from this mount point by default (overridable with `--image-root` or
`BTY_IMAGE_ROOT`).

Operators populate the partition by mounting it on any Linux / macOS /
Windows box - exFAT is universally readable - and dropping `.qcow2`,
`.img`, or `.img.zst` files into it. The partition is *not* under the
overlayroot tmpfs, so files copied there persist on the stick.

## Machine record

A `bty-web`-only concept. A persistent entry in the server's state
keyed by MAC address that captures: assigned image, optional
hostname, boot policy, and (after first PXE contact) last-seen IP +
discovery timestamp. The server uses machine records to render
per-MAC iPXE configurations.

## Boot policy

A field on the [machine record](#machine-record) that decides what
``GET /pxe/{mac}`` serves the target on every PXE contact. Three
values:

- `local` - sanboot fallback; the target boots whatever is on its
  local disk. Stable / production stance, the explicit-PUT default.
- `flash` - chain the live env in auto-flash mode. The target
  re-flashes the assigned image on every PXE boot - the per-job CI
  cadence.
- `tui` - chain the live env in interactive mode. The target lands
  at `bty-tui` on tty1 and the operator picks an image from the
  server's catalog by hand.

The auto-discovery default for unknown MACs is `tui`, so a new box
PXE-booting against a fresh server appliance becomes a useful TUI
session immediately - no per-MAC server-side configuration needed.

The completion signal `POST /pxe/{mac}/done` updates `last_flashed_at`
but never modifies `boot_policy`. Flipping back to `local` after a
flash is an explicit operator action.
