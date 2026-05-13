# Flash a target with the USB live image

The fastest path to "I just bty-flashed a box":

1. **Build** the bty USB live image once on a Linux host.
2. **Write** it to a USB stick with `dd`.
3. **Drop** the system image you want to flash onto the stick's
 `BTY_IMAGES` partition.
4. **Boot** the target machine from the stick.
5. **Flash** with `bty tui` (interactive) or `bty flash` (scripted).
6. **Reboot** the target into the freshly-flashed image.

End state: the target's local disk has whatever image you copied onto
the stick. No server needed, no network needed once the stick is made.

This walkthrough takes roughly 25 minutes the first time (mostly the
USB build, ~20 minutes), and under 5 minutes for any subsequent flash.

## Prerequisites

| You need | Notes |
|---|---|
| A **build host** with passwordless sudo | live-build runs a chroot, which needs root. Any Linux box works; no KVM required. |
| A **USB stick**, 4 GiB or larger | The pre-built image is ~2.5 GiB (~400 MB live boot + 2.1 GiB exFAT `BTY_IMAGES`). Larger sticks just leave the tail unallocated; grow `BTY_IMAGES` with gparted on your host afterwards if you want it. |
| A **target machine** with a free disk | This is the box that will get flashed. UEFI or legacy BIOS, x86_64. |
| A **system image** to flash | `.qcow2`, raw `.img`, or `.img.{zst,xz,gz,bz2}`. Any pre-built OS image of yours; bty doesn't ship one. |

The build host runs Debian 12+ or Ubuntu 24.04+. Other Linux distros
work if you can install `live-build`, `debootstrap`, `squashfs-tools`,
`xorriso`, `exfatprogs`, and `pipx`. There's a one-shot install
script for Debian-family at
[`scripts/install-dev-deps.sh`](https://github.com/safl/bty/blob/main/scripts/install-dev-deps.sh).

## Step 1: Get the USB image

You have two options - download a pre-built one, or build from source.

### Option A: Download the latest pre-built image (fastest)

Each tagged release publishes the USB image as a GitHub release asset.
The `releases/latest/download/<name>` URLs always redirect to the
newest version, so you can pin to "latest" or to a specific tag.

```bash
mkdir -p ~/system_imaging/disk && cd ~/system_imaging/disk

curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz.sha256
sha256sum -c bty-usb-x86_64.iso.gz.sha256
```

For a specific version, swap `latest` for the tag (e.g. `v0.8.3`).
Browse all releases at <https://github.com/safl/bty/releases>.

### Option B: Build from a checkout (when you want to modify it)

```bash
make media-deps                    # one-time: pipx installs cijoe
sudo make build VARIANT=usb-x86    # ~15 minutes
```

What this does: runs Debian's `live-build` (debootstrap + mksquashfs
+ mkinitramfs) directly on the build host (no QEMU, no cloud-init)
to produce a hybrid ISO carrying the bty CLI + TUI, then post-
processes the ISO to append a writable `BTY_IMAGES` exFAT partition,
and gzip-compresses the result.

When it finishes:

```text
~/system_imaging/disk/
  bty-usb-x86_64.iso.gz             <- the file you'll write to the stick
  bty-usb-x86_64-iso-gz.sha256
```

## Step 2: Write the image to a USB stick

**Identify the device first** - this step is destructive:

```bash
lsblk
```

Find the USB stick. It'll typically show as `sda` or `sdb` and have
the size of your stick. **Do not** confuse it with your laptop's
internal disk.

Two ways to write it:

**GUI flashers** (Balena Etcher, Raspberry Pi Imager, Rufus in DD
mode): open `bty-usb-x86_64.iso.gz` directly. They decompress
`.gz` natively, no extra step.

**Command line:**

```bash
gunzip -d --stdout ~/system_imaging/disk/bty-usb-x86_64.iso.gz | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Replace `/dev/sdX` with the actual device. The `conv=fsync` and
trailing `sync` are belt-and-braces: `dd` exits before the kernel has
flushed buffers, and unplugging early can land you with a half-written
stick.

Eject and re-plug the stick once. The kernel re-reads the partition
table, and you should now see the second partition labeled
`BTY_IMAGES`.

## Step 3: Drop your image(s) onto BTY_IMAGES

A fresh stick already ships with four starter `.bri` (bty Remote
Image) descriptors on the `BTY_IMAGES` partition, so you can flash
without copying anything onto the stick first:

- `nosi-debian-sysdev-x86_64.bri` -- Debian 13 trixie sysdev image
- `nosi-ubuntu-sysdev-x86_64.bri` -- Ubuntu 26.04 LTS resolute sysdev image
- `nosi-fedora-sysdev-x86_64.bri` -- Fedora 44 sysdev image
- `bty-server-x86_64.bri` -- latest bty-server appliance

All three nosi entries use `oras://ghcr.io/safl/nosi/<variant>:latest`,
which bty resolves at flash time to the current GHCR-published
layer digest. The bty-server entry uses a GitHub release URL.
See [`reference.md`](reference.md) for the `.bri` schema and the
`oras://` scheme details.

