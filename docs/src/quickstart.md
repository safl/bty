# Quickstart

What bty can do today, ordered the way an operator meets it: build a
delivery medium, boot a target, flash a disk, then drive a fleet over the
network via the bty-web server.

## Lowest-barrier trial: bty-web in Docker

To poke at the browser UI before committing to a USB stick or an
appliance, pull the published container:

```bash
docker run -d --name bty-web \
  -p 8080:8080 \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
# -> http://localhost:8080/ui   (login: bty / bty)
```

The docker-managed volume (`-v bty-data:/var/lib/bty`) is the simplest
start; the in-container bty user owns it. For bind-mounts (files show up in
the host filesystem) pre-chown the dir to uid 999 (the bty user) - the
entrypoint checks and exits with a clear hint if it cannot write.

Connect a `bty --catalog http://<host>:8080/catalog.toml` from a USB live
stick or a workstation to flash from this catalog without burning images
onto every stick.

The container has no dnsmasq / TFTP / iPXE - it's the catalog + UI shape,
not the PXE shape. For PXE-driven unattended flashing, build (or download)
the bty-server appliance image. Full details and rotation guidance:
[walkthrough-server-docker.md](walkthrough-server-docker.md).

## Get the USB live image

Either download a pre-built one from the GitHub release, or build
from a checkout.

**Pre-built (fastest):**

```bash
mkdir -p ~/system_imaging/disk && cd ~/system_imaging/disk
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz.sha256
sha256sum -c bty-usb-x86_64.iso.gz.sha256
```

`releases/latest/download/<name>` always points at the newest tag;
swap `latest` for a specific tag (e.g. `v0.11.1`) if you want to pin.

**Build from source** (when you need to modify the image):

```bash
# prerequisites: live-build, debootstrap, squashfs-tools, xorriso,
# exfatprogs, pipx, passwordless sudo
make media-deps                    # one-time: pipx install cijoe
sudo make build VARIANT=usb-x86    # 15-25 min
```

The build runs Debian's `live-build` (debootstrap + mksquashfs +
mkinitramfs) to produce a hybrid ISO, appends a writable `BTY_IMAGES`
exFAT partition, and gzip-compresses the result. Emits:

- `~/system_imaging/disk/bty-usb-x86_64.iso.gz` - distributable
  artifact (the file you decompress + `dd` to a USB stick).
- `~/system_imaging/disk/bty-usb-x86_64-iso-gz.sha256` - checksum.

## Flash a USB stick

```bash
# Identify the USB device first - this is destructive.
lsblk

# /dev/sdX is the USB stick (NOT your local system disk).
gunzip -d --stdout ~/system_imaging/disk/bty-usb-x86_64.iso.gz | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

The stick now has the bty live-boot ISO9660 + EFI partitions plus a 2.1 GiB
exFAT partition labelled `BTY_IMAGES`, pre-staged with four starter `.bri`
descriptors (nosi Debian / Ubuntu / Fedora sysdev images via
`oras://ghcr.io/safl/nosi/...`, plus bty-server) so the catalog is
non-empty out of the box, with room for a typical `bty-server` image (~1-
1.5 GiB compressed) plus headroom. The smaller partition makes the .iso
friendlier to Ventoy hosts and KVM-over-IP shims (piKVM / JetKVM). For more
space, grow `BTY_IMAGES` on your host with gparted after writing the
stick.

## Drop images onto the stick

Mount the `BTY_IMAGES` partition on any Linux / macOS / Windows box
(exFAT is read/write on all three) and copy your pre-built images
into it:

```bash
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt
sudo cp /path/to/my-image.img.gz /mnt/
sudo umount /mnt
```

