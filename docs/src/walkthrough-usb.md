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
| A **build host** with KVM | The image is baked inside QEMU. Any Linux box with `/dev/kvm` works; WSL2 with KVM enabled also works. |
| A **USB stick**, 16 GiB or larger | The image is ~12 GiB raw and includes a 9 GiB exFAT partition for your images. |
| A **target machine** with a free disk | This is the box that will get flashed. UEFI or legacy BIOS, x86_64. |
| A **system image** to flash | `.qcow2`, raw `.img`, or `.img.zst`. Any cooked OS image of yours; bty doesn't ship one. |

The build host runs Debian 12+ or Ubuntu 24.04+. Other Linux distros
work if you can install the equivalents of `qemu-system-x86`,
`qemu-utils`, `genisoimage`, `zstd`, and `pipx`. There's a one-shot
install script for Debian-family at
[`scripts/install-dev-deps.sh`](https://github.com/safl/bty/blob/main/scripts/install-dev-deps.sh).

## Step 1: Build the USB image

From a checkout of the bty repo:

```bash
make media-deps           # one-time: pipx installs cijoe
make build VARIANT=usb    # ~20 minutes with KVM
```

What this does: drives a Debian 13 cloud-image through cloud-init in
QEMU, partitions the disk into a 3 GiB Debian root + a 9 GiB exFAT
`BTY_IMAGES` volume, installs the bty CLI + TUI into the root, and
emits an `.img.zst` distributable.

When it finishes:

```text
~/system_imaging/disk/
  bty-usb-x86_64.img.zst         <- the file you'll write to the stick
  bty-usb-x86_64.img.zst.sha256
```

```{note}
For a recorded walkthrough of the build, see
[`docs/asciinema/usb-build.sh`](https://github.com/safl/bty/blob/main/docs/asciinema/usb-build.sh).
```

## Step 2: Write the image to a USB stick

**Identify the device first** — this step is destructive:

```bash
lsblk
```

Find the USB stick. It'll typically show as `sda` or `sdb` and have
the size of your stick. **Do not** confuse it with your laptop's
internal disk.

Decompress + write in one pipeline:

```bash
zstd -d --stdout ~/system_imaging/disk/bty-usb-x86_64.img.zst | \
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
macOS, or Windows. From any host:

::::{tab-set}

:::{tab-item} Linux
```bash
sudo mkdir -p /mnt/bty
sudo mount /dev/disk/by-label/BTY_IMAGES /mnt/bty
sudo cp /path/to/my-image.qcow2 /mnt/bty/
sudo umount /mnt/bty
```
:::

:::{tab-item} macOS
The stick auto-mounts at `/Volumes/BTY_IMAGES`. Drag images in via
Finder, or:
```bash
cp /path/to/my-image.qcow2 /Volumes/BTY_IMAGES/
diskutil unmount /Volumes/BTY_IMAGES
```
:::

:::{tab-item} Windows
The stick gets a drive letter (typically `D:` or `E:`). Copy images
in via Explorer.
:::

::::

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

The TUI shows two panes:

| Left pane | Right pane |
|---|---|
| **Images** found on `BTY_IMAGES` | **Disks** detected on the target |

Tab between panes, arrow keys to navigate, `Enter` to select. Once
you've picked an image and a target disk:

| Key | What happens |
|---|---|
| `F` | Show the flash plan (image format, target size, validation) |
| `Enter` (in the modal) | Confirm and run the flash |
| `q` | Cancel any modal / quit the TUI |

A status modal streams the result. When it says **`flash complete`**,
the image bytes are on the target disk.

```{image} _static/screenshot-tui-flashing.png
:alt: bty-tui mid-flash, showing the progress modal
:width: 720px
:align: center
```

```{note}
Without root the TUI launches in **read-only mode** — you can browse
images and disks, but `F` refuses with a status message. Use
``sudo bty-tui`` if you need to flash.
```

## Step 5b: Flash with `bty` (scriptable)

If you'd rather drive the flash from a shell — e.g. you want to
script a fleet of identical boxes — the same operations are
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
    --provision none \
    --dry-run

# 4. Run for real (requires root)
sudo bty flash \
    --image  /mnt/BTY_IMAGES/my-image.qcow2 \
    --target /dev/sda \
    --provision none \
    --yes
```

`--dry-run` prints the flash plan and validates without writing.
`--yes` is the explicit consent token for the destructive write —
`bty flash` refuses to do anything without one or the other, so you
never accidentally wipe a disk.

`--provision` controls what happens after the bytes land:

| `--provision` | What it does |
|---|---|
| `none` | Just write the image. Target boots into whatever the image was cooked with. |
| `cloud-init` | Drop a NoCloud `user-data` seed onto the freshly-flashed rootfs. Target self-configures on first boot. Requires `--user-data ./userdata.yaml`. |
| `cijoe` | Mount the rootfs, run a `cijoe` workflow against it. Useful for one-off image tweaks (drop SSH keys, set hostname, etc.). Requires `--cijoe-workflow ./workflow.yaml`. |

## Step 6: Reboot

Power-cycle the target without the USB stick. The newly-flashed disk
should boot the OS you wrote.

If it doesn't, see **Troubleshooting** below.

## Troubleshooting

### The target won't boot from USB

* Confirm the stick is bootable on a different machine.
* Check the target's BIOS/UEFI for "secure boot" — bty's live image
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

## What's next

* For provisioning many machines at once over the network, see
  [Walkthrough: server appliance](walkthrough-server.md) (todo).
* For the full CLI surface, see [Reference](reference.md).
* For how the live env works under the hood, see [Concepts](concepts.md).
