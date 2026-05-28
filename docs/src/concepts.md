# Concepts

The vocabulary used throughout the rest of the documentation.

## Image

A pre-built system image: the bytes that go on the target disk. bty treats
images as sealed artifacts and never authors their contents. Supported
formats: `.qcow2`, `.img`, `.img.zst`, `.img.xz`, `.img.gz`, `.img.bz2`.
Tarballs (`.tar.gz` etc.) are not flashable directly; extract first. Images
live under a configured *image root*.

## Target

The block device being flashed. For the direct-flash flow this is the local
disk seen by the live environment (typically `/dev/nvme0n1` or `/dev/sda`).
For the network-flash flow it is the target machine's primary disk,
selected by the live environment.

## No post-flash provisioning

bty has no provisioning surface: after the bytes land, bty is done. The
target reboots into whatever the pre-built image brings up by itself.

First-boot bring-up (users, network, packages, hostnames) is the image
builder's job, baked in at build time via cloud-init / NoCloud user-data.
Post-boot config management is whatever you run from the target itself
(ansible, cijoe over SSH, hand-edits), not from bty-web. The flasher never
holds credentials against the machines it flashes.

## Disk layout (USB live)

When the bty USB live image is `dd`-ed to a stick, the stick carries three
partitions in an MBR isohybrid layout:

- **ISO9660 partition** (~400 MB). Holds the bty live env (kernel, initrd,
  squashfs). Read-only; live-boot uses a tmpfs overlay, so operator changes
  vanish on reboot and the image on the stick is never mutated.
- **EFI ESP** (~3 MB). UEFI bootloader; relocated to a non-overlapping
  region so Windows hosts enumerate the stick correctly.
- **`BTY_IMAGES` partition** (2.1 GiB, exFAT, MBR label `BTY_IMAGES`).
  Holds pre-built images to flash onto target disks: room for a fleet of
  a few `.img.gz` / `.qcow2` files.
  Sized to play nicely with Ventoy (which hosts blobs on its own data
  partition) and KVM-over-IP shims like piKVM / JetKVM (which rely on
  smaller bundled blobs). Grow with gparted if you need
  more.

bty auto-mounts `/dev/disk/by-label/BTY_IMAGES` at `/var/lib/bty/images` on
boot. The ``bty`` wizard scans this mount point by default, overridable via
`BTY_IMAGE_ROOT`.

Operators populate the partition by mounting it on any Linux / macOS /
Windows box (exFAT is read/write on all three) and dropping `.img.gz`,
`.qcow2`, `.img.zst`, or `.img.gz` files into it. The partition is *not* under
the live-boot SquashFS+tmpfs overlay, so files copied there persist.

Fresh sticks ship with an empty BTY_IMAGES partition. The default catalog of seven
nosi images via `oras://ghcr.io/...` (rolling `:latest` tags
resolved to content-addressed layer digests at flash time) and the latest
bty-server appliance via a GitHub release URL. See
[`reference.md`](reference.md) for the catalog schema and `oras://` URL
form.

## Machine record

A `bty-web`-only concept. A persistent entry in the server's state keyed by
MAC address that captures: assigned image, optional hostname, boot mode,
and (after first PXE contact) last-seen IP + discovery timestamp. The
server uses machine records to render per-MAC iPXE configurations.

