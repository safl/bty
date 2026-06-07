# bty on a Ventoy USB stick

[Ventoy](https://www.ventoy.net/) is a multi-boot USB tool: format a
stick once, then drop ISO files onto its data partition and pick from a
menu at boot time. bty's USB image is published as a plain
`.iso.gz` (gzipped ISO9660 hybrid), which is what Ventoy expects.

## Why use this combo

- Carry one stick with bty alongside your other rescue / install ISOs
  instead of dedicating a stick to bty.
- Swap bty versions without re-flashing: drop a new `.iso` next to the
  old one, pick from the Ventoy menu.

## Steps

1. **Install Ventoy on a USB stick.** Follow the
   [upstream guide](https://www.ventoy.net/en/doc_start.html). One-time;
   wipes the stick.
2. **Grab the bty ISO.** From the
   [latest release](https://github.com/safl/bty/releases/latest):
   ```bash
   curl -fLO https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64-v$VERSION.iso.gz
   gunzip bty-usb-x86_64-v$VERSION.iso.gz
   ```
   (Or download the `.iso` directly from the release page if your browser
   doesn't auto-decompress.)
3. **Copy the ISO onto the Ventoy data partition.** Mount the larger of
   the two partitions on your Ventoy stick and drop the `.iso` in any
   subdirectory.
4. **Boot the target from the Ventoy stick** and pick the bty entry from
   the Ventoy menu. The bty live env comes up exactly as it would from a
   dedicated stick, with `bty` running on tty1.

## Caveats

- **First-boot delay (~90 s).** On Ventoy + bty's writable `BTY_IMAGES`
  partition, `bty-usb-grow.service` orders after `BTY_IMAGES.device` and
  systemd waits the default device timeout before giving up on the
  bind-mount. Harmless: the wizard appears once the timeout elapses.
  Targeted fix tracked separately.
- **No `BTY_IMAGES` persistence inside Ventoy.** Ventoy presents the bty
  ISO read-only, so images you copied via the standalone-stick
  `BTY_IMAGES` workflow are not visible. Use the wizard's `[d] default`
  catalog or pass `--catalog SOURCE` to pull images from a bty-web server
  / GHCR / a TOML on disk instead.
