# Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with four console-script
entry points, plus a sibling appliance-image builder (`bty-media/`).

## `bty` (CLI)

Main command-line interface. The single source of truth for image
inspection, target-disk discovery, flashing, and provisioning. Every
other component is a UI or delivery vehicle for what `bty` does.

Installable on any Linux environment with a sufficient Python runtime:

```bash
pipx install bty-lab
```

## `bty-ctl` (CLI client for the remote server)

Companion CLI for a remote `bty-web` deployment. `bty-ctl login`
authenticates against the server's PAM, caches a session token at
`~/.config/bty/token` (mode 0600), and `bty-ctl logout` revokes it.
Future subcommands will surface fleet-level operations (list machines,
assign images, drive provisioning) without requiring shell access on
the server.

Ships with the base install - no extra needed:

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

Sibling directory at the repo root. Not a Python package. Builds the
two appliance images:

**USB live image.** Bootable USB stick carrying the `bty` runtime and a
bundled set of system images. Operator plugs it in, boots a target,
runs `bty flash` against the local disk. Self-contained and offline.
Direct-flash delivery vehicle.

**Server image.** Installable disk image that, when written to a host's
disk and booted, runs the bty provisioning server (`bty-web`, the
iPXE/TFTP/HTTP services that PXE clients chain through, the
network-flash live environment those clients boot into, and a storage
layout for the image library). One artifact, ready to serve a fleet.

The intended operator experience is appliance-grade:

1. `dd` (or `bty flash`) the image onto the server host's disk.
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080` with a default `bty / bty` credential and an
   `odus` admin user with passwordless sudo.
3. SSH in once to rotate the password (`sudo passwd bty`) and then
   open `/ui/login` in a browser.
4. The Settings page activates the dnsmasq proxy-DHCP block when
   you're ready to start serving PXE; everything else (machine
   assignments, image catalog, boot artifacts) is browser-driven from
   that point on.

*Hardware targets.* The server image is built for `amd64` only:
older Intel NUCs, discarded 1U servers, recent GMKtec mini-PCs, and
similar small x86 boxes. The same artifact also boots as a VM disk.
An `arm64` variant for Raspberry Pi or other SBCs is feasible but
intentionally out of scope for the initial roadmap; if there is
interest, it can be added later.

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
