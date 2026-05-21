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
``GET /pxe/{mac}`` serves the target on every PXE contact. The
`bty-` prefix marks the policies that PXE-boot into bty's own live
env; `local` and `sanboot` boot something other than bty:

- `local` - iPXE emits `exit`, handing control back to the firmware,
  which then boots the next device in its BIOS/UEFI boot order
  (typically the local disk). It does **not** sanboot. The stable /
  production stance and the explicit-PUT default. Requires the
  firmware boot order to list the disk after PXE (see
  [Firmware boot order](#firmware-boot-order)).
- `sanboot` - iPXE boots the local disk itself
  (`sanboot --drive <drive> || exit`), rather than relying on the
  firmware boot order. The drive defaults to `0x80` (first BIOS
  disk) and is a per-machine override (`sanboot_drive`); the
  `|| exit` falls back to the firmware order if the drive isn't
  bootable.
- `bty-flash-always` - chain the live env in auto-flash mode for a
  fresh flash, then boot the just-flashed disk once before the next
  reflash - the per-job CI cadence. It does **not** loop: the server
  alternates flash-chain then sanboot across PXE contacts (see
  [Firmware boot order](#firmware-boot-order)), so under PXE-first
  firmware the freshly-flashed image actually boots instead of being
  re-flashed on every reboot.
- `bty-flash-once` - flash exactly once: behaves like
  `bty-flash-always` for the next boot, then the completion signal
  flips the policy to the configured *settle policy* so the box
  stops re-flashing.
- `bty-tui` - chain the live env in interactive mode. The target
  lands at `bty` on tty1 and the operator picks an image from the
  server's catalog by hand.

The auto-discovery default for unknown MACs is `bty-tui`, so a new
box PXE-booting against a fresh server appliance becomes a useful TUI
session immediately - no per-MAC server-side configuration needed.

The completion signal `POST /pxe/{mac}/done` always updates
`last_flashed_at`. It mutates `boot_policy` only for `bty-flash-once`,
flipping it to the **settle policy** - `local` (the default) or
`sanboot` - so the box stops re-flashing once it has been imaged.
The settle policy is configurable (setting `flash.settle_policy`, env
`BTY_FLASH_SETTLE_POLICY`) on the Settings page. `bty-flash-always`
is never modified, so the per-job CI cadence keeps reflashing.

(`sanboot` here means iPXE's local-disk boot. Before this was an
explicit policy, `local` was loosely described as "sanboot"; it is
actually a firmware-handoff `exit`.)

## Firmware boot order

For a PXE-driven target, set its BIOS/UEFI firmware to **boot from
the network (PXE) first**. bty-server then decides, per boot, whether
the box re-flashes, drops into the wizard, or boots its disk - all
driven by the machine's `boot_policy`, not by re-toggling the
firmware each time.

What happens *after* a flash depends on the policy:

- With `local`, iPXE runs `exit` and the firmware continues to the
  **next entry in its boot order** - so the local disk must be
  enabled and listed *after* PXE. If the firmware has no bootable
  entry after PXE (disk disabled, or PXE-only), the freshly-flashed
  box won't boot. This is the most common "I flashed it but it won't
  boot" gotcha: it's a firmware boot-order problem, not a bty one.
- With `sanboot`, iPXE boots the local disk itself, so the box boots
  the flashed disk regardless of where the disk sits in the firmware
  order (with `|| exit` falling back to the firmware order). Use this
  when the firmware order is awkward to set, or when you'd rather bty
  drive the local boot. `sanboot` selects the disk by BIOS drive
  number (`0x80` = first disk), not by the Linux serial used at flash
  time, so on a multi-disk box set `sanboot_drive` to the right
  drive.
- With `bty-flash-always`, the freshly-flashed disk boots even though
  PXE is first: the server hands out the flash chain, sees the box
  fetch the live-env artifacts (proof it booted the flasher), and on
  the *next* PXE contact serves a one-shot `sanboot` of the
  just-flashed disk before re-arming the flash chain. So under
  PXE-first firmware the box reflashes, boots the image, runs, and
  reflashes again on the next power cycle - it never loops on the
  flasher without booting. The one-shot `sanboot` honours the
  machine's `sanboot_drive`. Cost: two firmware boots per cycle (one
  to flash, one to boot the disk).

Calibrate `sanboot_drive` before relying on it: on a multi-disk box,
first set `boot_policy=sanboot`, set `sanboot_drive`, and reboot to
confirm the machine actually boots its disk. Once that's verified,
switch to `bty-flash-once` or `bty-flash-always` - the post-flash
`sanboot` then inherits a known-good drive (the field persists across
the policy change), so the boot after a flash isn't a guess.

### `local` vs `sanboot`: which to pick

Both hand the box off to its local disk, but they delegate
differently, and `sanboot` is **not** a strict superset of `local`.
The `|| exit` fallback only fires when the selected drive isn't
bootable; if drive `0x80` *is* bootable but isn't the disk you wanted,
`sanboot` boots the wrong thing and never falls back - exactly where
`local` would have let the firmware choose correctly.

- Prefer `local` when the firmware is configured correctly and you
  want it honoured: a multi-entry boot order, a specific UEFI
  bootloader entry (multi-boot, a chosen default OS, Secure Boot
  chains), RAID/LVM, or just "don't second-guess the firmware".
  `local` keeps iPXE out of the local-boot path entirely and uses the
  firmware's own well-tested boot logic. It is the assumption-free
  default. Its one requirement: the firmware must *advance* past PXE
  to the disk on `exit` (most do; a few restart the boot order).
- Prefer `sanboot` when the firmware order is awkward, unreliable, or
  restarts back to PXE on `exit` (a loop), or on a single-disk box
  where "first disk" is unambiguous and you want a deterministic disk
  boot. `sanboot` forces the disk regardless of firmware order - which
  is also why the flash policies lean on it for the post-flash boot.

Rule of thumb: `local` = "firmware decides"; `sanboot` = "iPXE forces
the first disk". Keep `local` as the default; reach for `sanboot` when
the firmware order can't be trusted to land on the disk.

Practical setup: enter firmware setup (often F2 / F10 / Del at
power-on), open the boot-order menu, put Network/PXE first and the
target disk second, save and exit. UEFI HTTP-Boot and legacy
PXE+TFTP both work; see [DHCP / PXE](walkthrough-server.md) for the
router-side options.

## Server-controlled vs interactive: who decides which image gets flashed

`bty` has two operating modes when the kernel cmdline carries
`bty.server` + `bty.mac`. The mode is chosen by `GET /pxe/<mac>/plan`,
which reads the [machine record](#machine-record) on the server side:

- **Server-controlled** (`plan.mode = "auto"`). Triggered when
  `boot_policy in {bty-flash-always, bty-flash-once}` AND `bty_image_ref` is bound
  AND `target_disk_serial` is picked. The plan response carries the
  image URL + target serial; `bty` flashes them without prompts.
  The server is the source of truth for *what gets flashed*.
- **Interactive** (`plan.mode = "interactive"`). Triggered when
  `boot_policy = bty-tui`, OR when a flash policy can't be auto-resolved
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
- you must set `boot_policy=bty-flash-always`, bind a `bty_image_ref`, and pick
a `target_disk_serial` on the server side. Interactive mode is for
"give me a box that boots `bty`, I'll decide locally what to do with
it" - the local pick stays local.
