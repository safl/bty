# Flows

The two end-to-end paths bty supports.

## Direct flash (CLI / TUI)

Used for ad-hoc provisioning of a single box, with no infrastructure on
the network.

1. Operator boots the target machine from the bty USB live image
   (built by `bty-media`).
2. The live environment auto-logins as root on `tty1`. The operator
   runs `bty-tui` for an interactive flow, or invokes `bty` directly
   from the shell.
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

1. Operator stands up the bty server image (`dd` to disk, boot). The
   appliance ships with default credentials (`bty / bty` for the web
   UI, `odus / odus` for SSH); operator rotates with `passwd` and
   activates the dnsmasq proxy-DHCP block via the Settings page.
2. Operator assigns `MAC -> image + provisioning mode + boot policy`
   in the web UI. ``boot_policy=flash`` arms the network flash;
   ``boot_policy=local`` (default) lets the target PXE-boot through
   to its own disk untouched.
3. Target machine PXE-boots; iPXE chains into the bty live
   environment served over HTTP by `bty-web` when the assignment
   says ``boot_policy=flash``.
4. The live environment fetches the assigned image from
   ``GET /images/{name}``, flashes the target disk, applies the
   provisioning mode, and `POST`s ``/pxe/{mac}/done`` to update
   ``last_flashed_at``.
5. The next reboot still chains the live env unless the operator
   flips the machine to ``boot_policy=local``. Per-job CI cadences
   that want every boot to reflash leave the policy on ``flash``.
6. If the assigned provisioning mode is `cijoe-online`, `bty-web`
   triggers the CIJOE workflow against the booted target and records
   the post-workflow state as the machine's known-good baseline.

Both BIOS and UEFI clients are supported via iPXE.