To add your own pre-built images, mount the partition and drop
files in. It's **exFAT** so you can mount it on Linux, macOS, or
Windows.

**Linux:**

```bash
sudo mkdir -p /mnt/bty
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt/bty
sudo cp /path/to/my-image.img.gz /mnt/bty/
sudo umount /mnt/bty
```

**macOS:** the stick auto-mounts at `/Volumes/BTY_IMAGES`. Drag
images in via Finder, or:

```bash
cp /path/to/my-image.img.gz /Volumes/BTY_IMAGES/
diskutil unmount /Volumes/BTY_IMAGES
```

**Windows:** the stick gets a drive letter (typically `D:` or `E:`).
Copy images in via Explorer.

You can drop **multiple images** onto the stick if you'll be flashing
several different OSes from the same boot media. The TUI lists every
recognised image it finds on `BTY_IMAGES`.

## Step 4: Boot the target from USB

Plug the stick into the target machine, power it on, and select the
USB stick from the boot menu. The boot-menu key varies by vendor; a
few common ones:

| Vendor | Boot-menu key |
|---|---|
| Dell | F12 |
| HP | F9 |
| Lenovo / ThinkPad | F12 |
| Intel NUC | F10 |
| Generic AMI | F11 / Esc |

The bty live env auto-logins as `root` on `tty1`. From there you have
two ways to flash: the TUI (interactive, recommended for one-offs) or
the CLI (scriptable).

## Step 5a: Flash with `bty tui` (interactive)

```bash
bty tui
```

The TUI is a three-pane wizard: pick an image, pick a disk,
flash. Each `Enter` commits the current step and advances to
the next; `Esc` (or `Backspace`) walks back.

| Pane | Contents |
|---|---|
| **1: Images** | Pre-built images found on `BTY_IMAGES` plus any entries from the catalog source passed to `--catalog SOURCE` (a local TOML path, HTTP URL, or `oras://` reference) |
| **2: Disks** | Block devices detected on this machine |
| **3: Flash** | A big `Flash!` button (becomes `Reboot` after a successful flash) |

Common keys:

| Key | What happens |
|---|---|
| `1` / `2` | Jump focus back to the Images / Disks pane |
| Arrow keys / `h` `l` | Cycle focus between panes |
| `Enter` | Commit the focused row, advance to the next pane (on the Flash button: trigger the flash) |
| `Esc` / `Backspace` | Undo the most recent commit, return one step |
| `f` | Trigger the flash from anywhere once both image and disk are picked |
| `r` | Refresh the catalog and disk list |
| `c` | Switch the catalog (local path or remote `bty-web`) |
| `i` | Install `bty-server` (latest from GitHub releases) |
| `/` | Filter the image catalog by substring |
| `q` | Quit the TUI |

After a successful flash the action-pane button flips from "Flash"
to "Reboot"; hit Enter on the focused button to reboot. There is no
single-key reboot shortcut (the old `Shift+R` binding was dropped
because it shoulder-rubbed `r` for refresh).

A confirmation modal shows the flash plan (image format, target
size, validation). `Enter` runs it; `Esc` cancels. A status modal
then streams the write progress; when it closes, the Flash pane's
button transforms into `Reboot` so you can boot into the
freshly-written image with a single keystroke.

<!--
A screenshot of bty-tui mid-flash will land at
``_static/screenshot-tui-flashing.png`` once the asciinema /
screenshot capture pass is done.
-->


```{note}
Without root the TUI launches in **read-only mode** - you can
browse images and disks, but the Flash button is disabled and
`f` refuses with a status message. Use ``sudo bty tui`` if you
need to flash.
```

## Step 5b: Flash with `bty` (scriptable)

If you'd rather drive the flash from a shell - e.g. you want to
script a fleet of identical boxes - the same operations are
available as CLI commands.

