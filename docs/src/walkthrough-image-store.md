# Walkthrough: persistent image store (survive appliance reflashes)

The bty-server appliance ships as a single rootfs image
(`bty-server-x86_64.img.gz`). Out of the box, all state lives on
the same disk you flashed: bty-web's SQLite database, the iPXE +
TFTP boot artefacts, and the image cache itself
(`/var/lib/bty/images`). That's fine for a first-look install, but
it means **reflashing the appliance erases the image cache** -- the
operator pays the bandwidth + time to re-download every catalog
entry from upstream.

This walkthrough sets up a **persistent image store** on a separate
disk. After the one-time setup, reflashing the bty-server appliance
preserves the image cache: the new appliance auto-detects the
labelled disk on boot, mounts it at `/var/lib/bty/images`, and
bty-web finds the cached images exactly where it left them.

## Prerequisites

- A running bty-server appliance (any version >= 0.10.0).
- A second physical disk or virtual volume attached to the machine,
  not currently in use. Any size; ext4 will be created on it.
- SSH access to the appliance as the `odus` admin user (passwordless
  sudo per the appliance defaults).

## One-time setup

SSH in:

```bash
ssh odus@<bty-server-ip>
```

Identify the second disk. Anything that isn't the rootfs is fair
game:

```bash
lsblk -d -e7,11 -o NAME,SIZE,MODEL,MOUNTPOINTS
```

Typical output on a NUC-class box with a 256 GB rootfs SSD plus a
1 TB image store:

```text
NAME    SIZE MODEL                MOUNTPOINTS
nvme0n1 238G CT250P2SSD8          (rootfs partitions)
sda     931G Samsung_SSD_870_EVO
```

Run the init helper against the chosen device:

```bash
sudo bty-image-store-init /dev/sda
```

The helper:

1. Prints the planned operations and prompts `Type "yes" to continue:`.
2. Stages anything currently sitting in `/var/lib/bty/images` (on
   the rootfs) so the format doesn't lose existing images.
3. Wipes `/dev/sda`, partitions it (GPT, single partition), and
   formats partition 1 as ext4 with label `BTY_IMAGE_STORE`.
4. Mounts the new filesystem at `/var/lib/bty/images`.
5. Restores any staged files onto the new mount.
6. Verifies the appliance's `/etc/fstab` already carries the
   `LABEL=BTY_IMAGE_STORE` line (it does -- baked in at appliance
   build time) so subsequent boots auto-mount the disk.
7. Restarts `bty-web.service` so it sees the now-mounted image
   directory.

After the script exits, verify:

```bash
mount | grep /var/lib/bty/images
# /dev/sda1 on /var/lib/bty/images type ext4 (rw,relatime)

lsblk -no LABEL /dev/sda1
# BTY_IMAGE_STORE
```

`bty-web` is already serving from the new disk. The browser UI at
`http://<ip>:8080/ui/images` lists whatever was on the rootfs
before plus any new uploads.

## Day-2: reflashing the appliance

To upgrade or repair the appliance:

1. Power off the machine.
2. Reflash `bty-server-x86_64.img.gz` onto the **rootfs disk only**
   (do NOT touch the image-store disk). Use `bty tui`, `bty flash`,
   Etcher, Rufus -- any tool that writes a raw image to a single
   disk.
3. Boot the appliance.

The new appliance's `/etc/fstab` (baked in at build time) carries:

```text
LABEL=BTY_IMAGE_STORE /var/lib/bty/images ext4 nofail,x-systemd.device-timeout=10s 0 2
```

systemd finds the labelled disk and mounts it before
`local-fs.target`. By the time `bty-web` starts, the image
directory is populated with the cache from before the reflash. No
operator action required.

## Day-2: moving the image store to a different machine

The disk is portable. Unplug it from one bty-server box, plug it
into another bty-server box, reboot -- the same `LABEL=BTY_IMAGE_STORE`
match auto-mounts. The bty-web database does **not** travel with
the disk (it's at `/var/lib/bty/bty-web.db` on the appliance's
rootfs), so machine-to-image assignments and the boot-policy state
stay with the appliance. Images travel; meta-state stays.

## What if no labelled disk is present?

The `nofail` mount option means the appliance still boots; bty-web
falls back to storing images on the rootfs at the same path. You
get the pre-0.10.0 behaviour without any error spam.

If you decide later to move to a separate disk, run
`sudo bty-image-store-init /dev/sdX` and the script copies the
rootfs-staged images onto the new disk before formatting -- same as
on a fresh install.

## Troubleshooting

**`bty-image-store-init` refuses to format a disk:**
- The script declines the rootfs disk (would self-destruct).
- It declines a disk with any partition currently mounted.
- Unmount first (`umount /dev/sdX*`), or pick a different disk.

**The fstab line was added but the disk isn't mounting on boot:**
- `systemctl daemon-reload` (force systemd to re-parse fstab).
- `mount /var/lib/bty/images` (test the mount manually).
- `journalctl -u var-lib-bty-images.mount` (systemd's view of why
  the mount failed; usually a typo in the fstab line).

**Re-using a disk that already has `LABEL=BTY_IMAGE_STORE` from a
previous appliance:**
- Just plug it in and boot. The fstab line matches by label so the
  data is preserved.
- Running the init script on that disk wipes it. Don't do that
  unless you want a clean slate.

**Multiple disks with `LABEL=BTY_IMAGE_STORE`:**
- systemd picks one (usually the first the kernel enumerated); the
  others sit idle.
- Either re-label one with `e2label /dev/sdX BTY_IMAGE_STORE_BACKUP`,
  or use UUID instead of LABEL in `/etc/fstab` if you want a
  specific disk pinned.
