# bty - flash images onto target disks, locally or over PXE

Image-flash provisioning toolkit for bare-metal and virtual targets.
Writes pre-built ("cooked") system images onto target disks - locally
from a USB live stick or remotely over PXE - and configures the
deployed system on first boot via cloud-init or CIJOE workflows.

`bty` is an umbrella project. The repository hosts several independent
software components that share a name, a goal, and a set of conventions, but
are otherwise developed and consumed on their own terms.

## Motivation

The overarching goal is to make it trivial to deploy pre-built ("cooked")
system images onto bare metal, appliance-style. The driving use case is CI:
labs and pipelines where a box's role is "be a fresh, known-good
environment for the next job," and the cheapest way to get there is to
reflash the disk from a curated image.

That use case shows up at three different cadences, all of which bty
treats as routine rather than exceptional:

- **Per-job** - wipe and reflash between CI runs, so each job starts from a
  bit-identical baseline.
- **On new image** - promote a freshly-cooked image and roll it out across
  the relevant fleet members.
- **On failure** - a deployed instance has gone bad; reflash recovers it
  without operator hand-holding.

Every design choice in this plan - the appliance-style server image, the
MAC-keyed assignment model, the no-SSH web UX, the iPXE network flash -
exists to make those three cadences cheap, fast, and boring.

bty is shaped to serve both ends of the spectrum:

- **Ad-hoc.** An operator with a single box and no infrastructure should
  be able to grab the USB live image, plug it in, flash, walk away. No
  server to stand up, no MAC registration, no network to wire. That path
  is owned by `bty-media`'s USB live image, plus `bty` and `bty-tui`.
- **DevOps infrastructure.** A lab or CI environment with a fleet should
  be able to stand up a single provisioning server, register machines by
  MAC, and let reflashes happen on schedule, on demand, or on failure
  without operator involvement. That path is owned by `bty-media`'s
  server image, plus `bty-web`.

The same `bty` runtime sits at the centre of both - same image catalog,
same target-disk operations, same provisioning modes - so the two paths
are different surfaces over one core, not two parallel implementations.

## Scope

bty is intentionally OS-agnostic. The image is a sealed, pre-built
artifact; bty puts it on disk and hands off to a first-boot mechanism if
any. The supported target list is therefore *"what can be packaged as a
bootable disk image,"* not *"what bty has special knowledge of."*

- **Linux of any flavor** - including vendor-dictated stacks like
  Ubuntu-with-NVIDIA, where the distribution is pinned by what the hardware
  vendor's driver tree supports rather than by operator preference.
- **FreeBSD** - first-class flash target.
- **Windows** - first-class flash target. First-boot configuration uses
  the OS's native unattend mechanism rather than cloud-init.
- **macOS** - desired but out of scope. Apple does not provide practical
  automation hooks at the disk-image level; if that changes, the door is
  open.

This is a deliberate contrast with NixOS, which solves the cooked-image
deployment problem brilliantly but only for NixOS-flavored systems. bty
sits in the niche NixOS does not cover: the *image* is whatever a vendor or
upstream produces, with no influence from bty.

## Components

bty is **one Python package** (distribution name `bty-lab` on PyPI; the
importable module stays `bty`) with three console-script entry points
(`bty`, `bty-tui`, `bty-web`) and optional install extras,
plus a sibling appliance-image builder under `bty-media/`. Splitting
the Python side into multiple distributions earned nothing for a
single-maintainer project; the "different install footprint for
different users" need is handled cleanly through optional extras.

The components below are conceptual code areas, not separate distributions.

### `bty` (library + main CLI)

The Python library and the `bty` command. Single source of truth for image
inspection, target-disk discovery, flashing, and provisioning. Everything
else is a UI or a delivery vehicle for this. Usable standalone from any
Linux environment with a sufficient runtime:

```bash
pipx install bty-lab
```

