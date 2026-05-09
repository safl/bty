# Flows

The four end-to-end paths bty supports. Pick by what infrastructure
you have:

- **Direct flash** - one-off provisioning, no server, USB live stick
  with the catalog baked onto its `BTY_IMAGES` partition.
- **USB + network catalog** - USB live stick boots a target as in the
  direct path, but the catalog comes from a remote `bty-web` instance
  (commonly the `ghcr.io/safl/bty-web` Docker container on a teammate's
  workstation). Same flash mechanics, shared catalog. No PXE.
- **Interactive PXE flash** - server is up, operator picks an image
  from the TUI on first PXE contact (default for unknown MACs).
- **Server-driven PXE flash** - fleet provisioning, machines reflash
  themselves on schedule / on demand / on failure.

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

## USB + network catalog (`bty-tui --server URL`)

A middle shape between the strictly-offline direct flash and the
PXE-driven flows. The operator boots from the same USB live stick
but points the TUI at a network-shared `bty-web` for the catalog.
Useful for a small team that wants one place to keep cooked images
without standing up the appliance + PXE stack.

1. Someone (operator's workstation, a homelab server, a dev box)
   runs `bty-web`. The lowest-friction shape is the published
   container:

   ```bash
   docker run -d -p 8080:8080 -v bty-data:/var/lib/bty \
     ghcr.io/safl/bty-web:latest
   ```

   Cooked images get dropped into the volume; they show up in the
   `/images` endpoint. The bare-metal `bty-server` appliance also
   serves `/images` and works identically as a catalog source -
   any `bty-web` instance does.

2. Operator boots a target from the bty USB live stick and runs
   `bty-tui --server http://<host>:8080` (either inline at the
   tty1 prompt or by relaunching the auto-started TUI with that
   flag).

3. The TUI populates its image pane from `GET /images` on the
   server, the operator picks a target disk on the local machine
   and an image from the remote catalog, and confirms the flash.
   Image bytes stream from `GET /images/{name}` directly through
   `zstd -d | dd` to the target disk - no temp file.

4. On completion the operator removes the stick and reboots; the
   target boots into the freshly-flashed image. The server has no
   per-MAC record (this isn't PXE), so no follow-up state to
   manage.

No PXE, no DHCP-proxy, no L2 broadcasts. The container can
therefore live anywhere reachable - operator's laptop, an EC2
instance, anywhere with HTTP. The cost: the operator still has to
plug in the USB stick and stand at the target.

## Interactive PXE flash (`boot_policy=tui`)

The "bty-on-a-USB but over the network" path. Default behaviour for
any MAC the server has never seen, so onboarding a new box needs
zero per-MAC configuration.

1. Operator stands up the bty server image (`dd` to disk, boot).
   Default credentials are `bty / bty` (web UI) and `odus / odus`
   (SSH); rotate with `passwd` and activate the dnsmasq proxy-DHCP
   block from the Settings page.
2. A target machine PXE-boots on the same segment for the first time.
   `bty-web` auto-discovers the MAC, creates a `Machine` record with
   `boot_policy=tui`, and serves the iPXE-tui template
   (`ipxe_tui.j2`).
3. The target chains into the bty live env with
   `bty.mode=interactive bty.server=URL bty.mac=MAC` on the kernel
   cmdline. `bty-flash-on-boot.service` sees the interactive flag
   and short-circuits; `bty-tui-on-tty1.service` takes over tty1 in
   place of the agetty and launches
   `bty-tui --server URL --mac MAC`.
4. The TUI fetches the catalog from `GET /images`, the operator
   picks an image and a target disk, and confirms the flash. Image
   bytes stream from `GET /images/{name}` straight through
   `zstd -d | dd` to the target disk - no temp file, no intermediate
   download.
5. On success the TUI `POST`s `/pxe/{mac}/done` so `last_flashed_at`
   updates server-side. The next reboot chains the TUI again unless
   the operator flips `boot_policy` (typically to `local` once the
   target is happy with what it's running).

This flow is also useful for the operator who just wants a
one-off remote flash without preparing a USB stick: any unknown
MAC on the segment becomes a TUI session reachable via IPMI / serial
console.

## Server-driven PXE flash (`boot_policy=flash`)

Used for fleet-managed provisioning, where targets are reflashed on
schedule, on demand, or on failure.

1. Server appliance is already up (same setup as the interactive
   flow above).
2. Operator assigns `MAC -> image + provisioning mode + boot policy`
   in the web UI. `boot_policy=flash` arms the auto-flash;
   `boot_policy=local` lets the target PXE-boot through to its own
   disk untouched.
3. Target machine PXE-boots; iPXE chains into the bty live
   environment served over HTTP by `bty-web`.
4. The live env's `bty-flash-on-boot.service` fetches the assigned
   image from `GET /images/{name}`, runs `bty flash`, applies the
   provisioning mode, and `POST`s `/pxe/{mac}/done` to update
   `last_flashed_at`. Then it reboots automatically.
5. The next reboot still chains the live env unless the operator
   flips the machine to `boot_policy=local`. Per-job CI cadences
   that want every boot to reflash leave the policy on `flash`.
6. If the assigned provisioning mode is `cijoe-online`, `bty-web`
   triggers the CIJOE workflow against the booted target and records
   the post-workflow state as the machine's known-good baseline.

Both BIOS and UEFI clients are supported via iPXE.