On every live-env boot `bty` also reports the box's hardware to the record:
the disk list (from `lsblk`, the flasher's target-disk source) and the full
`lshw -json` tree (CPU, RAM, NICs + MACs, peripherals, firmware). The
hardware tree is supplementary (shown on the Machine view and downloadable
raw), so a bty fleet doubles as a passive hardware inventory. The
`bty-inventory` policy keeps that data fresh on boxes that otherwise just
sanboot.

## Boot mode

bty is a **control plane for booting machines**. A target's firmware is
set to PXE-boot first (see [Firmware boot order](#firmware-boot-order)),
so every power-on chains into iPXE, which asks bty-web what to do via
``GET /pxe/{mac}``. The answer is the machine's **boot mode** - a field
on the [machine record](#machine-record) that bty serves on every PXE
contact. There's no per-boot firmware fiddling; the mode is the dial.

The modes are the things bty can do once a box checks in, in three
groups:

**Flash** (the primary job) - chain bty's live env and write a disk image
to the target:

- `bty-flash-always` - flash on every cycle. The per-job CI cadence:
  flash a fresh image, boot it once to run the job, reflash on the next
  power cycle. It does **not** loop on the flasher (see
  [Firmware boot order](#firmware-boot-order)).
- `bty-flash-once` - flash on the next boot only, then boot the disk on
  every boot after that. The mode **stays** `bty-flash-once` (it is not
  rewritten); a one-shot state bit - armed when the box fetched the
  flasher's artifacts - is what flips its behaviour from "flash" to "boot
  the disk". Re-arm by re-saving the machine.
- `bty-tui` - interactive flash. The box lands at `bty` on tty1 and the
  operator picks an image from the server's catalog and flashes by hand.

**Inventory** - `bty-inventory` chains the live env just to re-report the
box's hardware (`lshw` + the disk list), then boots the disk. Like
`bty-flash-always` it alternates an inventory boot then a disk boot
across PXE contacts, so every power cycle refreshes the inventory and
surfaces swapped hardware - no flash, no wizard. This is the
auto-discovery default for unknown MACs, so a new box self-reports its
disks against a fresh server and then just boots; the operator then
assigns a flash mode from the now-populated disk dropdown.

**Boot pass-through** - `ipxe-exit` is the short-circuit: iPXE does *not*
load the live env at all, it hands the box straight to its installed OS.
On UEFI it `exit`s back to the firmware boot order; on legacy BIOS it
`sanboot`s the local disk by BIOS drive number (`0x80` = first disk,
overridable per-machine via `sanboot_drive`). This is how bty boots an
already-provisioned machine, and the explicit-PUT default. (`bty-tui` is
the opt-in for "drop me at the wizard now".)

The completion signal `POST /pxe/{mac}/done` updates `last_flashed_at`
and nothing else - it **never** mutates `boot_mode`. The mode is the
operator's intent; the post-flash "boot the disk" behaviour comes from
the one-shot state bit, not from rewriting the mode. (Before this, a
finished `bty-flash-once` was rewritten to a boot-the-disk mode, which
lied about the operator's configured mode in the UI.)

## Firmware boot order

For a PXE-driven target, set its BIOS/UEFI firmware to **boot from the
network (PXE) first**. bty-server then decides, per boot, whether the box
re-flashes, re-inventories, drops into the wizard, or boots its disk -
all driven by the machine's `boot_mode`, not by re-toggling the firmware
each time.

Booting the local disk (the `ipxe-exit` mode, and the post-flash boot of
the flash modes) is firmware-aware:

- **UEFI** (the common case): iPXE hands control back to the firmware
  boot order via `exit`, and the firmware boots the disk's EFI loader
  (the next entry after network boot). bty doesn't need to know the
  disk's identity - the firmware already does, and a dd'd image carries
  its own ESP + bootloader. Nothing to configure.
- **Legacy BIOS**: iPXE `sanboot`s the disk by BIOS drive number (`0x80`
  = first disk), independent of the firmware boot order, with `|| exit`
  as the fallback if that drive isn't bootable. On a multi-disk box set
  `sanboot_drive` (it's a BIOS drive number, not the Linux serial the
  flash step matches on).

The flash modes reach the freshly-flashed disk the same way, just
deferred one PXE contact: the server hands out the flash chain, sees the
box fetch the live-env artifacts (proof it booted the flasher), and on
the *next* PXE contact serves a one-shot boot of the disk (UEFI exit /
BIOS sanboot) instead of reflashing. `bty-flash-always` then re-arms the
flash chain (reflash, boot, run, reflash - never looping on the flasher);
`bty-flash-once` stays on the disk. Cost: two firmware boots per flash
(one to flash, one to boot the disk).

On legacy BIOS, calibrate `sanboot_drive` before relying on it: set
`boot_mode=ipxe-exit`, set `sanboot_drive`, and reboot to confirm the box
boots its disk; then switch to a flash mode (the field persists, so the
post-flash boot inherits the known-good drive). On UEFI there's nothing
to calibrate - the firmware boot order handles it.

### When the BIOS drive boot can't reach the disk

A legacy-BIOS-only concern (`ipxe-exit` on UEFI just hands back to
firmware). `sanboot` boots by BIOS drive number, so the failure modes
are: a drive that isn't bootable (handled - `|| exit` falls back to the
firmware order) and a multi-disk box where `0x80` isn't the disk you
meant (handled - set `sanboot_drive`). The remaining edge is firmware
where iPXE's `sanboot` itself is flaky. bty keeps no second policy for
that; if a target's firmware can't be driven by `sanboot`, build it a
direct boot stick with
[boots-from](https://github.com/safl/boots-from) rather than relying on
the network path.

Practical setup: enter firmware setup (often F2 / F10 / Del at power-on),
open the boot-order menu, put Network/PXE first and the target disk second,
save and exit. UEFI HTTP-Boot and legacy PXE+TFTP both work; see
[DHCP / PXE](walkthrough-server.md) for the router-side options.

## Server-controlled vs interactive: who decides which image gets flashed

`bty` has two operating modes when the kernel cmdline carries `bty.server`
+ `bty.mac`. The mode is chosen by `GET /pxe/<mac>/plan`, which reads the
[machine record](#machine-record) on the server side:

- **Server-controlled** (`plan.mode = "auto"`). Triggered when
  `boot_mode in {bty-flash-always, bty-flash-once}` AND `bty_image_ref`
  is bound AND `target_disk_serial` is picked. The plan response carries
  the image URL + target serial; `bty` flashes them without prompts. The
  server is the source of truth for *what gets flashed*.
- **Interactive** (`plan.mode = "interactive"`). Triggered when
  `boot_mode = bty-tui`, OR when a flash policy can't be auto-resolved
  (no serial picked / orphan ref). `bty` drops the operator into the wizard
  with the server's catalog pre-loaded; the operator picks any image and
  flashes any local disk.

The asymmetry worth knowing: **interactive picks are not reported back to
the server.** `bty` POSTs `/pxe/<mac>/done` after a successful flash (so
the operator timeline shows a flash happened), but it does *not* tell the
server which image was chosen or which disk was written. The machine
record's `bty_image_ref` / `target_disk_serial` fields are unchanged by
interactive runs.

Practical consequence: to have the server drive flashing - know which image
is on each box, surface "this MAC will re-flash on next boot" in
`/ui/machines`, make a flash repeatable - set `boot_mode=bty-flash-always`,
bind a `bty_image_ref`, and pick a `target_disk_serial` on the server side.
Interactive mode is for "give me a box that boots `bty`, I'll decide
locally" - the local pick stays local.
