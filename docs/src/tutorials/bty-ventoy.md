# bty via bty-ventoy

[Ventoy](https://www.ventoy.net) lets one USB stick boot any of dozens
of `.iso` files via a menu at power-on. The Ventoy data partition
doubles as exFAT scratch space, which bty's live env auto-discovers and
uses as the image catalog -- one stick carries the bty boot env *and*
your pre-built target images.

## Step 1: Install Ventoy on a USB stick

```bash
# DESTRUCTIVE: this wipes /dev/sdX. Verify the device with lsblk first.
sudo Ventoy2Disk.sh -i /dev/sdX
```

Ventoy's installer is upstream; their docs cover Windows + Linux +
macOS. After install, the stick has two partitions: a small EFI /
bootloader partition and a large exFAT data partition labelled `Ventoy`
(the rest of the stick).

## Step 2: Stage the bty USB ISO on the Ventoy partition

```bash
# Discover the current release version + download the USB ISO. The
# release.toml URL always redirects to the newest tag, so this picks
# up whatever's latest. For a specific version, replace `latest` with
# a tag like v0.38.0.
VERSION=$(curl -fsSL https://github.com/safl/bty/releases/latest/download/release.toml \
  | grep -oP 'version = "\K[^"]+')
curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usb-x86_64-v$VERSION.iso

sudo mount /dev/disk/by-label/Ventoy /mnt
sudo cp bty-usb-x86_64-v$VERSION.iso /mnt/
```

The `.iso` can sit at the root of the Ventoy partition or in any
subdirectory. Ventoy's menu lists every `.iso` it finds anywhere on the
partition.

## Step 3: Stage your pre-built images in `bty-images/`

```bash
sudo mkdir -p /mnt/bty-images

# Supported extensions:
# *.img.gz / *.img.zst / *.img.xz / *.img.bz2 / *.qcow2 / *.img / *.iso / *.iso.gz
sudo cp /path/to/nosi-debian-sysdev-x86_64.img.gz /mnt/bty-images/
sudo cp /path/to/nosi-fedora-sysdev-x86_64.img.gz /mnt/bty-images/

sudo umount /mnt
```

The discovery service accepts either layout:

1. **Recommended**: a `bty-images/` subfolder at the partition root
   with your `.img.gz` / `.qcow2` / `.iso.gz` files inside. Keeps
   pre-built images visually separate from the `.iso` files Ventoy
   boots.
2. **Quick-drop**: the same files at the partition root, alongside
   `bty-usb-x86_64-v$VERSION.iso`. Less tidy but supported.

The service tries the subfolder first, then falls back to the root.
First match with at least one supported file (`.img*` / `.qcow2` /
`.iso*`) wins, gets bind-mounted at `/var/lib/bty/images`, and `bty`
picks it up.

## Step 4: Boot the target

1. Plug the Ventoy stick into the target machine.
2. Power-cycle the target, enter the BIOS/UEFI boot menu, pick the
   Ventoy stick.
3. Ventoy's menu appears. Pick `bty-usb-x86_64-v$VERSION.iso`.
4. bty live env boots. `bty-images-discover.service` scans the attached
   partitions, finds `bty-images/` on the Ventoy stick, and
   bind-mounts it at `/var/lib/bty/images`.
5. `bty` opens on tty1 with your image catalog already populated.

## Troubleshooting

If `bty` shows "No images in the catalog yet":

1. Press `Alt+F2` for a root shell on the alternate VT.
2. Run `journalctl -u bty-images-discover` to see exactly which
   partitions were scanned and which it skipped (and why).
3. Confirm the Ventoy partition's filesystem is exFAT (NTFS isn't
   probed): `lsblk -f`.
4. Confirm the folder is exactly `bty-images/` at the partition root
   (not `Bty-Images/`, not nested).
5. `Alt+F1` returns to `bty`; press `r` once you've fixed the layout
   to re-scan.

## Caveat: first-boot delay (~90s)

On Ventoy + bty's writable `BTY_IMAGES` partition,
`bty-usb-grow.service` orders after `BTY_IMAGES.device` and systemd
waits the default device timeout before giving up on the bind-mount.
Harmless: the wizard appears once the timeout elapses. Targeted fix
tracked separately.
