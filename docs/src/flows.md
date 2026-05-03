# Flows

The two end-to-end paths bty supports.

## Direct flash (CLI / TUI)

Used for ad-hoc provisioning of a single box, with no infrastructure on
the network.

1. Operator boots the target machine from the bty USB live image
   (built by `bty-media`).
2. The live environment auto-starts `bty-tui`, or the operator drops to
   a shell and runs `bty` directly.
3. Operator selects an image (sourced from the USB stick itself), a
   target disk (block device on the booted machine), and a provisioning
   mode.
4. `bty flash` writes the image, applies the provisioning mode, and
   reports success.
5. Operator removes the USB stick and reboots; the target boots into the
   freshly-flashed image.

The whole flow runs offline. No network, no server, no MAC registration.

## Network flash (web)

Used for fleet-managed provisioning, where targets are reflashed on
schedule, on demand, or on failure.

1. Operator stands up the bty server image (`dd` to disk, boot, run the
   first-boot wizard once via the web UI). After that, no SSH is
   needed.
2. Operator assigns `MAC -> image + provisioning mode` in the web UI.
3. Target machine PXE-boots; iPXE chains into the bty live environment
   served over HTTP by `bty-web`.
4. The live environment contacts `bty-web`, fetches a per-MAC bootstrap,
   flashes the target disk, and applies the provisioning mode.
5. `bty-web` rewrites the per-MAC iPXE configuration to "boot from local
   disk" so the next boot does not reflash.
6. If the assigned provisioning mode is `cijoe` (online), `bty-web`
   triggers the CIJOE workflow against the booted target and records
   the post-workflow state as the machine's known-good baseline.

Both BIOS and UEFI clients are supported via iPXE.
