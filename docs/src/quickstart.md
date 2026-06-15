# Quickstart

Two steps to a working bty-lab:

1. **Bootstrap a host** -- use bty's own USB flasher to install a
   [NOSI](https://github.com/safl/nosi) image onto a Linux box.
   Result: a clean system to land the lab on.
2. **Deploy bty-lab** -- on that host, run `bty-lab deploy` to
   stand up the network-flash server (`bty-web` + `withcache`
   via docker-compose).

## 1. Bootstrap: flash a NOSI image

Write the bty USB ISO to a stick, boot the target from it, pick a
NOSI image, flash:

```bash
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso
sudo dd if=bty-usbboot-pc-x86_64.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

Replace `/dev/sdX` with your USB stick (check `lsblk` first). Plug
into the target, boot from USB, run the wizard.

Full step-by-step (sha256 check, BIOS boot keys, troubleshooting):

- [bty via bty-usbboot-pc](tutorials/bty-usbboot-pc.md) -- x86 target
- [bty via bty-usbboot-rpi](tutorials/bty-usbboot-rpi.md) -- Raspberry Pi target

## 2. Deploy bty-lab on the freshly-installed host

Once the host is up, run the server-side deploy. The tutorial
covers both storage layouts (one disk for everything, or a
dedicated secondary drive for state):

- [bty-lab server setup](tutorials/bty-lab-deploy.md)

After deploy, point PXE clients at the new server: see
[bty via netboot](tutorials/bty-netboot-pc.md).