Lives at `src/bty/` with the CLI entry point in `src/bty/cli.py`.

### `bty-tui` (terminal UI)

Terminal UI on top of the library. Targeted at interactive use from a live
environment where a graphical browser is not appropriate - a serial
console, an SSH session, or a minimal recovery image. Exposes the same
operations as the CLI in a navigable form.

Shipped as the `bty-lab[tui]` install extra and exposed as the `bty-tui`
console script. Lives under `src/bty/tui/`.

```bash
pipx install "bty-lab[tui]"
```

### `bty-web` (HTTP server + browser UI)

HTTP server with browser UI. Hosts the MAC-address-keyed assignment of
image and provisioning mode, renders per-MAC iPXE configurations, serves
the bootstrap requests issued by the bty live environment during a network
flash, and - after the target first-boots - drives the online CIJOE step
and records the post-workflow state as the machine's known-good baseline.
Successor to the original Flask UI. Stateful; the system of record for
both fleet provisioning intent and per-machine known-good state.

Shipped as the `bty-lab[web]` install extra and exposed as the `bty-web`
console script. Lives under `src/bty/web/`.

```bash
pipx install "bty-lab[web]"
```

State (machine records, MAC <-> image/provisioning assignments, CIJOE
workflow references and run reports, per-machine known-good baselines,
image catalog metadata, server settings) is persisted in a single
SQLite database under `BTY_STATE_DIR`. Backup or migration is just
copying the file.

CIJOE produces a structured report on every workflow run. `bty-web`
captures these reports - both for offline runs (sent back from the live
environment) and online runs (executed by the server itself) - and exposes
them in the UI per machine and per run. Reports are downloadable in full,
so an operator chasing a flaky reflash can inspect the complete log
without leaving the browser.

The runtime is sized for modest x86 hardware: lightweight Python web
framework, no heavy front-end build pipeline, no JVM dependencies. Server
behaviour does not change with hardware tier - an older NUC and a recent
GMKtec mini-PC run the same code at different scales.

### `bty-media` (appliance-image builder)

Sibling directory at the repo root, *not* a Python package. Builds the
bootable images that turn this toolkit into something an operator can
carry around or stand up on a server. Follows the layout used by
`safl/jellyfin-kiosk-appliance-builder` (jkab); the cijoe orchestration
(configs, scripts, tasks) lives at the top-level `cijoe/` directory in
this repo and consumes the `bty-media/` content (rootfs trees,
cloud-init bases, live-build config).

Four shipping variants:

**`usb-x86`** - bootable USB stick carrying the `bty` CLI, `bty-tui`,
and an exFAT `BTY_IMAGES` partition for cooked images. The operator
plugs it into a target machine, boots it, and runs `bty flash` against
the target's local disk using images sourced from the stick itself.
Self-contained and offline. The direct-flash flow's delivery vehicle.

**`server-x86`** - installable disk image (amd64) that, when written
to a host's disk and booted, runs the bty provisioning server:
`bty-web`, the iPXE / TFTP / HTTP services that PXE clients chain
through, and the storage layout for the image library. The
network-flash flow's delivery vehicle for x86 servers.

**`server-rpi`** - same appliance role on arm64, delivered as an
SD-card image for Raspberry Pi 4 / 5. Built by mounting the upstream
Raspberry Pi OS Lite image and customising it in a
`qemu-aarch64-static` chroot (no QEMU full-system bake needed).
Booting a Pi off SD is the homelab-friendliest server-deployment path.

**`netboot-x86`** - kernel + initrd + squashfs trio that PXE
clients chain into via the server's HTTP boot stack. The chroot
ships `bty-flash-on-boot.service` (auto-flash mode) and
`bty-tui-on-tty1.service` (interactive `bty-tui` on tty1), with
the mode picked by kernel cmdline params from the server's iPXE
chain. Renamed from `live-x86` in M19 phase 5 to disambiguate
from `usb-x86` (which is also a live image).

