# Quickstart

## Flash OS images via USB

Put bty on a USB stick with `curl | dd`. The stick boots any x86 box
into the bty wizard, which flashes OS images onto the target's local
disk. The default catalog is the [NOSI](https://github.com/safl/nosi)
image set (e.g. pick `debian-13-headless` for a minimal Debian
server), but bty accepts any URL- or oras-served image, so swap in
your own catalog when you have one.

```bash
curl -fL https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso \
  | sudo dd of=/dev/sdX bs=4M conv=fsync
```

Replace `/dev/sdX` with your USB stick (check `lsblk` first), plug into the target, boot from USB, and run the wizard; for the full step-by-step (sha256 check, BIOS boot keys, troubleshooting) see [bty via bty-usbboot-pc](tutorials/bty-usbboot-pc.md) for x86 or [bty via bty-usbboot-rpi](tutorials/bty-usbboot-rpi.md) for Raspberry Pi.

## Deploy bty-server

One command on a Linux host:

```bash
sudo uvx bty-lab deploy /opt/bty
```

That sets up `bty-web` + `withcache` via docker-compose. Unlocks
PXE-boot for a fleet, image-byte caching across repeat flashes,
and hosting a custom catalog (your own image-builder, an internal
mirror, ...) on top of the USB flow. State lives under
`/opt/bty/data/`.

For a fleet you typically want state on a dedicated drive so an OS
reflash leaves the lab intact; prepare the drive first, then point
`--data-dir` at the mount:

```bash
sudo wipefs -a /dev/sdX
sudo mkfs.ext4 -L bty-data /dev/sdX
sudo mkdir -p /srv/bty
UUID=$(sudo blkid -o value -s UUID /dev/sdX)
echo "UUID=$UUID  /srv/bty  ext4  defaults,noatime,nofail  0 2" | sudo tee -a /etc/fstab
sudo mount -a

sudo uvx bty-lab deploy /opt/bty --data-dir /srv/bty
```

Full tutorial: [bty-lab server setup](tutorials/bty-lab-deploy.md).

## Next steps

The lab is up and you've flashed your first target. From here:

- **Alternate delivery shapes** for the operator-side flash:
  - [bty via Ventoy](tutorials/bty-ventoy.md): multi-boot USB
    carrying the bty boot env plus pre-staged target images.
  - [bty via BMC / OOB-MGMT](tutorials/bmc.md): piKVM / JetKVM /
    server BMCs that mount the bty ISO as virtual media.
- **Network-flash a fleet** from the server you just deployed:
  - [bty via netboot](tutorials/bty-netboot-pc.md).
- **How the pieces fit.** Read these once when you want to know
  why bty-web holds no image bytes, how catalogs travel, and what
  the container deploy actually wires:
  - [Persistent state and where image bytes live](walkthrough-image-store.md)
  - [Walkthrough: catalogs](walkthrough-catalog.md)
  - [Walkthrough: bty-web container deploy](walkthrough-server-docker.md)
- **Day 2** (backup, upgrade, move to a new host): see
  [Operations](operations.md).
