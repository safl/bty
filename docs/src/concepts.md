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
bty is done. The target reboots into whatever the pre-built image
brings up by itself.

First-boot bring-up (users, network, packages, hostnames) is the
image builder's job, baked in at build time via cloud-init /
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
  Holds pre-built images the operator wants to flash onto target disks
  -- room for a fleet of small `.bri` descriptors plus one large
  `.img.gz` or a few smaller ones. Sized to play nicely with
  Ventoy (which hosts the blobs on its own data partition) and
  KVM-over-IP shims like piKVM / JetKVM (which rely on `.bri`
  pointers rather than bundled blobs). Grow with gparted on your
  host if you need more.

bty auto-mounts `/dev/disk/by-label/BTY_IMAGES` at `/var/lib/bty/images`
on boot. The ``bty`` wizard scans this mount point by default,
overridable via the `BTY_IMAGE_ROOT` environment variable.

Operators populate the partition by mounting it on any Linux / macOS /
Windows box - exFAT is read/write on all three - and dropping
`.img.gz`, `.qcow2`, `.img.zst`, or `.bri` files into it. The
partition is *not* under the live-boot SquashFS+tmpfs overlay, so
files copied there persist on the stick.

Fresh sticks ship with four starter `.bri` files already on the
partition: three nosi sysdev images via the `oras://ghcr.io/...`
scheme (rolling `:latest` tags resolved to content-addressed
layer digests at flash time) and the latest bty-server appliance
via a GitHub release URL. See [`reference.md`](reference.md) for
the `.bri` schema and `oras://` URL form.

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
  at `bty` on tty1 and the operator picks an image from the
  server's catalog by hand.

The auto-discovery default for unknown MACs is `tui`, so a new box
PXE-booting against a fresh server appliance becomes a useful TUI
session immediately - no per-MAC server-side configuration needed.

The completion signal `POST /pxe/{mac}/done` updates `last_flashed_at`
but never modifies `boot_policy`. Flipping back to `local` after a
flash is an explicit operator action.

## Server-controlled vs interactive: who decides which image gets flashed

`bty` has two operating modes when the kernel cmdline carries
`bty.server` + `bty.mac`. The mode is chosen by `GET /pxe/<mac>/plan`,
which reads the [machine record](#machine-record) on the server side:

- **Server-controlled** (`plan.mode = "auto"`). Triggered when
  `boot_policy in {flash, flash-once}` AND `bty_image_ref` is bound
  AND `target_disk_serial` is picked. The plan response carries the
  image URL + target serial; `bty` flashes them without prompts.
  The server is the source of truth for *what gets flashed*.
- **Interactive** (`plan.mode = "interactive"`). Triggered when
  `boot_policy = tui`, OR when a flash policy can't be auto-resolved
  (no serial picked / orphan ref). `bty` drops the operator into the
  wizard with the server's catalog pre-loaded. The operator picks
  any image from the catalog and flashes any local disk.

The asymmetry worth knowing: **interactive picks are not reported
back to the server.** `bty` does POST `/pxe/<mac>/done` after a
successful flash (so the operator timeline shows a flash happened),
but it does *not* tell the server which image was chosen or which
disk was written. The machine record's `bty_image_ref` /
`target_disk_serial` fields are unchanged by interactive runs.

Practical consequence: if you want the server to drive flashing -
to know which image is on each box, to surface "this MAC will
re-flash on next boot" in `/ui/machines`, to make a flash repeatable
- you must set `boot_policy=flash`, bind a `bty_image_ref`, and pick
a `target_disk_serial` on the server side. Interactive mode is for
"give me a box that boots `bty`, I'll decide locally what to do with
it" - the local pick stays local.
