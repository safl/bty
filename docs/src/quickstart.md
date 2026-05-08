# Quickstart

A walk-through of what bty can do today, ordered roughly the way an
operator would meet it: build a delivery medium, boot a target,
flash, optionally provision, and finally drive a fleet over the
network via the bty-web server.

## Get the USB live image

Either download a pre-built one from the GitHub release, or build
from a checkout.

**Pre-built (fastest):**

```bash
mkdir -p ~/system_imaging/disk && cd ~/system_imaging/disk
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.xz
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.xz.sha256
sha256sum -c bty-usb-x86_64.iso.xz.sha256
```

`releases/latest/download/<name>` always points at the newest tag;
swap `latest` for a specific tag (e.g. `v0.2.7`) if you want to pin.

**Build from source** (when you need to modify the image):

```bash
# prerequisites: live-build, debootstrap, squashfs-tools, xorriso,
# exfatprogs, xz-utils, pipx, passwordless sudo
make media-deps                    # one-time: pipx install cijoe
sudo make build VARIANT=usb-x86    # 15-25 min
```

The build runs Debian's `live-build` (debootstrap + mksquashfs +
mkinitramfs) to produce a hybrid ISO, post-processes it to append
a writable `BTY_IMAGES` exFAT partition, and xz-compresses the
result. Emits:

- `~/system_imaging/disk/bty-usb-x86_64.iso.xz` - distributable
  artifact (the file you decompress + `dd` to a USB stick).
- `~/system_imaging/disk/bty-usb-x86_64-iso-xz.sha256` - checksum.

## Flash a USB stick

```bash
# Identify the USB device first - this is destructive.
lsblk

# /dev/sdX is the USB stick (NOT your local system disk).
xz -d --stdout ~/system_imaging/disk/bty-usb-x86_64.iso.xz | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

The stick now has the bty live-boot ISO9660 + EFI partitions plus
an empty exFAT partition labelled `BTY_IMAGES`. On first boot, a
one-shot service grows `BTY_IMAGES` to fill the rest of the stick
(only when empty - operator data is preserved on subsequent
boots).

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
live env auto-logins as root on `tty1`. From there you can run the CLI
(`bty list disks`, `bty flash ...`) or `bty-tui` for an interactive
terminal UI.

The rootfs is a read-only SquashFS with a tmpfs overlay (live-boot's
default), so anything you change in the live env vanishes on
reboot. The `BTY_IMAGES` partition is mounted RO at
`/var/lib/bty/images` inside the live env (read-write from any host
OS when the stick is removed) - files you copied there persist.

## What you can do today

### Inspect

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

### Flash a target disk

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
the explicit consent token for the destructive write - `bty flash`
refuses to do anything without one or the other.

### Cloud-init provisioning

Seed cloud-init's NoCloud datasource onto the freshly-flashed disk
so the target self-configures on first boot:

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

### CIJOE provisioning (offline)

Run a cijoe workflow against the freshly-flashed filesystem before
the target reboots:

```bash
sudo bty flash --image /var/lib/bty/images/debian.qcow2 \
               --target /dev/sdX \
               --provision cijoe \
               --cijoe-workflow ./tweaks.yaml \
               --yes
```

`bty` mounts the largest partition on the target, exports
`BTY_ROOTFS` pointing at the mount, then runs the supplied cijoe
workflow. Workflow tasks reference `$BTY_ROOTFS` to drop config
files, install seed credentials, etc. Requires `cijoe` on `PATH`
(install via `pipx install cijoe`).

Interactive flashing via the TUI:

```bash
sudo bty-tui
```

The TUI lists available images (left pane) and block devices (right
pane). Cursor between the panes, select with Enter, then press `F`
to flash. A modal shows the plan and any validation errors; confirm
to run. A status modal streams the result.

Without root the TUI still launches in a read-only mode (you can
inspect lists), but the `F` action refuses with a status message.
Requires the `[tui]` install extra (`pipx install "bty-lab[tui]"`).

See [Reference > CLI](reference.md#cli) for the full surface.

### Network flashing via the bty-web server

`bty-web` is the HTTP server side of bty - browser UI + REST API +
the iPXE chain a target boots into for network-flash. The server
appliance image (`make build VARIANT=server-x86`) ships preconfigured;
for a quick local test you can run it directly:

```bash
# On the server (or any box you're testing on):
export BTY_STATE_DIR=/var/lib/bty
bty-web   # listens on 0.0.0.0:8080 by default
```

Auth is OS-PAM against the bty service user (the account bty-web
runs as). On the appliance image the default is `bty / bty`; rotate
with `sudo passwd bty` before exposing. The browser UI at
`http://server:8080/ui/login` is the primary operator entry point;
``GET /pxe/{mac}`` (the route PXE clients hit) is open and needs no
auth.

If you want to script mutations from a shell, drive `/ui/login` once
to get the cookie, then attach it on subsequent requests:

```bash
COOKIE=$(curl -sS -i -X POST -d "password=bty" \
   http://server:8080/ui/login \
   | grep -i '^set-cookie:.*bty-token' | sed 's/.*bty-token=\([^;]*\).*/\1/')

curl -H "Cookie: bty-token=$COOKIE" http://server:8080/machines
curl -H "Cookie: bty-token=$COOKIE" -X PUT \
     -H "Content-Type: application/json" \
     -d '{"image":"debian.qcow2","provisioning_mode":"none","boot_policy":"flash"}' \
     http://server:8080/machines/aa:bb:cc:dd:ee:ff
```

PXE clients hit `GET /pxe/{mac}` (open, no auth) for the per-MAC
iPXE config and chain into the live env, which downloads the
assigned image and flashes the target's local disk.

### Browser UI

`http://server:8080/ui/login` - the same `bty / bty` credential
gets you a cookie-backed session. The dashboard shows machine /
image counts; the **Machines** page is a live table that updates
via Server-Sent Events as PXE clients self-discover. The
**Settings** page activates the dnsmasq proxy-DHCP block when
you're ready to start serving PXE.

All client-side assets (Bootstrap CSS, Bootstrap Icons, HTMX,
htmx-ext-sse) are vendored in the wheel - the appliance does not
contact any external CDN at runtime.

## What is coming

See [`PLAN.md`](https://github.com/safl/bty/blob/main/PLAN.md) for
the live roadmap (per-machine cijoe online provisioning, image
catalog upload via the UI, target-disk hints in the per-MAC plan,
etc.).
