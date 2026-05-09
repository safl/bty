# Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with three console-script
entry points, plus a sibling appliance-image builder (`bty-media/`).

## How the pieces connect

Three delivery shapes, the same `bty` library at the centre of all
three:

```
   Self-contained USB        USB + network catalog        PXE-driven (no operator)
   (no infra)                (light infra)                (full appliance)

   operator's box            operator's box               operator's workstation
   |                         |                            | browser
   v                         v                            v
 +----------------+      +----------------+            +-----------------+
 |  bty-usb       |      |  bty-usb       |            |  bty-server     |
 |  live env      |      |  live env      |            |  appliance      |
 |                |      |                |            |                 |
 | +------------+ |      | +------------+ |            | +-------------+ |
 | | bty-tui    | |      | | bty-tui    | |    HTTP    | | bty-web     | |
 | | (local     | |      | | --server   +-+----------->+ | iPXE/TFTP/  | |
 | |  catalog)  | |      | |   URL)     | | (catalog)  | | dnsmasq     | |
 | +-----+------+ |      | +-----+------+ |            | +------+------+ |
 +-------+--------+      +-------+--------+            +--------+--------+
         |                       |                              |
         | bty flash             | bty flash                    | iPXE
         | (BTY_IMAGES)          | (image fetched               | chain to
         |                       |   from catalog               | target +
         v                       |   server)                    | flash-on-
   +-----------+                 v                              | boot
   | target    |           +-----------+                        v
   | machine   |           | target    |                  +---------------+
   | local     |           | machine   |                  | target machine|
   | disk      |           | local     |                  | netboot env   |
   +-----------+           | disk      |                  | -> local disk |
                           +-----------+                  +---------------+

                              ^                                  ^
                              | network catalog                  | full PXE
                              | source (one of):                 | server
                              |                                  |
                              | * ghcr.io/safl/bty-web           | bty-server-x86
                              |   (Docker; trial / small team)   | bty-server-rpi
                              | * bty-server appliance           |
                              |   (also serves catalog over HTTP)|
```

The `bty` library implements the flashing logic (`bty.flash`,
`bty.images`, `bty.disks`) consumed by all three flows. `bty-tui` and
`bty-web` are UI shells; `bty-flash-on-boot` is the systemd service
that runs the flash unattended after a PXE boot. Same operations,
different delivery vehicles. The middle shape (`--server URL`) is
where the Docker container fits naturally - a single command on a
workstation gives a small team a shared image catalog without
standing up the appliance.

## `bty` (CLI)

Main command-line interface. The single source of truth for image
inspection, target-disk discovery, flashing, and provisioning. Every
other component is a UI or delivery vehicle for what `bty` does.

Installable on any Linux environment with a sufficient Python runtime:

```bash
pipx install bty-lab
```

## `bty-tui` (Terminal UI)

Terminal UI on top of the same library. Targeted at interactive use from
a live environment where a graphical browser is not appropriate - a
serial console, an SSH session, or a minimal recovery image. Exposes the
same operations as the CLI in a navigable form.

Requires the `tui` extra:

```bash
pipx install "bty-lab[tui]"
```

## `bty-web` (HTTP server + browser UI)

HTTP server with a browser UI, intended to run on the bty server
appliance. Hosts:

- MAC-address-keyed assignment of image and provisioning mode.
- Per-MAC iPXE configuration rendering.
- Bootstrap requests issued by the bty live environment during a
  network flash.
- Online CIJOE orchestration after a target first-boots, with
  per-machine known-good state tracking.

Requires the `web` extra:

```bash
pipx install "bty-lab[web]"
```

State (machine records, MAC <-> image/provisioning assignments, CIJOE
workflow references and run reports, known-good baselines, image
catalog metadata, server settings, sessions) is persisted in a
single SQLite database under the configured `BTY_STATE_DIR`. Backup
or migrate by copying the file.

CIJOE produces a structured report on every workflow run. `bty-web`
captures these reports - both for offline runs (sent back from the live
environment) and online runs (executed by the server itself) - and
exposes them in the UI per machine and per run. Reports are downloadable
in full, so an operator chasing a flaky reflash can inspect the
complete log without leaving the browser.

