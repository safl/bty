# Quickstart

Two steps to a working bty-lab: use bty's USB flasher to install a
clean [NOSI](https://github.com/safl/nosi) image on a Linux host,
then on that host stand up the bty-lab server.

## bty via USB

Write the bty USB ISO to a stick, boot the target from it, pick a
NOSI image -- e.g. `debian-13-headless` for a minimal Debian server
without a desktop -- and flash:

```bash
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso
sudo dd if=bty-usbboot-pc-x86_64.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

Replace `/dev/sdX` with your USB stick (check `lsblk` first). Plug
into the target, boot from USB, run the wizard.

Full step-by-step (sha256 check, BIOS boot keys, troubleshooting):

- [bty via bty-usbboot-pc](tutorials/bty-usbboot-pc.md) -- x86 target
- [bty via bty-usbboot-rpi](tutorials/bty-usbboot-rpi.md) -- Raspberry Pi target

## Deploy the bty-lab server

Once the host is up, run the server-side deploy. The tutorial
covers both storage layouts (one disk for everything, or a
dedicated secondary drive for state):

- [bty-lab server setup](tutorials/bty-lab-deploy.md)

## Next steps

The lab is up and you've flashed your first target. From here:

- **Alternate delivery shapes** for the operator-side flash:
  - [bty via Ventoy](tutorials/bty-ventoy.md) -- multi-boot USB
    carrying the bty boot env plus pre-staged target images.
  - [bty via BMC / OOB-MGMT](tutorials/bmc.md) -- piKVM / JetKVM /
    server BMCs that mount the bty ISO as virtual media.
- **Network-flash a fleet** from the server you just deployed:
  - [bty via netboot](tutorials/bty-netboot-pc.md).
- **How the pieces fit** -- read these once when you want to know
  why bty-web holds no image bytes, how catalogs travel, and what
  the container deploy actually wires:
  - [Persistent state and where image bytes live](walkthrough-image-store.md)
  - [Walkthrough: catalogs](walkthrough-catalog.md)
  - [Walkthrough: bty-web container deploy](walkthrough-server-docker.md)
- **Day 2** -- backup, upgrade, move to a new host:
  [Operations](operations.md).