The intended operator experience for the server variants is
appliance-grade:

1. `dd` (or `bty flash`) the image onto the server host's disk
   (or SD card, for the Pi).
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080` with the default `bty / bty` PAM credential
   and an `odus` SSH admin user (passwordless sudo).
3. Open `http://<host>:8080/` in a browser - the bare host redirects
   to the login form. Default `bty / bty` credential gets you in;
   rotate with `sudo passwd bty` on the appliance before exposing.
4. From that point on, the server is driven entirely through the web
   UI for fleet operations (machine assignments, image catalog, boot
   artifacts). The Settings page activates the dnsmasq proxy-DHCP
   block when ready to serve PXE.

All four variants are produced by the cijoe orchestration in
`cijoe/` consuming the content under `bty-media/` - they share a
single `bty` wheel and a single `rootfs/server/` overlay (for the
two server variants).

## Image formats

`.qcow2`, `.img`, `.img.zst`

## Provisioning modes

After the image is written to disk, bty can hand off to a first-boot
configuration mechanism. Four modes:

- **`none`** - no post-flash configuration. Reboot into the cooked image
  as-is.
- **`cloud-init`** - populate the OS's cloud-init seed (NoCloud
  datasource) with operator-supplied user-data and meta-data; the OS picks
  it up on first boot. Linux and FreeBSD today; the Windows analogue
  (unattend) occupies the same slot when Windows lands.
- **`cijoe`** - run a CIJOE workflow against the freshly-written
  filesystem (mount, edit, unmount) before the target reboots. The
  USB live env's offline customisation path. Constrained to what is
  possible by manipulating the filesystem from the outside - file
  edits, package staging, seed-file drops.
- **`cijoe-online`** - bty-web only. After the target first-boots into
  its own OS, `bty-web` triggers a CIJOE workflow against the running
  machine and records the post-workflow state as that machine's
  known-good baseline. The server - not the image - becomes the source
  of truth for *"what this box is supposed to look like,"* which is
  what closes the loop on the per-job and on-failure cadences from
  the Motivation section.

CIJOE is bty's official extension point for deviations from a stock
image: vendor-specific tweaks, licence files, IPMI credentials, fleet-
specific tuning that should not be baked into the image itself.

## Concepts

- **Image** - a system image file in one of the supported formats,
  residing in a configured image root (or fetched from an HTTP URL via
  `bty flash --image http://...`).
- **Target** - a block device on the machine being provisioned.
- **Provisioning mode** - what (if anything) runs on first boot.
- **Machine record** (web only) - MAC-address-keyed assignment of image
  + provisioning mode + optional hostname + boot policy.
- **Boot policy** (web only) - what `GET /pxe/{mac}` returns: `local`
  (sanboot), `flash` (auto-flash chain), or `tui` (interactive
  `bty-tui` on tty1; the auto-discovery default for unknown MACs).

## Flows

### Direct flash (CLI / TUI)

Operator boots the target machine from bty live media (USB), then
runs `bty` locally:

```
sudo bty flash --image IMG --target /dev/sda --provision cloud-init ...
```

`bty flash --image` also accepts an HTTP/HTTPS URL, in which case
the bytes stream from the URL through `zstd -d | dd` straight to
the target disk - no temp file for `.img.zst` / `.img`.

### Interactive PXE flash (`boot_policy=tui`)

The default for unknown MACs that PXE-boot through the server. The
client lands in the live env in interactive mode; `bty-tui-on-tty1`
launches `bty-tui --server URL --mac MAC` which fetches the catalog
from `GET /images` and streams the operator-picked image straight to
the target disk via `bty flash --image URL`. On success, the TUI
`POST`s `/pxe/{mac}/done` so `last_flashed_at` updates server-side.
"bty-on-a-USB but over the network" - first PXE contact lands a
useful UI without prior server-side configuration.

