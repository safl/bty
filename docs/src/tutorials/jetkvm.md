# bty via JetKVM (KVM-over-IP)

[JetKVM](https://jetkvm.com/) is a compact KVM-over-IP dongle with a
web UI for video, keyboard / mouse, and **virtual media** (mounts an
uploaded ISO to the target as a USB device). The bty workflow is
identical to [PiKVM](pikvm.md) -- the only difference is the host UI.

## Why use this combo

- Tiny remote-access dongle for a small lab / home-rack: bolt one onto a
  box, drive its boot media remotely.
- Same keyboard-only bty wizard you'd run from a stick, accessible over
  the browser.

## Steps

1. **Grab the bty ISO** on the workstation that talks to the JetKVM web
   UI:
   ```bash
   curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64-v$VERSION.iso.gz
   gunzip bty-usb-x86_64-v$VERSION.iso.gz
   ```
2. **Upload + mount.** In the JetKVM UI, open the **Virtual Media** /
   storage panel, upload the `.iso`, and attach it as a USB / CD-ROM
   device to the target.
3. **Boot the target from virtual media.** Power-cycle or reset via the
   JetKVM power control; pick the virtual device in firmware boot menu.
4. **Run the wizard.** bty on tty1, keyboard-driven, same as a physical
   stick.

## Caveats

- **Catalog source.** As with PiKVM, the virtual-media ISO is read-only,
  so `BTY_IMAGES` is empty. Use `--catalog` against a bty-web instance
  or fall back to the `[d] default` GHCR-streaming catalog.
- **Stream quality vs flash speed.** The JetKVM stream is for operator
  input; the actual image bytes flow over the target's NIC.