The runtime is sized for modest x86 hardware: lightweight Python web
framework, no heavy front-end build pipeline, no JVM dependencies.

## `bty-media/` (appliance-image builder)

Sibling directory at the repo root. Not a Python package. Builds four
appliance variants from a shared rootfs overlay:

**USB live image (`usb-x86`).** Bootable USB stick carrying the `bty`
runtime and an exFAT `BTY_IMAGES` partition for cooked images. Operator
plugs it in, boots a target, runs `bty flash` against the local disk.
Self-contained and offline. Direct-flash delivery vehicle.

**Server image, x86_64 (`server-x86`).** Installable disk image that,
when written to a host's disk and booted, runs the bty provisioning
server (`bty-web`, the iPXE/TFTP/HTTP services that PXE clients chain
through, the network-flash live environment those clients boot into,
and a storage layout for the image library). One artifact, ready to
serve a fleet. Boots with the same plymouth bty splash the USB live
env shows; a `/etc/issue` + `/etc/motd` figlet banner takes over on
the console + over SSH.

**Server image, Raspberry Pi 4 / 5 (`server-rpi`).** Same appliance
role, delivered as an SD-card image for arm64. Built by mounting the
upstream Raspberry Pi OS Lite image and customising it in a
`qemu-aarch64-static` chroot. Operator `dd`'s the resulting `.img.zst`
to an SD card and boots a Pi 4 or Pi 5; first-boot ends at the same
`bty / bty` credential as the x86 server image.

**Network-flash live env (`netboot-x86`).** Kernel + initrd + squashfs
trio that PXE clients chain into. Built via Debian's `live-build`. The
chroot ships a `bty-flash-on-boot.service` oneshot that reads its
assignment from `/proc/cmdline`, downloads the assigned image, runs
`bty flash`, signals completion, and reboots.

## `ghcr.io/safl/bty-web` (Docker container)

A multi-arch container (`linux/amd64` + `linux/arm64`) built from
the same `bty-lab[web]` wheel and published to
[GitHub Container Registry](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. Hosts `bty-web` only - **no** dnsmasq,
TFTP, or iPXE proxy-DHCP, because Docker bridge networking cannot
relay L2 broadcasts.

Use cases:

- Trial / kicking-the-tires deploys: `docker run -p 8080:8080
  ghcr.io/safl/bty-web:latest` and the browser UI is up in seconds.
- Network-shared image catalog: a fleet of operators with bty USB
  sticks all point `bty-tui --server URL` at the same container.
- Local development backend for `bty-tui --server` work.

For PXE-boot provisioning, deploy the bare-metal `bty-server`
appliance instead. See
[`walkthrough-server-docker.md`](walkthrough-server-docker.md)
for the full operator guide.

The intended operator experience is appliance-grade:

1. `dd` (or `bty flash`) the image onto the server host's disk (or SD
   card, for the Pi variant).
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080` with a default `bty / bty` credential and an
   `odus` admin user with passwordless sudo.
3. SSH in once to rotate the password (`sudo passwd bty`) and then
   open `/ui/login` in a browser.
4. The Settings page activates the dnsmasq proxy-DHCP block when
   you're ready to start serving PXE; everything else (machine
   assignments, image catalog, boot artifacts) is browser-driven from
   that point on.

*Hardware targets.* `server-x86` runs on any amd64 box that boots a
Debian cloud image: older Intel NUCs, discarded 1U servers, recent
GMKtec mini-PCs. The same artifact also boots as a VM disk.
`server-rpi` targets the 64-bit Raspberry Pis (4 and 5); both boot the
SD-card image natively.

## CIJOE provisioning modes

`cijoe` runs in one of two execution modes depending on the deployment
vehicle:

- **Offline (USB live).** The workflow runs from the live environment
  after the flash, against the freshly-written filesystem (mount, edit,
  unmount), before the target reboots. Customisation is constrained to
  what is possible by manipulating the filesystem from the outside.
- **Online (PXE / server).** After the target first-boots into its own
  OS, `bty-web` triggers a CIJOE workflow against the running machine
  and records the post-workflow state as that machine's known-good
  baseline. The server - not the image - becomes the source of truth
  for "what this box is supposed to look like."
