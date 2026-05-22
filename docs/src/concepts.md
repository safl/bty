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
  small `.bri` descriptors plus one large `.img.gz` or a few smaller ones.
  Sized to play nicely with Ventoy (which hosts blobs on its own data
  partition) and KVM-over-IP shims like piKVM / JetKVM (which rely on
  `.bri` pointers rather than bundled blobs). Grow with gparted if you need
  more.

bty auto-mounts `/dev/disk/by-label/BTY_IMAGES` at `/var/lib/bty/images` on
boot. The ``bty`` wizard scans this mount point by default, overridable via
`BTY_IMAGE_ROOT`.

Operators populate the partition by mounting it on any Linux / macOS /
Windows box (exFAT is read/write on all three) and dropping `.img.gz`,
`.qcow2`, `.img.zst`, or `.bri` files into it. The partition is *not* under
the live-boot SquashFS+tmpfs overlay, so files copied there persist.

Fresh sticks ship with four starter `.bri` files on the partition: three
nosi sysdev images via `oras://ghcr.io/...` (rolling `:latest` tags
resolved to content-addressed layer digests at flash time) and the latest
bty-server appliance via a GitHub release URL. See
[`reference.md`](reference.md) for the `.bri` schema and `oras://` URL
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

A field on the [machine record](#machine-record) that decides what
``GET /pxe/{mac}`` serves on every PXE contact. The `bty-` prefix marks the
policies that PXE-boot into bty's own live env; `sanboot` boots the local
disk:

- `sanboot` - iPXE boots the local disk itself
  (`sanboot --drive <drive> || exit`). The drive defaults to `0x80` (first
  BIOS disk), overridable per-machine via `sanboot_drive`; the `|| exit`
  hands back to the firmware boot order if the drive isn't bootable. This
  is how bty boots an already-provisioned machine, and the explicit-PUT
  default. (There is no separate `local` policy: a bare `exit` is just
  `sanboot`'s fallback, emitted internally on the no-assignment / error
  paths.)
- `bty-flash-always` - chain the live env in auto-flash mode for a fresh
  flash, then boot the just-flashed disk once before the next reflash (the
  per-job CI cadence). It does **not** loop: the server alternates
  flash-chain then sanboot across PXE contacts (see
  [Firmware boot order](#firmware-boot-order)), so under PXE-first firmware
  the freshly-flashed image actually boots instead of being re-flashed on
  every reboot.
- `bty-flash-once` - flash exactly once: behaves like `bty-flash-always`
  for the next boot, then the completion signal flips the policy to
  `sanboot` so the box boots its freshly-flashed disk and stops
  re-flashing.
- `bty-tui` - chain the live env in interactive mode. The target lands at
  `bty` on tty1 and the operator picks an image from the server's catalog
  by hand.
- `bty-inventory` - to inventory what `bty-flash-always` is to flashing:
  alternates an inventory live-env boot then a sanboot across PXE contacts
  (same mechanism). The active boot chains the live env just to re-report
  the box's disks (no flash, no wizard), then reboots; the next contact
  sanboots the disk. Every power cycle refreshes the inventory before
  booting, surfacing swapped hardware.

The auto-discovery default for unknown MACs is `bty-inventory`, so a new
box PXE-booting against a fresh server appliance self-reports its disks and
then just boots - no per-MAC configuration needed, and the operator can
assign a flash policy from the now-populated disk dropdown. (`bty-tui` is
the explicit opt-in for "drop me at the wizard to flash by hand now".)

The completion signal `POST /pxe/{mac}/done` always updates
`last_flashed_at`. It mutates `boot_mode` only for `bty-flash-once`,
flipping it to `sanboot` so the box boots its freshly-flashed disk.
`bty-flash-always` is never modified, so the per-job CI cadence keeps
reflashing.

## Firmware boot order

For a PXE-driven target, set its BIOS/UEFI firmware to **boot from the
network (PXE) first**. bty-server then decides, per boot, whether the box
re-flashes, drops into the wizard, or boots its disk - all driven by the
machine's `boot_mode`, not by re-toggling the firmware each time.

What happens *after* a flash depends on the policy:

- With `sanboot`, iPXE boots the local disk itself, so the box boots the
  flashed disk regardless of where the disk sits in the firmware order.
  `sanboot` selects the disk by BIOS drive number (`0x80` = first disk),
  not by the Linux serial used at flash time, so on a multi-disk box set
  `sanboot_drive` to the right drive. The `|| exit` safety net hands back
  to the firmware boot order if the chosen drive isn't bootable.
- With `bty-flash-always`, the freshly-flashed disk boots even though PXE
  is first: the server hands out the flash chain, sees the box fetch the
  live-env artifacts (proof it booted the flasher), and on the *next* PXE
  contact serves a one-shot `sanboot` of the just-flashed disk before
  re-arming the flash chain. So the box reflashes, boots the image, runs,
  and reflashes again on the next power cycle - it never loops on the
  flasher without booting. The one-shot `sanboot` honours `sanboot_drive`.
  Cost: two firmware boots per cycle (one to flash, one to boot the disk).

Calibrate `sanboot_drive` before relying on it: on a multi-disk box, first
set `boot_mode=sanboot`, set `sanboot_drive`, and reboot to confirm the
machine boots its disk. Then switch to `bty-flash-once` or
`bty-flash-always` - the post-flash `sanboot` inherits the known-good drive
(the field persists across the policy change), so the boot after a flash
isn't a guess.

### When `sanboot` can't reach the disk

`sanboot` boots by BIOS drive number, so the two failure modes are: a drive
that isn't bootable (handled - `|| exit` hands back to the firmware order)
and a multi-disk box where `0x80` isn't the disk you meant (handled - set
`sanboot_drive`). The remaining edge is firmware where iPXE's `sanboot`
itself is flaky (some UEFI / NVMe quirks). bty keeps no second "bare exit"
policy for that case; if a target's firmware can't be driven by `sanboot`,
build it a direct boot stick with
[boots-from](https://github.com/safl/boots-from) rather than relying on the
network path.

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
