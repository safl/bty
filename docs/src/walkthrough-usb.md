# Flash a target with the USB live image

The fastest path to "I just bty-flashed a box":

1. **Build** the bty USB live image once on a host that has KVM.
2. **Write** it to a USB stick with `dd`.
3. **Drop** the system image you want to flash onto the stick's
 `BTY_IMAGES` partition.
4. **Boot** the target machine from the stick.
5. **Flash** with `bty-tui` (interactive) or `bty flash` (scripted).
6. **Reboot** the target into the freshly-flashed image.

End state: the target's local disk has whatever image you copied onto
the stick. No server needed, no network needed once the stick is made.

This walkthrough takes roughly 25 minutes the first time (mostly the
USB build, ~20 minutes), and under 5 minutes for any subsequent flash.

## Prerequisites

| You need | Notes |
|---|---|
| A **build host** with passwordless sudo | live-build runs a chroot, which needs root. Any Linux box works (no KVM required after M19; we no longer bake in QEMU). |
| A **USB stick**, 4 GiB or larger | The cooked image is ~2.5 GiB (~400 MB live boot + 2.1 GiB exFAT `BTY_IMAGES`). Larger sticks just leave the tail unallocated; grow `BTY_IMAGES` with gparted on your host afterwards if you want it. |
| A **target machine** with a free disk | This is the box that will get flashed. UEFI or legacy BIOS, x86_64. |
| A **system image** to flash | `.qcow2`, raw `.img`, or `.img.{zst,xz,gz,bz2}`. Any cooked OS image of yours; bty doesn't ship one. |

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

For a specific version, swap `latest` for the tag (e.g. `v0.8.2`).
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
and xz-compresses the result.

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

The `BTY_IMAGES` partition is **exFAT** so you can mount it on Linux,
macOS, or Windows.

**Linux:**

```bash
sudo mkdir -p /mnt/bty
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt/bty
sudo cp /path/to/my-image.qcow2 /mnt/bty/
sudo umount /mnt/bty
```

**macOS:** the stick auto-mounts at `/Volumes/BTY_IMAGES`. Drag
images in via Finder, or:

```bash
cp /path/to/my-image.qcow2 /Volumes/BTY_IMAGES/
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

## Step 5a: Flash with `bty-tui` (interactive)

```bash
bty-tui
```

The TUI is a three-pane wizard: pick an image, pick a disk,
flash. Each `Enter` commits the current step and advances to
the next; `Esc` (or `Backspace`) walks back.

| Pane | Contents |
|---|---|
| **1: Images** | Cooked images found on `BTY_IMAGES` (or on a remote `bty-web` if you launched with `--server URL`) |
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
| `Shift+R` | Reboot (active after a successful flash) |
| `r` | Refresh the catalog and disk list |
| `s` | Switch the catalog source (local path or remote `bty-web`) |
| `t` | Open the theme picker |
| `/` | Filter the image catalog by substring |
| `q` | Quit the TUI |

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
`f` refuses with a status message. Use ``sudo bty-tui`` if you
need to flash.
```

## Step 5b: Flash with `bty` (scriptable)

If you'd rather drive the flash from a shell - e.g. you want to
script a fleet of identical boxes - the same operations are
available as CLI commands.

```bash
# 1. List what's on the system
bty list disks                          # block devices on the target
bty list images --image-root /mnt/BTY_IMAGES   # images on the stick

# 2. Inspect a specific image
bty inspect image /mnt/BTY_IMAGES/my-image.qcow2

# 3. Dry-run the flash to validate the plan without writing
bty flash \
    --image  /mnt/BTY_IMAGES/my-image.qcow2 \
    --target /dev/sda \
    --dry-run

# 4. Run for real (requires root)
sudo bty flash \
    --image  /mnt/BTY_IMAGES/my-image.qcow2 \
    --target /dev/sda \
    --yes
```

`--dry-run` prints the flash plan and validates without writing.
`--yes` is the explicit consent token for the destructive write - 
`bty flash` refuses to do anything without one or the other, so you
never accidentally wipe a disk.