### Server-driven PXE flash (`boot_policy=flash`)

1. Operator assigns `MAC -> image + provisioning + boot_policy=flash`
   in the web UI.
2. Target machine PXE-boots; iPXE chains into the bty live env over
   HTTP.
3. bty live env's `bty-flash-on-boot.service` reads kernel cmdline
   params, fetches the assigned image, flashes the target disk,
   applies provisioning, signals `/pxe/{mac}/done`, reboots.
4. Per-job CI cadences leave `boot_policy=flash` so every boot
   reflashes; one-shot deployments flip to `local` after the first
   successful flash.

Both BIOS and UEFI clients are supported via iPXE.

## Repository layout

One Python project at the repo root (src layout, hatchling build backend),
plus a sibling appliance-image builder and a docs tree. `uv` manages the
project venv and the lockfile.

```
bty/
+-- pyproject.toml          # one [project] = "bty-lab" with optional extras
+-- uv.lock                 # committed
+-- Makefile                # one-stop driver (deps / test / build / docs)
+-- PLAN.md
+-- README.md
+-- AGENTS.md
+-- LICENSE                 # GPL-3.0-only
+-- src/
|   \-- bty/                # the Python package
|       +-- __init__.py
|       +-- cli.py          # bty console script
|       +-- tui/            # bty-tui console script (extra: tui)
|       \-- web/            # bty-web console script (extra: web)
+-- tests/
+-- docs/
|   +-- src/                # MyST + Sphinx sources
|   \-- tooling/            # bty-docs-* commands (pipx install ./tooling)
+-- bty-media/              # appliance-image content (rootfs trees,
|   +-- README.md           #   cloud-init bases, live-build config).
|   +-- auxiliary/          #   NOT a Python package.
|   +-- rootfs/             #   {common,usb,server}/ overlays
|   \-- live-build/         #   debian-live config tree
+-- cijoe/                  # appliance-image build orchestration
|   +-- configs/            #   per-variant TOML
|   +-- scripts/            #   cijoe scripts (download, customise, publish)
|   \-- tasks/              #   per-variant task pipelines (build /
|                           #   build-rpi / live / test-pxe)
\-- .github/
    \-- workflows/          # ci / docs / release
```

There is one wheel (`bty`), one set of console scripts (`bty`, `bty-tui`,
`bty-web`), and one version. Optional extras (`tui`, `web`, `all`) gate
the heavier dependencies so a CLI-only install stays light.

## Continuous integration

CI runs on GitHub Actions, organised as three workflows: `ci`, `docs`,
and `release`.

### Per-PR (and on push to `main`)

- **Lint** - `uv run ruff check` and `uv run ruff format --check`.
- **Type-check** - `uv run mypy src`.
- **Test** - `uv run pytest`; matrix over supported Python versions.
- **Docs build** - `bty-docs-build-html` and `bty-docs-build-pdf` via
  the pipx-installed docs tooling. PR builds upload both artifacts;
  pushes to `main` additionally publish HTML to GitHub Pages. The
  pdflatex toolchain is kept simple by writing sane UTF-8 in the
  sources (em-dashes and smart quotes fine; no exotic arrows or
  box-drawing).

### On tag

- **`v*` tags** - single unified release. `uv build` produces the
  wheel and sdist (PyPI publish via trusted publishing); the same
  workflow builds the four `bty-media` variants in parallel
  (`usb-x86`, `server-x86`, `server-rpi`, `netboot-x86`), runs the
  end-to-end PXE chain test against the freshly-built artefacts,
  builds HTML + PDF docs, and attaches every release-bound artifact
  to the GitHub release at the same tag. Operators get one release
  page covering the whole stack at one version; `/ui/boot`'s "fetch
  latest" on the appliance pulls the matching live trio.

### On `main`

- **Pages deploy** - published documentation (HTML).
- **Nightly media build** *(optional, later)* - fresh `bty-media`
  artifacts available as a workflow artifact for testing.

