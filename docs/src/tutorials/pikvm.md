# bty via PiKVM (KVM-over-IP)

[PiKVM](https://pikvm.org/) is an open-source KVM-over-IP appliance:
video capture, USB HID emulation, and **virtual storage** (the PiKVM
exposes itself to the target as a USB mass-storage device serving an
ISO file you upload via the web UI). That makes it a natural way to
boot bty on a remote target that has no console and no physical stick.

## Why use this combo

- Reflash a colocated / remote box without on-site hands.
- Reuse the same bty wizard you'd use locally: bty's TUI is keyboard-only
  and renders cleanly over the PiKVM video stream.

## Steps

1. **Grab the bty ISO** (already decompressed) on the workstation that
   talks to the PiKVM. Match the format the PiKVM upload form expects --
   `.iso`, not `.iso.gz`:
   ```bash
   curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64-v$VERSION.iso.gz
   gunzip bty-usb-x86_64-v$VERSION.iso.gz
   ```
2. **Upload to the PiKVM.** In the PiKVM web UI, open the **Mass Storage
   Drive** menu, upload the `.iso`, set the mode to **CD-ROM** (or USB
   stick), and click **Connect**.
3. **Reboot the target into the PiKVM virtual media.** From the PiKVM
   Power menu, reset the host; in firmware setup pick the PiKVM virtual
   CD-ROM as the boot device (one-time boot menu is usually easiest).
4. **Drive the wizard via the PiKVM stream.** bty comes up on tty1; pick
   image, pick disk, confirm flash, reboot. All keyboard-only.

## Caveats

- **Network catalogs preferred.** The PiKVM serves a read-only image, so
  the wizard's local-images flow can't see anything you'd normally drop
  onto `BTY_IMAGES`. Use `[d] default` (streams from GHCR via
  `oras://`) or point the wizard at a `bty-web` instance via
  `--catalog http://bty-server:8080/catalog.toml`.
- **Image fetch goes over the target's NIC**, not the PiKVM's: the
  target needs a working DHCP lease + outbound HTTPS (or LAN reach to
  your bty-web) at flash time.