`--image` accepts an HTTP/HTTPS URL too, not just a local path.
Useful for scripted flashes from a remote `bty-web` (the appliance
or the `ghcr.io/safl/bty-web` Docker container) without pre-staging
the image:

```bash
sudo bty flash \
    --image  http://server:8080/images/my-image.img.zst \
    --target /dev/sda \
    --yes
```

`.img` and `.img.zst` URLs stream straight from the network through
`zstd -d | dd` to disk; `.qcow2` URLs download to a temp file first
(qemu-img needs random access to convert to raw bytes). Either way,
no operator copy step.

`bty flash` writes the bytes and stops. There's no
post-flash provisioning step -- first-boot bring-up (users,
network, packages, hostnames) is the image cooker's job, baked
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

### `bty list disks` shows the USB stick but not the target's internal disk

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
 `bty inspect image` shows the resulting on-disk format).
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

If you don't want to dedicate a USB stick to bty-usb (operator on
the go with many ISOs, remote-only access via IP-KVM, lab-server
out-of-band management), three patterns let you deliver the same
`bty-usb-x86_64.iso` through a multi-boot stick or a virtual mass-
storage shim. All three rely on the
`bty-images-discover.service` shipped with the live env: when the
internal `BTY_IMAGES` partition isn't enumerated by the kernel
(because the shim presents the .iso as a single CD-ROM), the
service scans every attached vfat / exfat partition for a
`bty-images/` folder or top-level `.img.*` / `.qcow2` / `.bri`
files and bind-mounts the first hit at `/var/lib/bty/images`.

### Ventoy (multi-ISO USB stick)

[Ventoy](https://www.ventoy.net) lets a single USB stick boot any
of dozens of `.iso` files, picked from a menu at boot time. Drop
`bty-usb-x86_64.iso` onto a Ventoy stick alongside your other
rescue / installer ISOs and Ventoy boots it like any other entry.

**Setup (one-time):**

```bash
# Install Ventoy on the stick (DESTRUCTIVE - check /dev/sdX!).
# Ventoy's installer is upstream; their docs cover Windows + Linux
# + macOS. On Linux:
sudo Ventoy2Disk.sh -i /dev/sdX
```

This leaves the stick with two partitions: a small EFI/bootloader
partition and a large exFAT data partition. The data partition is
where you drop ISOs.

**Stage bty + its image catalog on the Ventoy data partition:**

```bash
# Mount the data partition (Linux example; on Windows / macOS just
# open the stick in the file manager and drop files in).
sudo mount /dev/disk/by-label/Ventoy /mnt

# Decompress the bty .iso.gz - Ventoy doesn't decompress on the fly.
gunzip -k ~/system_imaging/disk/bty-usb-x86_64.iso.gz
sudo cp ~/system_imaging/disk/bty-usb-x86_64.iso /mnt/

# Drop your cooked images in a top-level ``bty-images/`` folder.
# This is the convention the discovery service looks for; it falls
# back to the partition root if the folder doesn't exist, but the
# subdir is cleaner.
sudo mkdir -p /mnt/bty-images
sudo cp /path/to/my-image.img.gz /mnt/bty-images/
sudo cp /path/to/my-image.qcow2  /mnt/bty-images/

sudo umount /mnt
```

**Boot a target:**

1. Plug the Ventoy stick into the target.
2. Boot, enter the Ventoy menu, pick `bty-usb-x86_64.iso`.
3. The bty live env starts; `bty-images-discover.service` finds
 `/mnt/bty-images/` on the Ventoy data partition and bind-mounts
 it at `/var/lib/bty/images`.
4. `bty-tui` lands on tty1 with your catalog already populated.

If the discovery service doesn't find your files, the empty-state
welcome panel inside the TUI walks through the layout it expects
(top-level files OR a `bty-images/` subdir, vfat or exFAT, no NTFS).
Drop into a root shell via Alt+F2 and run
`journalctl -u bty-images-discover` to see exactly what was scanned.

### piKVM (IP-KVM with mass-storage emulation)

[piKVM](https://pikvm.org) is a Raspberry-Pi-based IP-KVM. It
exposes the target's HDMI + USB as a web UI, and can emulate a
USB mass-storage device so the target boots from an .iso the
operator uploaded over the network. Useful for remote racks where
nobody can plug a stick in.

**Setup (one-time):**

1. Cable the piKVM to the target: HDMI from target -> piKVM HDMI
 in, USB OTG cable from piKVM USB-C -> target USB.
2. Connect piKVM to your LAN; reach its web UI in a browser.
3. Decompress `bty-usb-x86_64.iso.gz` on your workstation (piKVM
 needs the raw `.iso`).

**Upload the bty .iso to piKVM:**

In the piKVM web UI:

1. Open the "Storage" page.
2. Click "Upload" and pick `bty-usb-x86_64.iso`.
3. Set "Mode" to **CD-ROM** (or "Flash drive" - either works; CD-ROM
 mode is closer to how a real bty-usb stick presents).
4. Click "Connect".

**Boot the target:**

1. From the piKVM "Power" page, power-cycle the target.
2. Enter the target's BIOS/UEFI boot menu, pick the piKVM virtual
 mass-storage device.
3. bty live env boots; you see it in the piKVM HDMI viewer.

**Image catalog source - three options:**

a) **Remote `bty-web` (recommended for piKVM).** Run a `bty-web`
 instance somewhere on the same LAN as the target (e.g. the
 trial container; see the [server-Docker
 walkthrough](walkthrough-server-docker.md)). Inside the TUI press
 `s`, pick "Remote", paste the server URL (e.g. `http://10.0.0.5:8080`),
 click Apply. The catalog is fetched live; images stream from the
 server straight through the live env to the target disk. No
 image storage on the piKVM is needed.

b) **piKVM secondary writable partition.** Some piKVM versions
 expose an additional writable area (a "Storage" drive separate
 from the CD-ROM) that you can format as exFAT and populate with
 a `bty-images/` folder. The live env's discovery service picks
 it up automatically. Check your piKVM version's docs for the
 "MSD" / "MSD drives" feature.

