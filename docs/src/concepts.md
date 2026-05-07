# Concepts

The vocabulary used throughout the rest of the documentation.

## Image

A pre-built system image: the bytes that go on the target disk. bty
treats images as sealed artifacts and never authors their contents.
Supported formats: `.qcow2`, `.img`, `.img.zst`. Images live under a
configured *image root*.

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
exactly two persistent partitions:

- **Debian root partition** (~3 GB). Holds the bty live env. Mounted
  read-only at runtime via `overlayroot`; operator changes go to a
  tmpfs overlay and disappear on reboot. The image on the stick is
  never mutated by use.
- **`BTY_IMAGES` partition** (~9 GB, exFAT, GPT label `BTY_IMAGES`).
  Holds cooked images the operator wants to flash onto target disks.

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
