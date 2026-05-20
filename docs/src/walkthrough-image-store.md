# Walkthrough: persistent state (survive appliance reflashes)

The bty-server appliance ships as a single rootfs image
(`bty-server-x86_64.img.gz`). Out of the box, all bty state lives on
the disk you flashed, under `/var/lib/bty`: the image cache
(`cache/`), the netboot artefacts (`boot/`), the uploaded images
(`images/`), and bty-web's SQLite database (`state.db`, which holds
the machine inventory, catalog, and boot-policy assignments). That's
fine for a first-look install, but it means **reflashing the
appliance erases all of it** -- the operator re-downloads every
image and re-inventories every machine.

This walkthrough moves `/var/lib/bty` onto a **separate disk** with
`bty-state-migrate`. After the one-time setup, a reflash upgrades or
repairs the OS (and the `bty` venv, which stays on the rootfs at
`/opt/bty/venv`) while the disk preserves the data: the new appliance
auto-detects the labelled disk on boot, mounts it at `/var/lib/bty`,
and bty-web comes back with its images, netboot artefacts, and
machine inventory intact -- no re-downloading, no re-inventorying.

## Prerequisites

- A running bty-server appliance (>= 0.22.17 for the whole-state
  layout; earlier versions mounted only `images/`).
- A second physical disk or virtual volume attached to the machine,
  not currently in use. Any size; ext4 will be created on it.
- SSH access as the `odus` admin user (passwordless sudo per the
  appliance defaults).

## One-time setup

SSH in and identify the second disk (anything that isn't the rootfs):

```bash
ssh odus@<bty-server-ip>
lsblk -d -e7,11 -o NAME,SIZE,MODEL,MOUNTPOINTS
```

Typical output on a NUC-class box with an NVMe rootfs plus a SATA
state disk:

```text
NAME    SIZE MODEL                MOUNTPOINTS
nvme0n1 238G CT250P2SSD8          (rootfs partitions)
sda     931G Samsung_SSD_870_EVO
```

Run the migrate helper against the chosen device:

```bash
sudo bty-state-migrate /dev/sda
```

The helper:

1. Prints the planned operations and prompts `Type "yes" to continue:`.
2. Stops `bty-web.service` for the duration (so `state.db` is quiescent).
3. Wipes `/dev/sda`, creates a GPT label + single partition, and
   formats it ext4 with label `BTY_IMAGE_STORE`.
4. Copies the **whole** `/var/lib/bty` (cache + boot + images +
   state.db + workflows) onto the new disk and verifies the copy.
5. Mounts the disk at `/var/lib/bty`, then removes the now-redundant
   rootfs copy to reclaim rootfs space.
6. Ensures the `LABEL=BTY_IMAGE_STORE` line is in `/etc/fstab` (it is
   -- baked in at appliance build time) so subsequent boots auto-mount.
7. Restarts `bty-web.service`.

Verify:

```bash
findmnt /var/lib/bty
# TARGET        SOURCE    FSTYPE OPTIONS
# /var/lib/bty  /dev/sda1 ext4   rw,relatime

sudo df -h /var/lib/bty     # the big disk, with your cache + images
```

Note: `/var/lib/bty` is mode `0750` owned by `bty:bty`, so a plain
`df -h` run as `odus` silently omits it (can't `statfs` it) -- use
`sudo`, or query the path directly.

## Day-2: reflashing the appliance

To upgrade or repair the appliance:

1. Power off the machine.
2. Reflash `bty-server-x86_64.img.gz` onto the **rootfs disk only**
   (do NOT touch the state disk). Any raw-image writer works.
3. Boot.

The new appliance's `/etc/fstab` (baked in) carries:

```text
LABEL=BTY_IMAGE_STORE /var/lib/bty ext4 nofail,x-systemd.device-timeout=10s 0 2
```

systemd finds the labelled disk and mounts it at `/var/lib/bty`;
`bty-web.service` is ordered after that mount (`After=var-lib-bty.mount`),
so by the time it starts, the cache, netboot artefacts, AND the
machine inventory are exactly where they were before the reflash. No
operator action required -- and crucially, no re-inventorying.

## Day-2: moving the state to a different machine

The disk is portable: unplug it, plug it into another bty-server box,
reboot -- the same `LABEL=BTY_IMAGE_STORE` match auto-mounts at
`/var/lib/bty`. Because the whole state dir lives on the disk now
(including `state.db`), the machine inventory + assignments travel
**with** the disk.

## What if no labelled disk is present?

The `nofail` mount option means the appliance still boots and bty-web
falls back to the rootfs `/var/lib/bty` (the baked venv at
`/opt/bty/venv` is always on the rootfs, so bty-web starts either
way). To move to a separate disk later, run `sudo bty-state-migrate
/dev/sdX` -- it copies the current state onto the disk before
switching over.

## Troubleshooting

**`bty-state-migrate` refuses to run:**
- It declines the rootfs disk (would self-destruct).
- It declines a disk with any partition currently mounted.
- It no-ops if `/var/lib/bty` is already its own mount.
- Unmount first (`umount /dev/sdX*`), or pick a different disk.

**The fstab line is present but the disk isn't mounting on boot:**
- `systemctl daemon-reload` then `mount /var/lib/bty` to test manually.
- `journalctl -b -u var-lib-bty.mount` for systemd's view.
- A slow/USB disk may exceed `x-systemd.device-timeout=10s`; bump it
  (e.g. `30s`) in `/etc/fstab` for that hardware.

**Re-using a disk that already has `LABEL=BTY_IMAGE_STORE`:**
- Plug it in and boot -- the fstab line matches by label, data is
  preserved. Running `bty-state-migrate` on it **wipes** it; only do
  that for a clean slate.
- A disk prepared by a pre-0.22.17 appliance held only the image
  files (mounted at `.../images`); re-run `bty-state-migrate` to
  re-lay it out for the whole-state layout.
