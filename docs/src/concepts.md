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

The block device on the machine being provisioned. For the direct-flash
flow this is the local disk seen by the live environment (typically
`/dev/nvme0n1` or `/dev/sda`). For the network-flash flow it is the
target machine's primary disk, selected by the live environment.

## Provisioning mode

What (if anything) configures the deployed system after the bytes
land. Four modes:

- `none` - no post-flash configuration; reboot into the cooked image
  as-is.
- `cloud-init` - populate the OS's cloud-init seed (NoCloud datasource)
  before the target reboots; the OS picks it up on first boot.
- `cijoe` - run a CIJOE workflow against the freshly-flashed filesystem
  (mount, edit, unmount) before the target reboots. Constrained to
  filesystem-level customisation.
- `cijoe-online` - bty-web only. After the target first-boots into its
  own OS, `bty-web` runs a CIJOE workflow against the running machine
  and records the post-workflow state as that machine's known-good
  baseline. The server, not the image, becomes the source of truth
  for "what this box should look like."

`bty flash` understands the first three modes (offline,
filesystem-level). The fourth runs server-side; see the
[components](components.md) chapter and the
[reference](reference.md#wire-types) for the wire shape.

## Disk layout (USB live)

When the bty USB live image is `dd`-ed to a stick, the stick carries
three partitions in an MBR isohybrid layout:

- **ISO9660 partition** (~400 MB). Holds the bty live env (kernel,
  initrd, squashfs). Read-only by definition; live-boot uses a tmpfs
  overlay, so operator changes vanish on reboot. The image on the
  stick is never mutated by use.
- **EFI ESP** (~3 MB). UEFI bootloader; relocated to a non-overlapping
  region so Windows hosts enumerate the stick correctly.
- **`BTY_IMAGES` partition** (4 GiB, exFAT, MBR label `BTY_IMAGES`).
  Holds cooked images the operator wants to flash onto target disks.
  Sized for the dominant single-image `bty-server` flash use case;
  grow with gparted on your host if you need more.

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
keyed by MAC address that captures: assigned image, provisioning mode,
optional hostname, references to CIJOE workflows, and (after first boot)
the post-workflow known-good baseline. The server uses machine records
to render per-MAC iPXE configurations and to drive online CIJOE runs.

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