## Documentation

Documentation lives in `docs/` and follows the aisio convention:

- `docs/src/` - Sphinx + MyST markdown sources.
- `docs/tooling/` - Python package providing dev commands, installed via
  `pipx install ./tooling`.
- Build commands: `bty-docs-serve` (live-rebuild dev server), 
  `bty-docs-build-html`, `bty-docs-build-pdf`. Both HTML and PDF (LaTeX) are
  first-class outputs.

### Outline

- **Overview** - what bty is, the components, and how they compose into
  the direct-flash and network-flash flows.
- **Concepts** - image, target, provisioning mode, machine record.
- **Flows** - direct flash, network flash (BIOS + UEFI via iPXE).
- **Components** - sections per component (`bty` CLI, `bty-tui`,
  `bty-web`, `bty-media/`). Scope, public surface, configuration,
  operational notes.
- **Related work** - how bty positions against MAAS, FOG, iVentoy,
  NixOS, and others.
- **Reference** - CLI, HTTP API, configuration schemas, state
  export/import format.

## Non-goals

- Not a general-purpose OS installer.
- Not a replacement for CIJOE.
- Not a second configuration-management system.
- Not built on Alpine.
- Not a deployment story for macOS targets.

## Milestones

The original 1.0 roadmap. All shipping in current releases; kept here
as historical record of the build-out order.

1. **[done]** Repo skeleton - single Python package, sibling
   `bty-media/`, docs tooling, CI workflows.
2. **[done]** `bty-media` USB live build pipeline (cijoe + Debian
   cloud-image + QEMU bake).
3. **[done]** `bty list disks` - block-device discovery.
4. **[done]** `bty list images` + `bty inspect image`.
5. **[done]** `bty flash --dry-run` validation.
6. **[done]** `bty flash` write path for `.qcow2` / `.img` / `.img.zst`.
7. **[done]** Provisioning: `none`.
8. **[done]** Provisioning: `cloud-init`.
9. **[done]** Provisioning: `cijoe` (offline).
10. **[done]** `bty-tui`.
11. **[done]** `bty-web` server - MAC-keyed assignment, iPXE rendering.
12. **[done]** `bty-web` UI - browser front-end.
13. **[done]** `bty-media` server image (`server-x86` variant).
14. **[done]** Network-flash end-to-end (iPXE -> bty live -> flash ->
    reboot, BIOS + UEFI).
15. **[done]** Provisioning: `cijoe-online` - server triggers a workflow
    against the booted target and records the known-good baseline.

### Post-roadmap milestones

Landed after the original 1.0 list:

16. **[done]** TUI-on-PXE flow - new `boot_policy=tui` (default for
    auto-discovered MACs), `ipxe_tui.j2` template, streaming
    `bty flash --image URL`, `bty-tui --server URL --mac MAC` remote
    mode, `bty-tui-on-tty1.service` in the live env. First PXE
    contact lands the operator at the TUI without prior server-side
    configuration ("bty-on-a-USB but over the network").
17. **[done]** `server-rpi` variant - SD-card image for Raspberry Pi
    4 / 5. Built by chrooting into Raspberry Pi OS Lite arm64 via
    `qemu-aarch64-static`. Same `bty / bty` PAM credential and
    `odus / odus` SSH admin as the x86 server image.
18. **[done]** Auth simplification - dropped the `bty-ctl` console
    script, the `/auth/login` / `/auth/logout` HTTP endpoints, the
    Bearer scheme, and the custom `sessions` SQLite table. Replaced
    with Starlette's `SessionMiddleware` (server-signed cookie, no
    DB hop). Net ~760 LOC deleted with no browser-flow regression.