c) **`.bri` descriptors.** Drop tiny `.bri` TOML pointer files
 wherever option (a) or (b) would put images; each `.bri` carries
 a `url = "..."` pointing at an `.img.gz` / `.qcow2` somewhere
 reachable from the target. `bty-tui` resolves the URL at flash
 time. Smallest payload to ship through the piKVM.

### JetKVM (commercial IP-KVM)

[JetKVM](https://jetkvm.com) is a compact commercial IP-KVM
(USB-stick-shaped device) that ships the same mass-storage-
emulation feature as piKVM, with a polished hosted control plane.
The bty live env doesn't know the difference between piKVM and
JetKVM - both look like a CD-ROM to the booted kernel.

**Setup (one-time):**

1. Cable the JetKVM to the target per the JetKVM quickstart.
2. Pair it with your account, reach the web UI.
3. Decompress `bty-usb-x86_64.iso.gz` on your workstation.

**Upload the bty .iso:**

In the JetKVM web UI:

1. Open the device's "Virtual Media" panel.
2. Upload `bty-usb-x86_64.iso`.
3. Mount it as a virtual CD-ROM.

**Boot the target:**

Same flow as piKVM - power-cycle, BIOS boot menu, pick the
virtual mass-storage device.

**Image catalog source:**

JetKVM's onboard storage is small (the device is fundamentally
a network-attached USB shim, not a multi-purpose Pi), so option
(b) from the piKVM list is less relevant here.

Recommended: **remote `bty-web` via `s` in the TUI** (option (a) of
the piKVM list above). The catalog and the image bytes both come
from `bty-web` over the network; the JetKVM's job is just to deliver
the boot environment.

If a remote `bty-web` isn't an option, drop `.bri` descriptors on
whatever writable storage the JetKVM exposes (option (c)) and let
them point at HTTP-served images.

## What's next

* For provisioning many machines at once over the network, see the
 server-appliance section in [Quickstart](quickstart.md#network-flashing-via-the-bty-web-server).
 (A full server-appliance walkthrough is queued; until then the
 quickstart covers the same ground at lower depth.)
* For the full CLI surface, see [Reference](reference.md).
* For how the live env works under the hood, see [Concepts](concepts.md).
