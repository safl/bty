# Quickstart

What bty can do today, ordered the way an operator meets it: build a
delivery medium, boot a target, flash a disk, then drive a fleet over the
network via the bty-web server.

## Deploy the bty server

```bash
sudo uvx bty-lab deploy /opt/bty
#   bty-web:   http://<host>:8080/ui     (login: bty-lab / bty-lab)
#   withcache: http://<host>:3000/       (login: bty-lab / bty-lab)
```

That's it. `deploy` auto-detects install mode from your euid:

- **As root (recommended)** -- full **system install**: writes
  `envvars`, brings up the stack *with* the TFTP sidecar, installs
  Podman Quadlet units to `/etc/containers/systemd/`, and starts the
  services via systemctl. Stack survives host reboots.
- **As a regular user** -- **user install**: compose-only. No TFTP
  sidecar (binds privileged UDP/69), no autostart. Operator must
  re-run `podman compose up -d` after host reboot. UEFI HTTP Boot
  works; legacy BIOS PXE clients won't. The CLI prints exactly what
  was skipped and how to promote to a system install at the end.

`HOST_ADDR` is detected from the host's outbound-route IP; admin
passwords default to `bty-lab`. Change the passwords in `/opt/bty/envvars`
before exposing past trusted LAN.

- `uvx bty-lab upgrade /opt/bty` -- in-place upgrade. Auto-detects
  compose- vs Quadlet-managed; preserves `envvars` + `data/`.
- `uvx bty-lab init /opt/bty` -- emit files only, no side effects
  (inspect / customise before applying).

Bind-mount layout, env vars, the full subcommand surface:
[`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
and [walkthrough-server-docker.md](walkthrough-server-docker.md).

## Get the USB live image

Either download a pre-built one from the GitHub release, or build
from a checkout.

**Pre-built (fastest):**

```bash
mkdir -p ~/system_imaging/disk && cd ~/system_imaging/disk

# Discover the current release via release.toml (stable URL); each
# artifact's filename carries the version, so the manifest is the
# single source of truth for "what's the latest".
curl -fsSL -o release.toml \
  https://github.com/safl/bty/releases/latest/download/release.toml
VERSION=$(grep -E '^version *=' release.toml | head -1 | cut -d'"' -f2)
echo "Latest bty release: v$VERSION"

curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usbboot-pc-x86_64-v$VERSION.iso
curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usbboot-pc-x86_64-v$VERSION.iso.sha256
sha256sum -c bty-usbboot-pc-x86_64-v$VERSION.iso.sha256
```

`releases/latest/download/release.toml` is a stable URL (GitHub
redirects to the newest tag's copy) so the lookup never breaks across
releases. To pin a specific version, swap `latest` for a tag (e.g.
`v0.25.5`) and skip the `release.toml` step.

**Build from source** (when you need to modify the image):

```bash
# prerequisites: live-build, debootstrap, squashfs-tools, xorriso,
# exfatprogs, pipx, passwordless sudo
make media-deps                    # one-time: pipx install cijoe
sudo make build VARIANT=usbboot-pc    # 15-25 min
```

The build runs Debian's `live-build` (debootstrap + mksquashfs +
mkinitramfs) to produce a hybrid ISO, appends a writable `BTY_IMAGES`
exFAT partition, and gzip-compresses the result. Emits:

- `~/system_imaging/disk/bty-usbboot-pc-x86_64-v<version>.iso` - distributable
  artifact (the file you decompress + `dd` to a USB stick).
- `~/system_imaging/disk/bty-usbboot-pc-x86_64-v<version>.iso.sha256` - checksum.

## Flash a USB stick

```bash
# Identify the USB device first - this is destructive.
lsblk

# /dev/sdX is the USB stick (NOT your local system disk).
sudo dd if=~/system_imaging/disk/bty-usbboot-pc-x86_64-v$VERSION.iso \
        of=/dev/sdX bs=4M status=progress oflag=direct conv=fsync
sync
```

The stick now has the bty live-boot ISO9660 + EFI partitions plus a small
exFAT partition labelled `BTY_IMAGES` that auto-grows to fill the stick on
first boot (32 MiB at bake, then up to the disk's tail). The wizard's
`[d] default` catalog
(nosi Debian / Ubuntu / Fedora / FreeBSD headless images plus a Fedora
desktop, via `oras://ghcr.io/safl/nosi/...`) streams from GHCR at flash
time, so the partition starts empty and has room for a typical headless
image (~1-1.5 GiB compressed) plus headroom. The smaller partition makes
the .iso friendlier to Ventoy hosts and KVM-over-IP shims (piKVM /
JetKVM). For more space, grow `BTY_IMAGES` on your host with gparted after
writing the stick.

## Drop images onto the stick

Mount the `BTY_IMAGES` partition on any Linux / macOS / Windows box
(exFAT is read/write on all three) and copy your pre-built images
into it:

```bash
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt
sudo cp /path/to/nosi-debian-sysdev-x86_64.img.gz /mnt/
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
chain a target boots into for network-flash. The canonical deploy is the
container stack from the [Deploy the bty server](#deploy-the-bty-server)
section above (`uvx bty-lab init`). For contributor / dev work on a
checkout you can also run it directly:

```bash
# On the server (or any box you're testing on):
export BTY_PATHS_STATE_DIR=/var/lib/bty
bty-web   # listens on 0.0.0.0:8080 by default
```

The operator UI is gated by `$BTY_ADMIN_PASSWORD` (constant-time compare);
when it is unset the UI is open and bty-web logs a startup warning. Set it
before exposing, and rotate by changing the env var and restarting bty-web.
The browser UI at `http://server:8080/ui/login` is
the primary operator entry point; ``GET /pxe/{mac}`` (the route PXE clients
hit) is open and needs no auth.

To script mutations from a shell, drive `/ui/login` once to get the cookie,
then attach it on subsequent requests:

```bash
COOKIE=$(curl -sS -i -X POST -d "password=bty-lab" \
   http://server:8080/ui/login \
   | grep -i '^set-cookie:.*bty-token' | sed 's/.*bty-token=\([^;]*\).*/\1/')

curl -H "Cookie: bty-token=$COOKIE" http://server:8080/machines
curl -H "Cookie: bty-token=$COOKIE" -X PUT \
     -H "Content-Type: application/json" \
     -d '{"bty_image_ref":"<64-hex>","boot_mode":"bty-flash-always","target_disk_serial":"<serial>"}' \
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

`http://server:8080/ui/login` - the `$BTY_ADMIN_PASSWORD` value gets you a
cookie-backed session. The dashboard shows machine / image counts; the
**Machines** page is a live table that updates via Server-Sent Events as
PXE clients self-discover. The **Netboot** page has a per-interface
cheatsheet for pointing your LAN DHCP server (option 60/66/67) at the bty
host; bty serves TFTP (via the sidecar) but does not run DHCP.

All client-side assets (Bootstrap CSS, Bootstrap Icons, HTMX, htmx-ext-sse)
are vendored in the wheel - bty-web contacts no external CDN at runtime.
