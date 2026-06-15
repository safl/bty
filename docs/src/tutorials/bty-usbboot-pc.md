# bty via bty-usb

The fastest path from "I have a USB stick" to "this box is
running my image":

1. **Download** the bty USB live image.
2. **Write** it to a USB stick with `dd` (or a GUI flasher).
3. **Boot** the target machine from the stick.
4. **Flash** with `bty` -- the wizard on tty1 picks an image
   and a target disk.
5. **Reboot** the target into the freshly-flashed image.

No server, no extra staging needed. The wizard ships with the
default [nosi](https://github.com/safl/nosi) catalog and streams
images straight from `ghcr.io` at flash time. Want your own
images on the stick instead? See [Extras](#extras).

## Prerequisites

| You need | Notes |
|---|---|
| A **USB stick**, 4 GiB or larger | The bty USB image is ~2.5 GiB (~400 MB live boot + 2.1 GiB exFAT `BTY_IMAGES`). Larger sticks just leave the tail unallocated; see [Extras](#extras) to grow it. |
| A **target machine** with a free disk | UEFI or legacy BIOS, x86_64. |

## Step 1: Download the bty USB image

Each tagged release publishes the USB image + checksum as a
GitHub release asset. The `releases/latest/download/<name>`
URLs always redirect to the newest version.

```bash
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso.sha256
sha256sum -c bty-usbboot-pc-x86_64.iso.sha256
```

For a specific version, replace `latest` with the tag (e.g.
`v0.39.0`). Browse releases at
<https://github.com/safl/bty/releases>.

## Step 2: Write the image to a USB stick

**Identify the device first** -- this is destructive:

```bash
lsblk
```

Find the USB stick (typically `sda` / `sdb`, sized to your
stick). **Do not** confuse it with your laptop's internal disk.

```bash
sudo dd if=bty-usbboot-pc-x86_64.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Replace `/dev/sdX` with the actual device. `conv=fsync` and the
trailing `sync` are belt-and-braces: `dd` exits before the
kernel flushes buffers, and unplugging early can land you with
a half-written stick.

### GUI alternative

If you'd rather click than type, [Balena Etcher],
[Raspberry Pi Imager], or [Rufus] (in DD mode) all accept
`bty-usbboot-pc-x86_64.iso` directly. They handle device selection
and flushing for you.

[Balena Etcher]: https://etcher.balena.io/
[Raspberry Pi Imager]: https://www.raspberrypi.com/software/
[Rufus]: https://rufus.ie/

Eject and re-plug the stick once. The kernel re-reads the
partition table; you should now see the second partition
labelled `BTY_IMAGES`.

## Step 3: Boot the target from USB

Plug the stick into the target, power it on, and select the USB
stick from the boot menu. The key varies by vendor:

| Vendor | Boot-menu key |
|---|---|
| Dell | F12 |
| HP | F9 |
| Lenovo / ThinkPad | F12 |
| Intel NUC | F10 |
| Generic AMI | F11 / Esc |

The bty live env auto-logins as `root` on `tty1` and starts the
`bty` wizard automatically.

## Step 4: Flash with `bty`

`bty` is already running on tty1. The wizard is a five-stage
flow:

| Stage | What it asks |
|---|---|
| **1: Source**  | Default catalog (bty release), custom catalog URL (http / oras), or local-only |
| **2: Image**   | Pick from the chosen catalog overlay (and any locally staged images) |
| **3: Disk**    | Block devices detected on this machine (filtered to flash-eligible) |
| **4: Confirm** | Shows the flash plan + validation; `y` to write, `b` to back |
| **5: Reboot**  | After a successful flash, boot the freshly-written image |

The default catalog ships with the
[nosi](https://github.com/safl/nosi) headless + desktop images
(e.g. `nosi-debian-sysdev-x86_64.img.gz`,
`nosi-fedora-sysdev-x86_64.img.gz`) and resolves at flash time
from `ghcr.io`. You don't need to stage anything on the stick to
flash a working image -- you just need HTTPS reachability at
flash time.

Common keys at every prompt:

| Key | What happens |
|---|---|
| `<number>` | Pick row N from the list (1-based) |
| `b`        | Back one stage (undo the most recent commit) |
| `r`        | Refresh the current list |
| `q`        | Quit |

```{note}
Without root the wizard launches in **read-only mode** -- you
can browse images and disks, but the Flash button is disabled.
The live env logs you in as root by default, so this only
matters on a workstation install.
```

## Step 5: Reboot

After a successful flash, the wizard offers to reboot.
Power-cycle the target without the USB stick; the newly-flashed
disk should boot the OS you wrote.

If it doesn't, see [Troubleshooting](#troubleshooting).

## Extras

### Adding your own images to BTY_IMAGES

The `BTY_IMAGES` partition is exFAT, so you can mount it on any
Linux / macOS / Windows host and drop in your own `.img.gz` /
`.img.zst` / `.qcow2` files. The wizard lists every recognised
image it finds on the partition alongside the default catalog.

**Linux:**

```bash
sudo mkdir -p /mnt/bty
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt/bty
sudo cp /path/to/nosi-debian-sysdev-x86_64.img.gz /mnt/bty/
sudo umount /mnt/bty
```

**macOS:** the stick auto-mounts at `/Volumes/BTY_IMAGES`. Drag
images in via Finder, or:

```bash
cp /path/to/nosi-debian-sysdev-x86_64.img.gz /Volumes/BTY_IMAGES/
diskutil unmount /Volumes/BTY_IMAGES
```

**Windows:** the stick gets a drive letter (typically `D:` /
`E:`). Copy images in via Explorer.

You can drop **multiple images** to flash several different OSes
from the same stick.

### Growing BTY_IMAGES on a larger stick

Fresh sticks size `BTY_IMAGES` to 2.1 GiB -- enough for a
handful of images, and friendly to Ventoy + KVM-over-IP shims
that have bundled-blob size limits. Larger sticks just leave the
tail unallocated.

To use the rest, open the stick in `gparted` (Linux) or any
exFAT-aware partition editor and resize `BTY_IMAGES`
right-to-end. The exFAT filesystem inside resizes with the same
tool. The bty live env picks up the larger mount on next boot --
no config change.

### Alternative delivery shapes

The same bty `.iso` also works in:

- [bty via bty-ventoy](bty-ventoy.md) -- multi-boot USB stick:
  one stick carries the bty boot env *and* your pre-built target
  images.
- [bty via BMC / OOB-MGMT](bmc.md) -- piKVM / JetKVM /
  proprietary server BMCs that mount the `.iso` as virtual
  media.
- [bty via netboot](bty-netboot-pc.md) -- skip the USB stick
  entirely; PXE-boot a fleet from a bty-web server.

## Troubleshooting

### The target won't boot from USB

* Confirm the stick is bootable on a different machine.
* Check the target's BIOS/UEFI for "secure boot" -- bty's live
  image is unsigned and won't boot under secure boot. Disable it
  (or switch to legacy / CSM mode) for the bty live env.
* On older BIOSes, USB 3.0 sticks sometimes only enumerate from
  USB 2.0 ports. Try a different port.

### `lsblk` shows the USB stick but not the target's internal disk

* The kernel may not have a driver for an unusual storage
  controller (e.g. some embedded NVMe-over-PCIe paths on
  consumer mini-PCs). `dmesg | tail` from the live env shows
  what was probed.
* If the disk is hidden behind a hardware RAID, configure the
  RAID for JBOD / passthrough first.

### Flash succeeds but the target doesn't boot

* Confirm the image's format is right. A qcow2 flashed onto a
  disk creates a qcow2-formatted disk, not a bootable
  filesystem. For a bootable target, use a raw `.img` or let bty
  convert the qcow2 at flash time (it does so automatically via
  `qemu-img convert`).
* If the image was built for UEFI but the target is set to
  legacy BIOS (or vice versa), the firmware won't find a
  bootloader. Check the target's BIOS settings.

### "target /dev/sdX is mounted"

bty refuses to flash a disk that has a mounted partition;
otherwise it'd corrupt whatever filesystem was using it.
Unmount any mounted partitions of the target disk first:

```bash
sudo umount /dev/sdX*
```

## What's next

* For provisioning many machines at once over the network, see
  [bty via netboot](bty-netboot-pc.md) and the in-depth
  [bty-web container guide](../walkthrough-server-docker.md).
* For the full CLI surface, see [Reference](../reference.md).
* For how the live env works under the hood, see
  [Concepts](../concepts.md).