19. **[wip]** Ported `usb-x86` from cloud-init + `overlayroot` to
    live-build. The previous path stitched a stock Debian cloud
    image, an ext4 rootfs, and the `overlayroot` package's
    initramfs hook; that hook was fragile across kernel / hardware
    combos (kernel panic on GMKtec MiniBoxXS, kernel
    6.12.85+deb13-amd64). live-boot's SquashFS + tmpfs overlay is
    the canonical Debian path for ephemeral live media and is what
    `netboot-x86` already uses. Phases 1-6 are done; phase 7
    (Windows-friendly partition layout) is open.

    1. **[done]** Add an `iso-hybrid` output target to the existing
       live-build config (parallel to the current `--binary-images
       netboot` output). Output: `bty-usb-x86_64.iso`. Most of the
       chroot is shared with `netboot-x86` - this is genuinely a
       packaging variant. Driven by a new `usb-iso` cijoe variant
       (`cijoe/configs/usb-iso.toml`, `cijoe/tasks/usb.yaml`,
       `cijoe/scripts/usb_iso_build.py`); marked experimental in
       the release.yml media matrix until proven on real hardware.
    2. **[done]** Make `bty-tui-on-tty1.service` graceful when no
       `bty.server` / `bty.mac` is on the kernel cmdline: the
       wrapper script forwards no flags and `bty-tui` falls back
       to scanning the local image-root. Same service, two modes
       (PXE-driven remote, USB-driven local). The usb-iso bake
       sets `bty.mode=interactive` directly via `--bootappend-live`
       so the existing service fires on USB boot the same way it
       fires for the PXE-tui flow.
    3. **[done]** Bake a writable `BTY_IMAGES` exFAT partition
       into the cooked ISO. Post-process the live-build output:
       `truncate +4G` to extend the file, `sgdisk
       --move-second-header` + `--new` to add the partition entry
       to the GPT, `losetup -fP` + `mkfs.exfat` to format. The
       single artifact `dd`'s onto a stick with a writable area
       the operator can drop `*.img.zst` files onto from any host
       OS - same UX the legacy cloud-init `usb-x86` provided, but
       baked statically instead of carved by cloud-init runcmd.
    4. **[done]** First-boot grow + mount.
       `bty-grow-images-partition.service` extends `BTY_IMAGES` to
       fill the rest of the stick (only when empty so operator
       data is preserved; multiple safety gates short-circuit the
       grow on Ventoy / loopback / netboot / already-grown
       sticks). `var-lib-bty-images.mount` then mounts the
       partition RO at `/var/lib/bty/images` so `bty-tui` and the
       flash flow find it at the default image-root path - no
       runtime auto-discovery needed.
    5. **[done]** Documented delivery options + renamed
       `live-x86` -> `netboot-x86` (both deferred work folded
       into the same commit). Stock hybrid ISO with built-in
       writable area; operators write it with their existing
       tooling (`dd`, Balena Etcher, Rufus) or drop it onto an
       existing Ventoy stick alongside their other rescue ISOs.
       The project does not ship a stick-writing tool of its
       own. The `live-x86` rename addressed a long-standing
       naming bug: both `usb-x86` and `live-x86` produced live
       envs, so calling one "live" was ambiguous; the actual
       distinguishing axis is delivery mechanism (USB vs PXE).
       Artifact name dropped the redundant `-netboot` suffix:
       `bty-live-x86_64-netboot/bty-live-x86_64.*` ->
       `bty-netboot-x86_64/bty-netboot-x86_64.*`.
    6. **[done]** Retired the `overlayroot` dependency, the
       cloud-init usb-x86 bake (`cloudinit-base-usb.user`,
       `rootfs/usb/`, the cloud-init `usb-x86.toml` config,
       `docs/asciinema/usb-build.sh`), and the legacy
       `bty-usb-x86_64-img-zst` release artifact. The `usb-x86`
       variant name now points at the live-build path that was
       called `usb-iso` during phases 1-5; the cijoe config moved
       from `usb-iso.toml` to `usb-x86.toml`. Gated on hardware
       verification of the v0.2.20 release; landed after the
       GMKtec MiniBoxXS booted cleanly with the live-build
       artifact and the BTY_IMAGES partition mounted at
       `/var/lib/bty/images`.
    7. Make `BTY_IMAGES` visible to Windows. Discovered on
       hardware verification of v0.2.20: when the dd'd stick is
       plugged into a Windows host, the operator can read the
       ISO9660 (drive `D:`) and sees the EFI partition + an
       "Unallocated" trailing region in Disk Management. The
       trailing region IS the BTY_IMAGES exFAT - Windows refuses
       to enumerate it because the EFI partition entry (#2) sits
       *inside* the ISO9660 partition's byte range (an artifact
       of how live-build's `iso-hybrid` mode embeds the EFI
       FAT image in the ISO9660 stream). Linux mounts BTY_IMAGES
       fine; Windows / Mac don't, breaking the "drop images from
       any host OS" UX promise from phase 3.

       Recommended fix: **relocate the EFI partition entry out
       of the overlap**.

         a. After `lb build` produces the hybrid ISO, parse the
            existing MBR to find the EFI partition's current
            byte range (sectors 540..7067 by default; ~3 MiB).
            Read those bytes - the FAT image of the EFI boot
            payload.
         b. Append a fresh copy of the EFI FAT image to the
            iso file at a non-overlapping location (right
            after the ISO9660, before BTY_IMAGES).
         c. Rewrite the MBR partition table:
            - p1: ISO9660 (covers the live-build output, no
              change)
            - p2: EFI System partition pointing at the NEW
              location (non-overlapping with p1)
            - p3: BTY_IMAGES exFAT (current trailing 4 GiB)
         d. The El Torito catalog inside the ISO9660 still
            has its embedded EFI image for CD-style UEFI boot;
            the relocated MBR partition entry handles USB-style
            UEFI boot. BIOS boot via the existing `isohdpfx.bin`
            in MBR sectors 0..432 is untouched.

       Result: Windows enumerates all three partitions cleanly,
       auto-mounts (or lets Disk Management mount) BTY_IMAGES,
       operators drop `*.img.zst` from any host OS as advertised.

       **Alternatives considered:**

       - Drop the MBR EFI partition entry entirely; rely on El
         Torito's embedded EFI image for both CD and USB UEFI
         boot. Simpler change, but compatibility risk: some
         older UEFI firmware needs the MBR/GPT EFI partition
         entry to recognize the USB stick as bootable.
       - Switch to `--binary-images hdd` instead of
         `iso-hybrid`. live-build's hdd mode produces a regular
         disk image with non-overlapping partitions, but the
         BIOS boot path differs and may need additional
         bootloader-config plumbing.
       - Use Rufus's "ISO Image" mode (rebuilds the layout into
         a single FAT32 partition). Operator-side workaround
         only; doesn't fix the artifact and discards
         BTY_IMAGES.

       Critical files:
       - `cijoe/scripts/usb_iso_build.py::_extend_with_exfat` -
         add the EFI relocation step before / alongside the
         BTY_IMAGES partition append.
       - `bty-media/live-build/auto/config` - no change
         expected; the EFI bootloader stays the same, only its
         on-disk location moves.

       Not blocking phase 6 (cloud-init `usb-x86` retire);
       phase 6 depends on the boot path working on hardware,
       which it now does. Windows-side catalog management is a
       UX improvement on top of a working appliance.

## Preserved from legacy bty

These behaviors from the pre-rewrite version are load-bearing requirements,
not optional:

- MAC-keyed image assignment.
- Per-MAC bootloader configs, rewritten by the server.
- Reflash-loop prevention: after a successful flash, the next boot is from
  local disk, not from the network.
- "Live env does the heavy lifting; the server just orchestrates"
  architecture (originally Clonezilla; replacement is the bty media builder
  output).