```bash
# 1. List what's on the system
lsblk -d -e7                              # block devices on the target
bty images --image-root /mnt/BTY_IMAGES   # images on the stick

# 2. Inspect a specific image
bty inspect /mnt/BTY_IMAGES/my-image.img.gz

# 3. Dry-run the flash to validate the plan without writing
bty flash /mnt/BTY_IMAGES/my-image.img.gz /dev/sda --dry-run

# 4. Run for real (requires root)
sudo bty flash /mnt/BTY_IMAGES/my-image.img.gz /dev/sda --yes
```

`--dry-run` prints the flash plan and validates without writing.
`--yes` is the explicit consent token for the destructive write - 
`bty flash` refuses to do anything without one or the other, so you
never accidentally wipe a disk.

The `IMAGE` positional accepts an HTTP/HTTPS URL too, not just a
local path. Useful for scripted flashes from a remote `bty-web`
(the appliance or the `ghcr.io/safl/bty-web` Docker container)
without pre-staging the image:

```bash
sudo bty flash http://server:8080/images/my-image.img.zst /dev/sda --yes
```

`.img` and `.img.zst` URLs stream straight from the network through
`zstd -d | dd` to disk; `.qcow2` URLs download to a temp file first
(qemu-img needs random access to convert to raw bytes). Either way,
no operator copy step.

`bty flash` writes the bytes and stops. There's no
post-flash provisioning step -- first-boot bring-up (users,
network, packages, hostnames) is the image builder's job, baked
in via cloud-init / NoCloud user-data at image-build time. bty
itself only writes bytes.

## Step 6: Reboot

Power-cycle the target without the USB stick. The newly-flashed disk
should boot the OS you wrote.

If it doesn't, see **Troubleshooting** below.

## Troubleshooting

### The target won't boot from USB

* Confirm the stick is bootable on a different machine.
* Check the target's BIOS/UEFI for "secure boot" - bty's live image
 is unsigned and won't boot under secure boot. Disable it (or
 switch to legacy / CSM mode) for the bty live env.
* On older BIOSes, USB 3.0 sticks sometimes only enumerate from
 USB 2.0 ports. Try a different port.

### `lsblk` shows the USB stick but not the target's internal disk

* The kernel may not have a driver for an unusual storage controller
 (e.g. some embedded NVMe-over-PCIe paths on consumer mini-PCs).
 `dmesg | tail` from the live env shows what was probed.
* If the disk is hidden behind a hardware RAID, you'll need to
 configure the RAID for JBOD / passthrough first.

### Flash succeeds but the target doesn't boot

* Confirm the image's format is right for what you wanted. A qcow2
 flashed onto a disk creates a qcow2-formatted disk, not a
 bootable filesystem. For a bootable target, use a raw `.img` or
 let `bty flash` convert the qcow2 (which it does automatically:
 `bty inspect` shows the resulting on-disk format).
* If the image was built for UEFI but the target is configured for
 legacy BIOS (or vice versa), the firmware won't find a bootloader.
 Check the target's BIOS settings.

### Validation fails with "image format not recognised"

The image's filename extension determines bty's format detection.
Supported: `.qcow2`, `.img`, `.img.zst`. If your image has an
unusual extension, rename it.

### "target /dev/sdX is mounted"

bty refuses to flash a disk that has a mounted partition; otherwise
it'd corrupt whatever filesystem was using it. Unmount any mounted
partitions of the target disk first:

```bash
sudo umount /dev/sdX*
```

## Alternative delivery shapes

Three concrete ways to deliver the bty live image without
dedicating a USB stick to it: a multi-boot stick via Ventoy, or
remote boot via an IP-KVM (piKVM, JetKVM). The Ventoy path also
carries the image catalog; the IP-KVM paths use a remote `bty-web`
for the catalog because IP-KVMs expose the `.iso` as a single
CD-ROM and there is no local storage to put image files on.

**Always-available bty-server install shortcut.** Regardless of
which delivery shape you use, `bty tui` has a built-in `i`
shortcut that pre-selects the latest `bty-server-x86_64.img.gz`
from GitHub releases. From the TUI: press `i`, pick a target disk,
hit Flash. The image streams directly from GitHub through the live
env to the target's disk; no local staging is needed.

Network constraint: the live env needs HTTPS reachability to
`github.com` / `objects.githubusercontent.com` at flash time.
Air-gapped operators should ship their own `bty-server.img.gz`
via the Ventoy `bty-images/` folder path described below instead.

### Ventoy