See the [Disk layout](concepts.md#disk-layout-usb-live) section in
Concepts for the convention bty expects.

## Boot a target machine

Insert the USB stick into the target machine and boot from it. The bty
live env runs `bty` on `tty1` automatically (via `bty-on-tty1.service`), so
the operator lands on the interactive wizard without typing anything.
Alt+F2 through Alt+F6 drop into a root shell for diagnostics; Alt+F1
returns to `bty`.

The rootfs is a read-only SquashFS with a tmpfs overlay (live-boot's
default), so changes in the live env vanish on reboot. The `BTY_IMAGES`
partition is mounted RO at `/var/lib/bty/images` inside the live env
(read-write from any host OS when the stick is removed) - files you copied
there persist.

## What you can do today

### Inspect + flash a target disk

Inside the live env `bty` runs automatically on tty1; on any other Linux
box install the wizard (`pipx install "bty-lab[tui]"`) and launch it as
root:

```bash
sudo bty
```

The wizard is a five-stage flow: pick a catalog source (or skip when local
images exist), pick an image, pick a target disk, confirm the flash plan,
reboot. Each step accepts a number (`1`, `2`, ...) or a single letter for
navigation (`b` back, `q` quit, `r` refresh). A confirmation panel shows
the plan + any validation errors before the destructive write.

`lsblk -d -e7` remains the right tool for "what block devices does the
kernel see"; `bty` shows the same data but only for flash-eligible disks
(excludes loop devices, partitions, read-only media).

### No post-flash provisioning

bty is a flasher, not an image builder. First-boot bring-up (users,
network, packages, hostnames) gets baked into the image upstream via
cloud-init / NoCloud user-data; bty just writes the bytes.

### bty --catalog: pre-load a remote catalog

To start the wizard with a known catalog overlay (e.g. a bty-web instance
hosting your team's image library), pass its URL:

```bash
sudo bty --catalog http://bty-server:8080/catalog.toml
```

This skips the SELECT_CATALOG screen and jumps straight to SELECT_IMAGE
with the catalog merged into the local image-root listing - equivalent to
picking `[c] custom` on the source screen and typing the URL.

See [Reference](reference.md) for the full cmdline surface.

### Network flashing via the bty-web server

`bty-web` is the HTTP server side of bty - browser UI + REST API + the iPXE
chain a target boots into for network-flash. The server appliance image
(`make build VARIANT=server-x86`) ships preconfigured; for a quick local
test run it directly:

```bash
# On the server (or any box you're testing on):
export BTY_STATE_DIR=/var/lib/bty
bty-web   # listens on 0.0.0.0:8080 by default
```

Auth is OS-PAM against the bty service user (the account bty-web runs as).
On the appliance image the default is `bty / bty`; rotate with `sudo passwd
bty` before exposing. The browser UI at `http://server:8080/ui/login` is
the primary operator entry point; ``GET /pxe/{mac}`` (the route PXE clients
hit) is open and needs no auth.

To script mutations from a shell, drive `/ui/login` once to get the cookie,
then attach it on subsequent requests:

```bash
COOKIE=$(curl -sS -i -X POST -d "password=bty" \
   http://server:8080/ui/login \
   | grep -i '^set-cookie:.*bty-token' | sed 's/.*bty-token=\([^;]*\).*/\1/')

curl -H "Cookie: bty-token=$COOKIE" http://server:8080/machines
curl -H "Cookie: bty-token=$COOKIE" -X PUT \
     -H "Content-Type: application/json" \
     -d '{"bty_image_ref":"<64-hex>","boot_policy":"bty-flash-always","target_disk_serial":"<serial>"}' \
     http://server:8080/machines/aa:bb:cc:dd:ee:ff
```

(The flash policies also need a `target_disk_serial` picked from the
machine's reported inventory; without one the chain falls back to a local
boot. Boot the box once as `bty-tui` so it reports its disks, then pick the
target.)

PXE clients hit `GET /pxe/{mac}` (open, no auth) for the per-MAC iPXE
config and chain into the live env, which downloads the assigned image and
flashes the target's local disk.

### Browser UI

`http://server:8080/ui/login` - the same `bty / bty` credential gets you a
cookie-backed session. The dashboard shows machine / image counts; the
**Machines** page is a live table that updates via Server-Sent Events as
PXE clients self-discover. The **Netboot** page has a per-interface
cheatsheet for pointing your LAN DHCP server (option 60/66/67) at the
appliance; bty serves TFTP but does not run DHCP.

All client-side assets (Bootstrap CSS, Bootstrap Icons, HTMX, htmx-ext-sse)
are vendored in the wheel - the appliance contacts no external CDN at
runtime.

## What is coming

See [`PLAN.md`](https://github.com/safl/bty/blob/main/PLAN.md) for the live
roadmap. First-boot bring-up of flashed targets is the image builder's job
(cloud-init / NoCloud user-data); bty stays a flasher.