[Ventoy](https://www.ventoy.net) lets one USB stick boot any of
dozens of `.iso` files via a menu at power-on. The Ventoy data
partition doubles as exFAT-formatted scratch space, which bty's
live env auto-discovers and uses as the image catalog.

#### Step 1: Install Ventoy on a USB stick

```bash
# DESTRUCTIVE: this wipes /dev/sdX. Verify the device with lsblk
# first.
sudo Ventoy2Disk.sh -i /dev/sdX
```

Ventoy's installer is upstream; their docs cover Windows + Linux +
macOS. After install, the stick has two partitions: an EFI /
bootloader partition (small) and an exFAT data partition labelled
`Ventoy` (large; uses the rest of the stick).

#### Step 2: Stage `bty-usb-x86_64.iso` on the Ventoy partition

```bash
# Mount the Ventoy data partition.
sudo mount /dev/disk/by-label/Ventoy /mnt

# Decompress the bty .iso.gz - Ventoy boots .iso, not .iso.gz.
gunzip -k ~/system_imaging/disk/bty-usb-x86_64.iso.gz

# Copy the .iso to the Ventoy data partition.
sudo cp ~/system_imaging/disk/bty-usb-x86_64.iso /mnt/
```

The `.iso` can sit at the root of the Ventoy partition or in any
subdirectory. Ventoy's menu lists every `.iso` it finds anywhere
on the partition.

#### Step 3: Stage your pre-built images in `bty-images/`

```bash
# Create the bty-images/ folder at the root of the Ventoy partition.
sudo mkdir -p /mnt/bty-images

# Copy as many pre-built images as you need. Supported extensions:
# *.img.gz / *.img.zst / *.img.xz / *.img.bz2 / *.qcow2 / *.bri
# (bri is a tiny TOML pointer at a remote image URL).
sudo cp /path/to/debian-13-server.img.gz /mnt/bty-images/
sudo cp /path/to/ubuntu-26.04-server.img.gz /mnt/bty-images/

# Unmount so the writes flush.
sudo umount /mnt
```

The discovery service accepts either layout:

1. **Recommended**: a `bty-images/` subfolder at the partition
   root, with your `.img.gz` / `.qcow2` / `.bri` files inside.
   Keeps your pre-built images visually separate from the `.iso`
   files Ventoy is booting.
2. **Quick-drop**: the same files at the partition root,
   alongside `bty-usb-x86_64.iso`. Less tidy but supported -- the
   discovery service falls through to the root if no
   `bty-images/` subfolder exists.

The service tries the subfolder first, then falls back to the
root. First match with at least one supported file (`.bri` /
`.img*` / `.qcow2`) wins, gets bind-mounted at
`/var/lib/bty/images`, and the TUI picks it up from there.

#### Step 4: Boot the target

1. Plug the Ventoy stick into the target machine.
2. Power-cycle the target, enter the BIOS/UEFI boot menu, pick the
   Ventoy stick.
3. Ventoy's menu appears. Pick `bty-usb-x86_64.iso`.
4. bty live env boots. `bty-images-discover.service` scans the
   attached partitions, finds `bty-images/` on the Ventoy stick,
   and bind-mounts it at `/var/lib/bty/images`.
5. `bty-tui` opens on tty1 with your image catalog already
   populated.

#### Troubleshooting

If `bty-tui` shows "No images in the catalog yet":

1. Press `Alt+F2` for a root shell on the alternate VT.
2. Run `journalctl -u bty-images-discover` to see exactly which
   partitions were scanned and which it skipped (and why).
3. Confirm the Ventoy partition's filesystem is exFAT (NTFS isn't
   probed): `lsblk -f`.
4. Confirm the folder is exactly `bty-images/` at the partition
   root (not `Bty-Images/`, not nested).
5. `Alt+F1` returns to `bty-tui`; press `r` once you've fixed the
   layout to re-scan.

### piKVM (remote catalog only)

[piKVM](https://pikvm.org) is a Raspberry-Pi-based IP-KVM. It
exposes a target machine's HDMI + USB over the network and can
emulate a USB mass-storage device, so the target boots from an
`.iso` an operator uploaded via the piKVM web UI.

**piKVM hosts the `.iso` as a single CD-ROM.** The kernel inside
the bty live env cannot reach the `.iso`'s internal partitions, so
there is no place on the piKVM to put image files. Use a remote
`bty-web` instance for the catalog instead.

#### Step 1: Set up a `bty-web` server reachable from the target

The simplest option is the trial container; see
[walkthrough-server-docker.md](walkthrough-server-docker.md) for
the full setup. On any host that the target can reach over the
LAN:

```bash
docker run -d --name bty-web \
  -p 8080:8080 \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

The default credentials are `bty / bty`. Note the host's IP
address (e.g. `10.0.0.5`); the target needs to reach it on TCP
8080.

Upload your pre-built images to the server via the bty-web Images
page (`http://10.0.0.5:8080/ui/images`).

#### Step 2: Connect piKVM to the target

1. HDMI: target's HDMI out -> piKVM HDMI in.
2. USB: piKVM's USB-C OTG port -> target's USB port.
3. Network: piKVM to your LAN.
4. Open piKVM's web UI in a browser.

#### Step 3: Upload `bty-usb-x86_64.iso` to piKVM

```bash
# Decompress on your workstation before uploading; piKVM doesn't
# unzip on the fly.
gunzip -k ~/system_imaging/disk/bty-usb-x86_64.iso.gz
```

In the piKVM web UI:

1. Open the "Storage" page.
2. Click "Upload" and select `bty-usb-x86_64.iso`.
3. Wait for the upload to finish.
4. Pick the entry; in the dialog, set "Mode" to **CD-ROM**.
5. Click "Connect" (or the analogous "Attach" button).

#### Step 4: Boot the target

1. From piKVM's "Power" page, power-cycle the target.
2. In the piKVM HDMI viewer, watch the target's BIOS/UEFI come up.
3. Enter the target's boot menu, pick the piKVM virtual storage
   device.
4. The bty live env boots; `bty-tui` opens on tty1.

#### Step 5: Point `bty-tui` at the remote `bty-web` catalog

The local catalog is empty (no images on the piKVM). Switch to the
remote `bty-web` from inside the TUI:

1. Press `c` (Catalog).
2. In the modal, enter the catalog source URL:
   `http://10.0.0.5:8080/catalog.toml` (substitute your host).
3. Click "Apply".

`bty-tui` fetches the TOML catalog from `GET /catalog.toml` on the
server. Pick an image (Enter), pick the target disk (Enter), flash
(Enter on Flash). The image streams directly from `bty-web` through
the live env to the target's disk; piKVM only carried the boot env.

If you already have bty's published default catalog in mind, press
`d` instead at the empty-state screen: that points the TUI at
`https://github.com/safl/bty/releases/latest/download/catalog.toml`
without typing.

If you don't yet have a `bty-web` instance running (chicken-and-
egg case for setting up the very first one), press `i` instead:
that's the built-in shortcut to install `bty-server` straight
from GitHub. Once it boots, point subsequent piKVM-driven
flashes at the new server.

### JetKVM (remote catalog only)

[JetKVM](https://jetkvm.com) is a compact commercial IP-KVM
(USB-stick-shaped device) with mass-storage emulation. Same
constraint as piKVM: the `.iso` is hosted as a single CD-ROM, so
there is no local storage on the JetKVM to put image files.
Use a remote `bty-web` for the catalog.

#### Step 1: Set up a `bty-web` server reachable from the target

Same as piKVM Step 1 above. Run a `bty-web` instance somewhere on
the LAN; note its IP and port.

#### Step 2: Connect JetKVM to the target

Cable per the JetKVM quickstart (USB-C from JetKVM to target;
JetKVM to LAN). Pair the device with your JetKVM account, reach
its web UI.

#### Step 3: Upload `bty-usb-x86_64.iso` to JetKVM

```bash
gunzip -k ~/system_imaging/disk/bty-usb-x86_64.iso.gz
```

In the JetKVM web UI:

1. Open the "Virtual Media" panel.
2. Upload `bty-usb-x86_64.iso`.
3. Mount the uploaded image as a virtual CD-ROM.

#### Step 4: Boot the target

1. Power-cycle the target via JetKVM's power-control page.
2. In the HDMI viewer, enter the target's boot menu and pick the
   JetKVM virtual storage device.
3. The bty live env boots; `bty-tui` opens on tty1.

#### Step 5: Point `bty-tui` at the remote `bty-web` catalog

Same as piKVM Step 5. Press `c`, enter the catalog URL
(e.g. `http://10.0.0.5:8080/catalog.toml`), click Apply. The
catalog populates from the server, images stream through the
JetKVM-booted live env to the target's disk.

To bootstrap the very first `bty-server` (no existing one to point
at), press `i` instead of `c`: the built-in shortcut installs
`bty-server` directly from GitHub's latest release.

## What's next

* For provisioning many machines at once over the network, see the
 server-appliance section in [Quickstart](quickstart.md#network-flashing-via-the-bty-web-server).
 (A full server-appliance walkthrough is queued; until then the
 quickstart covers the same ground at lower depth.)
* For the full CLI surface, see [Reference](reference.md).
* For how the live env works under the hood, see [Concepts](concepts.md).
