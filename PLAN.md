# bty - flash images onto target disks, offline or networked with and without PXE

Image-flash toolkit for bare-metal and virtual targets. Writes
pre-built system images onto target disks - locally from a
USB live stick or remotely over PXE. First-boot bring-up is the
image builder's job (cloud-init / NoCloud user-data baked in at
build time); bty itself only writes bytes.

`bty` is an umbrella project. The repository hosts several independent
software components that share a name, a goal, and a set of conventions, but
are otherwise developed and consumed on their own terms.

## Motivation

The overarching goal is to make it trivial to deploy pre-built
system images onto bare metal, appliance-style. The driving use case is CI:
labs and pipelines where a box's role is "be a fresh, known-good
environment for the next job," and the cheapest way to get there is to
reflash the disk from a curated image.

That use case shows up at three different cadences, all of which bty
treats as routine rather than exceptional:

- **Per-job** - wipe and reflash between CI runs, so each job starts from a
  bit-identical baseline.
- **On new image** - promote a freshly-built image and roll it out across
  the relevant fleet members.
- **On failure** - a deployed instance has gone bad; reflash recovers it
  without operator hand-holding.

Every design choice in this plan - the appliance-style server image, the
MAC-keyed assignment model, the no-SSH web UX, the iPXE network flash -
exists to make those three cadences cheap, fast, and boring.

bty is shaped to serve both ends of the spectrum:

- **Ad-hoc.** An operator with a single box and no infrastructure should
  be able to grab the USB live image, plug it in, flash, walk away. No
  server to set up, no MAC registration, no network to wire. That path
  is owned by `bty-media`'s USB live image, plus the `bty` wizard.
- **DevOps infrastructure.** A lab or CI environment with a fleet should
  be able to set up a single bty server appliance, register machines
  by MAC, and let reflashes happen on schedule, on demand, or on failure
  without operator involvement. That path is owned by `bty-media`'s
  server image, plus `bty-web`.

The same `bty` runtime sits at the centre of both - same image catalog,
same target-disk operations - so the two paths are different surfaces
over one core, not two parallel implementations.

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

This is a deliberate contrast with NixOS, which solves the pre-built-image
deployment problem brilliantly but only for NixOS-flavored systems. bty
sits in the niche NixOS does not cover: the *image* is whatever a vendor or
upstream produces, with no influence from bty.

## Components

bty is **one Python package** (distribution name `bty-lab` on PyPI;
the importable module stays `bty`) with two console-script entry
points (`bty`, `bty-web`) and optional install extras, plus a
sibling appliance-image builder under `bty-media/`. Splitting the
Python side into multiple distributions earned nothing for a
single-maintainer project; the "different install footprint for
different users" need is handled cleanly through optional extras.

The components below are conceptual code areas, not separate
distributions.

### `bty` (the wizard + library)

The operator-facing tool. A Rich-based wizard that picks an image
+ a target disk and flashes; the same code path also handles
scripted / server-driven flashes via the bty-web plan endpoint.
Single source of truth for image inspection, target-disk discovery,
flashing, and remote-catalog ingestion. Lives at `src/bty/tui/`
with the entry point in `src/bty/tui/__init__.py`; shared library
modules at `src/bty/{flash,images,catalog,disks,oras}.py`.

Shipped as the `bty-lab[tui]` install extra (the Rich dependency
sits behind that extra so a `[web]`-only install footprint is
small). The console script is `bty`:

```bash
pipx install "bty-lab[tui]"
```

Three invocation shapes:

- `bty` -- interactive wizard, local image-root only.
- `bty --catalog URL` -- interactive wizard with the catalog
  pre-loaded.
- `bty --server X --mac Y` -- server-driven: GETs
  `<X>/pxe/<Y>/plan` and dispatches (auto-flash / interactive
  / no-op) on the JSON response.

(v0.22.10 collapsed the historical separate `bty` CLI and
`bty-tui` wizard into this single tool. `bty inspect` / `bty
flash` / `bty images` / `bty catalog` subcommands retired; the
wizard subsumes them.)

### `bty-web` (HTTP server + browser UI)

HTTP server with browser UI. Hosts the MAC-address-keyed assignment of
image + boot policy, renders per-MAC iPXE configurations, serves the
bootstrap requests issued by the bty live environment during a network
flash, and records last-seen / last-flashed timestamps as the per-MAC
audit trail.
Successor to the original Flask UI. Stateful; the system of record for
per-MAC image + boot-policy assignments and the audit-log timeline.

Shipped as the `bty-lab[web]` install extra and exposed as the `bty-web`
console script. Lives under `src/bty/web/`.

```bash
pipx install "bty-lab[web]"
```

State (machine records, MAC <-> image / boot-policy assignments,
image catalog metadata, audit-log events, server settings) is
persisted in a single SQLite database under `BTY_STATE_DIR`. Backup
or migration is just copying the file.

The runtime is sized for modest x86 hardware: lightweight Python web
framework, no heavy front-end build pipeline, no JVM dependencies. Server
behaviour does not change with hardware tier - an older NUC and a recent
GMKtec mini-PC run the same code at different scales.

### `bty-media` (appliance-image builder)

Sibling directory at the repo root, *not* a Python package. Builds the
bootable images that turn this toolkit into something an operator can
carry around or set up on a server. Follows the layout used by
`safl/jellyfin-kiosk-appliance-builder` (jkab); the cijoe orchestration
(configs, scripts, tasks) lives at the top-level `cijoe/` directory in
this repo and consumes the `bty-media/` content (rootfs trees,
cloud-init bases, live-build config).

Four shipping variants:

**`usbboot-pc`** - bootable USB stick carrying the `bty` runtime
and an exFAT `BTY_IMAGES` partition for pre-built images. The
operator plugs it into a target machine, boots it; `bty`
auto-launches on tty1 (via `bty-on-tty1.service`) and walks
the operator through pick + flash using images sourced from the
stick itself. Self-contained and offline. The direct-flash flow's
delivery vehicle.

**`server-x86`** - installable disk image (amd64) that, when written
to a host's disk and booted, runs the bty server appliance:
`bty-web`, the iPXE / TFTP / HTTP services that PXE clients chain
through, and the storage layout for the image library. The
network-flash flow's delivery vehicle for x86 servers.

**`server-rpi`** - same appliance role on arm64, delivered as an
SD-card image for Raspberry Pi 4 / 5. Built by mounting the upstream
Raspberry Pi OS Lite image and customising it in a
`qemu-aarch64-static` chroot (no QEMU full-system bake needed).
Booting a Pi off SD is the homelab-friendliest server-deployment path.

**`netboot-pc`** - kernel + initrd + squashfs trio that PXE
clients chain into via the server's HTTP boot stack. The chroot
ships `bty-on-tty1.service` (unconditional; runs on every
boot). The cmdline carries `bty.server` + `bty.mac` only; `bty`
GETs `<server>/pxe/<mac>/plan` to decide whether to auto-flash,
interact, or exit. Renamed from `live-x86` to disambiguate from
`usbboot-pc` (which is also a live image).

The intended operator experience for the server variants is
appliance-grade:

1. `dd` (or boot the bty USB live, run `bty`, flash the appliance
   from the starter `.bri` catalog) the image onto the server
   host's disk (or SD card, for the Pi).
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080`, the operator UI gated by `$BTY_ADMIN_PASSWORD`
   (unset = open, with a startup warning), and an `odus` SSH admin user
   (passwordless sudo).
3. Open `http://<host>:8080/` in a browser - the bare host redirects
   to the login form. The `$BTY_ADMIN_PASSWORD` value gets you in;
   rotate by changing the env var and restarting bty-web before exposing.
4. From that point on, the server is driven entirely through the web
   UI for fleet operations (machine assignments, image catalog, boot
   artifacts). The Settings page activates the dnsmasq proxy-DHCP
   block when ready to serve PXE.

All four variants are produced by the cijoe orchestration in
`cijoe/` consuming the content under `bty-media/` - they share a
single `bty` wheel and a single `rootfs/server/` overlay (for the
two server variants).

## Image formats

`.qcow2`, `.img`, `.img.zst`, `.img.xz`, `.img.gz`, `.img.bz2`.
Tarballs (`.tar.gz` etc.) are not flashable directly; the flash
code refuses them with a specific "extract first" message
because dd'ing a tar stream into a target's MBR would be
catastrophic.

## Post-boot configuration

bty is a flasher, not an image builder. First-boot bring-up (users, network,
packages, hostnames) is the image builder's job upstream. The reboot
after flashing is unconfigured by design: the target comes up as
whatever the pre-built image declared. bty-web does not hold creds for
any target it has provisioned, and there is no post-flash workflow
runner; if you need fleet-specific tweaks, bake them into the image.

## Concepts

- **Image** - a system image file in one of the supported formats,
  residing in a configured image root or fetched from an HTTP /
  oras:// URL via a catalog entry.
- **Target** - a block device on the machine being flashed.
- **Machine record** (web only) - MAC-address-keyed assignment of
  image + optional hostname + boot policy + target disk serial.
- **Boot mode** (web only) - what `GET /pxe/{mac}` returns:
  `ipxe-exit` (boots the local disk via the iPXE sanboot verb on
  BIOS or firmware exit on UEFI), `bty-flash-always` (live-env
  chain; the plan endpoint returns `mode=auto` for the unattended
  flash on every netboot), `bty-flash-once` (same chain; the
  plan-emit path observes `saw_flasher_boot + last_flashed_at`
  and returns the `ipxe-exit` chain after the flash completes),
  `bty-tui` (live-env chain; plan returns `mode=interactive` so
  `bty` drops the operator into the wizard), `bty-inventory` (the
  auto-discovery default for unknown MACs; the live env posts
  disks and reboots into the ipxe-exit chain), or `ramboot`
  (mounts the bound catalog image over NBD via the nbdmux
  sidecar with overlayfs over tmpfs for writes).

## Flows

### Direct flash (USB live, offline)

Operator boots the target machine from bty live media (USB).
`bty` auto-launches on tty1 via ``bty-on-tty1.service``;
without `bty.mac=` on the kernel cmdline it runs in local-only
mode (scans `BTY_IMAGES` + any `.bri` descriptors). Operator
picks image + target disk + confirms; the same Rich progress
panel runs in both interactive and scripted flashes.

`.img` / `.img.zst` / `.img.xz` / `.img.gz` / `.img.bz2` images
stream through the appropriate decompressor piped to `dd`
straight to the target disk -- no temp file. `.qcow2` images are
converted in place via ``qemu-img convert``.

### Interactive PXE flash (`boot_mode=bty-tui`)

The default for unknown MACs that PXE-boot through the server.
The client lands in the live env; `bty-on-tty1.service`
exec's `bty --server X --mac Y` (server URL + MAC from the
kernel cmdline). `bty` GETs `<server>/pxe/<mac>/plan`, sees
`mode=interactive`, drops the operator into the wizard with the
server's catalog pre-loaded. On success the wizard POSTs
`/pxe/{mac}/done` so `last_flashed_at` updates server-side, but
the operator's image pick is NOT reported back. "bty-on-a-USB
but over the network" -- first PXE contact lands a useful UI
without prior server-side configuration.

### Server-driven PXE flash (`boot_mode=bty-flash-always`)

1. Operator assigns `MAC -> bty_image_ref + target_disk_serial +
   boot_mode=bty-flash-always` in the web UI.
2. Target machine PXE-boots; iPXE chains into the bty live env
   over HTTP. Cmdline carries just `bty.server` + `bty.mac`.
3. `bty-on-tty1.service` exec's `bty --server X --mac Y`.
   `bty` GETs `<server>/pxe/<mac>/plan`, sees `mode=auto` with
   the image URL + target serial filled in, flashes without
   prompts, POSTs `/pxe/{mac}/done`, reboots.
4. Per-job CI cadences leave `boot_mode=bty-flash-always` so every
   boot reflashes; one-shot deployments use `bty-flash-once` which
   settles onto the ipxe-exit chain via the `saw_flasher_boot` bit
   after `POST /pxe/{mac}/done`.

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
|       +-- {flash,images,catalog,disks,oras}.py  # shared library
|       +-- tui/            # bty console script (extra: tui)
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

There is one wheel (`bty-lab`), two console scripts (`bty`,
`bty-web`), and one version. Optional extras (`tui`, `web`,
`all`) gate the heavier dependencies so a `[web]`-only install
stays light.

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
  sources: smart quotes fine; no em-dashes, exotic arrows, or
  box-drawing.

### On tag

- **`v*` tags** - single unified release. `uv build` produces the
  wheel and sdist (PyPI publish via trusted publishing); the same
  workflow builds the four `bty-media` variants in parallel
  (`usbboot-pc`, `server-x86`, `server-rpi`, `netboot-pc`), runs the
  end-to-end PXE chain test against the freshly-built artifacts,
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
- **Concepts** - image, target, machine record, boot policy.
- **Flows** - direct flash, network flash (BIOS + UEFI via iPXE).
- **Components** - sections per component (`bty` (wizard +
  library), `bty-web`, `bty-media/`). Scope, public surface,
  configuration, operational notes.
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
3. **[done]** `lsblk -d -e7` - block-device discovery.
4. **[done]** `bty images` + `bty inspect`.
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
15. **[done, then removed v0.8.0]** Provisioning: `cijoe-task` -
    server triggered a workflow against the booted target. Pulled
    because bty-web holding root creds on every provisioned machine
    is the wrong blast radius; fleet-specific tweaks belong in the
    image builder now.

### Post-roadmap milestones

Landed after the original 1.0 list:

16. **[done]** TUI-on-PXE flow - new `boot_mode=bty-tui` (default for
    auto-discovered MACs), `ipxe_tui.j2` template, streaming
    `bty flash URL /dev/sdX`, `bty tui --catalog URL --mac MAC` remote
    mode, `bty-on-tty1.service` in the live env. First PXE
    contact lands the operator at the TUI without prior server-side
    configuration ("bty-on-a-USB but over the network").
17. **[done]** `server-rpi` variant - SD-card image for Raspberry Pi
    4 / 5. Built by chrooting into Raspberry Pi OS Lite arm64 via
    `qemu-aarch64-static`. Same `$BTY_ADMIN_PASSWORD`-gated operator UI and
    `odus / odus` SSH admin as the x86 server image.
18. **[done]** Auth simplification - dropped the `bty-ctl` console
    script, the `/auth/login` / `/auth/logout` HTTP endpoints, the
    Bearer scheme, and the custom `sessions` SQLite table. Replaced
    with Starlette's `SessionMiddleware` (server-signed cookie, no
    DB hop). Net ~760 LOC deleted with no browser-flow regression.
19. **[done]** Ported `usbboot-pc` from cloud-init + `overlayroot`
    to live-build. The previous path stitched a stock Debian
    cloud image, an ext4 rootfs, and the `overlayroot` package's
    initramfs hook; that hook was fragile across kernel /
    hardware combos (kernel panic on GMKtec MiniBoxXS, kernel
    6.12.85+deb13-amd64). live-boot's SquashFS + tmpfs overlay is
    the canonical Debian path for ephemeral live media and is
    what `netboot-pc` already uses. Shipped over v0.2.16 ->
    v0.3.1 across seven phases.

    1. **[done]** Add an `iso-hybrid` output target to the existing
       live-build config (parallel to the current `--binary-images
       netboot` output). Output: `bty-usbboot-pc-x86_64.iso`. Most of the
       chroot is shared with `netboot-pc` - this is genuinely a
       packaging variant. Driven by a new `usb-iso` cijoe variant
       (`cijoe/configs/usb-iso.toml`, `cijoe/tasks/usbboot-pc.yaml`,
       `cijoe/scripts/usb_iso_build.py`); marked experimental in
       the ci-cd.yml media matrix until proven on real hardware.
    2. **[done]** Make `bty-on-tty1.service` graceful when no
       `bty.server` / `bty.mac` is on the kernel cmdline: the
       wrapper script forwards no flags and `bty-tui` falls back
       to scanning the local image-root. Same service, two modes
       (PXE-driven remote, USB-driven local). The usb-iso bake
       sets `bty.mode=interactive` directly via `--bootappend-live`
       so the existing service fires on USB boot the same way it
       fires for the PXE-tui flow.
    3. **[done]** Bake a writable `BTY_IMAGES` exFAT partition
       into the pre-built ISO. Post-process the live-build output:
       `truncate +4G` to extend the file, `sgdisk
       --move-second-header` + `--new` to add the partition entry
       to the GPT, `losetup -fP` + `mkfs.exfat` to format. The
       single artifact `dd`'s onto a stick with a writable area
       the operator can drop `*.img.zst` files onto from any host
       OS - same UX the legacy cloud-init `usbboot-pc` provided, but
       baked statically instead of carved by cloud-init runcmd.
    4. **[done]** Mount BTY_IMAGES at boot.
       `var-lib-bty-images.mount` mounts the partition RO at
       `/var/lib/bty/images` so `bty-tui` and the flash flow
       find it at the default image-root path - no runtime
       auto-discovery needed. (An auto-grow service used to live
       here; removed in v0.5.11 -- the live env's tmpfs sentinel
       didn't survive reboots, so the service ran every boot and
       destroyed operator data dropped between boots. Sticks now
       ship at the baked 2.1 GiB BTY_IMAGES size (shrunk from 4 GiB
       in v0.8.0 for Ventoy / piKVM / JetKVM friendliness); operators
       who want more grow with gparted on their host.)
    5. **[done]** Documented delivery options + renamed
       `live-x86` -> `netboot-pc` (both deferred work folded
       into the same commit). Stock hybrid ISO with built-in
       writable area; operators write it with their existing
       tooling (`dd`, Balena Etcher, Rufus) or drop it onto an
       existing Ventoy stick alongside their other rescue ISOs.
       The project does not ship a stick-writing tool of its
       own. The `live-x86` rename addressed a long-standing
       naming bug: both `usbboot-pc` and `live-x86` produced live
       envs, so calling one "live" was ambiguous; the actual
       distinguishing axis is delivery mechanism (USB vs PXE).
       Artifact name dropped the redundant `-netboot` suffix:
       `bty-live-x86_64-netboot/bty-live-x86_64.*` ->
       `bty-netboot-pc-x86_64/bty-netboot-pc-x86_64.*`.
    6. **[done]** Retired the `overlayroot` dependency, the
       cloud-init usbboot-pc bake (`cloudinit-base-usb.user`,
       `rootfs/usb/`, the cloud-init `usbboot-pc.toml` config,
       `docs/asciinema/usb-build.sh`), and the legacy
       `bty-usbboot-pc-x86_64-img-zst` release artifact. The `usbboot-pc`
       variant name now points at the live-build path that was
       called `usb-iso` during phases 1-5; the cijoe config moved
       from `usb-iso.toml` to `usbboot-pc.toml`. Gated on hardware
       verification of the v0.2.20 release; landed after the
       GMKtec MiniBoxXS booted cleanly with the live-build
       artifact and the BTY_IMAGES partition mounted at
       `/var/lib/bty/images`.
    7. **[done]** Made `BTY_IMAGES` visible to Windows.
       Discovered on hardware verification of v0.2.20: when the
       dd'd stick was plugged into a Windows host, the operator
       saw the ISO9660 (drive `D:`) + EFI + an "Unallocated"
       trailing region in Disk Management. The trailing region
       was the BTY_IMAGES exFAT - Windows refused to enumerate
       it because the EFI partition entry (#2) sat *inside* the
       ISO9660 partition's byte range (live-build's `iso-hybrid`
       embeds the EFI FAT image in the ISO9660 stream). Linux
       mounted BTY_IMAGES fine; Windows / Mac didn't, breaking
       the "drop images from any host OS" UX promise from phase
       3.

       Fixed in `cijoe/scripts/usb_iso_build.py::_extend_with_exfat`:
       read the EFI FAT bytes from the overlapping location, copy
       them to a non-overlapping byte range right after the
       ISO9660 partition (8-sector aligned), and rewrite the MBR
       partition table atomically with three non-overlapping
       entries (p1 ISO9660 / p2 EFI relocated / p3 BTY_IMAGES).
       The El Torito catalog inside the ISO9660 still has its
       embedded EFI image for CD-style UEFI boot; the relocated
       MBR partition entry handles USB-style UEFI boot. BIOS boot
       via `isohdpfx.bin` in MBR sectors 0..432 is untouched
       (sfdisk's stdin form only edits the partition-table area
       at offsets 446..510 and preserves the bootable flag on
       p1). Windows now enumerates all three partitions cleanly.
20. **[done]** TUI polish (helix / zellij-inspired). Three-pane
    layout (images | disks | details, with the details pane
    updating live as the cursor moves), `/` filter for the
    image catalog, onboarding panel for empty catalogs (with
    differentiated text for local vs remote sources), and a
    floating flash modal with a stop-the-world warning header
    and a stage tracker that ticks as `flash.execute_plan`
    emits each lifecycle event. Tokyo Night theme matches the
    bty mascot's navy + warm-yellow palette. Single-key direct
    bindings (q / r / f / / / escape) only - bty has so few
    actions that helix-style modal navigation would be
    overkill.

21. **[done, v0.5.13]** Docker container for `bty-web`. Multi-arch
    (amd64 + arm64) image at `ghcr.io/safl/bty-web`, published
    by the same release workflow that ships PyPI + the appliance
    images. HTTP-only by design: bty-web + image catalog +
    machine registry + browser UI; no TFTP daemon bundled. The
    container's deployment lane is HTTP-Boot / `boots-from`:

    - **UEFI HTTP Boot** -- operator's LAN DHCP serves
      ``option 67 = http://<bty>/ipxe.efi`` (bty-web serves
      iPXE binaries from ``/boot/`` over HTTP). Modern UEFI
      firmware supports this directly; no TFTP in the path.
    - **`boots-from` USB stick** (sibling project
      ``safl/boots-from``) -- the operator boots a USB whose
      embedded iPXE script chains to bty-web's
      ``/pxe-bootstrap.ipxe``. Works on legacy BIOS too,
      since the USB replaces firmware-driven PXE. Neither
      DHCP-PXE options nor a TFTP daemon are needed on the
      LAN.

    For mixed-firmware fleets that include legacy BIOS or
    older UEFI implementations that only support TFTP-via-
    option-67, deploy the bare-metal `bty-server` appliance
    instead -- it bundles ``dnsmasq`` configured for TFTP
    serving alongside bty-web. The test matrix exercises
    both lanes (appliance: SeaBIOS+TFTP and OVMF+HTTP-Boot;
    docker: OVMF+HTTP-Boot).

    The container is also the lowest-barrier-to-try shape
    (``docker run -p 8080:8080 ...`` and the UI is up) and
    adds a third lane: USB live stick + network-shared
    catalog. Operators run the container on a workstation,
    point ``bty tui --catalog URL`` at it, and pick images
    from the catalog without flashing the catalog onto every
    stick.

22. **[done, v0.6.0]** `bty-web` catalog manifest with `src`
    URLs + local SHA-verified cache. Today `/images` enumerates
    whatever lives under `BTY_IMAGE_ROOT`; an operator who wants
    to share images across a fleet has to copy bytes onto every
    `bty-web` instance. The shape of M22:

    A YAML manifest (default `${BTY_STATE_DIR}/catalog.yaml`,
    overridable via `BTY_CATALOG_FILE`) lists named images with
    upstream `src` URLs and pinned `sha256` digests:

    ```yaml
    version: 1
    images:
      - name: ubuntu-server-22.04-bty.img.zst
        src: https://github.com/safl/bty-images/releases/download/v0.1/ubuntu-22.04.img.zst
        sha256: abc123...
        format: img.zst
      - name: freebsd-14-test.img.zst
        src: https://github.com/someone/bty-freebsd/releases/download/v3/freebsd-14.img.zst
        sha256: def456...
        format: img.zst
    ```

    `bty-web`'s `/images` endpoint merges directory-scan
    entries with manifest entries (the directory scan stays the
    primary source for files dropped on a volume mount; the
    manifest adds named-with-src entries on top). On first
    request for a manifest entry, `bty-web` downloads the blob,
    verifies SHA-256 against the manifest, atomically writes it
    into `${BTY_STATE_DIR}/cache/<sha>` (cache keyed by SHA so
    duplicate hashes across manifest entries dedupe naturally),
    and serves from there. Subsequent requests hit the cache
    directly.

    This unlocks the **super-catalog pattern**: a `catalog.yaml`
    published at a stable URL (a github repo, an internal
    artifact server, anywhere) referencing artifacts spread
    across many other locations. A fleet of `bty-web` instances
    pulls the same manifest and lazily caches the blobs each
    actually flashes. Adding a new image is a manifest PR,
    not a "copy bytes to every server" exercise.

    **v1 scope** (M22 first release):

    - Schema + parser + validator (`bty catalog validate`).
    - Cache module: download, SHA-verify, atomic write,
      content-addressed storage at `cache/<sha>`. Block-and-
      serve on first fetch (no streaming-while-verifying).
    - **Download manager** -- a small in-memory scheduler that
      tracks every active fetch (entry name, status, bytes
      downloaded, total bytes, error / cancel reason),
      enforces a parallelism cap (default 2, env-overridable
      via `BTY_CATALOG_MAX_PARALLEL`), and supports cooperative
      cancellation (a downloader checks a cancel-flag between
      1 MiB chunks; aborts cleanly within seconds, leaves no
      half-written cache file). State is in-memory; server
      restart loses the queue but the cache directory remains
      the source of truth for "what's cached".
    - **SHA-keyed image identity (unified)**. Both directory-
      scan images (under ``BTY_IMAGE_ROOT``) and manifest
      entries are merged under a single concept: an *image* is
      identified by its SHA-256, optionally with one or more
      names attached (a local filename, a manifest entry name,
      or both). Directory-scan images get a sidecar
      ``<file>.sha256`` written on first discovery (computed
      lazily via the hash manager below; subsequent calls read
      the sidecar). Manifest entries already have SHA from the
      manifest. Same content-addressed cache: the live env /
      target fetches by SHA, not by name; renaming a file in
      ``BTY_IMAGE_ROOT`` does not break a binding.
    - **Hash manager (background, single worker)**. Computing
      SHA-256 on multi-GiB images takes minutes on small
      hardware (Pi 4, old NUCs, mini-PCs); inline hashing on
      the request path would block bty-web for minutes per
      operator click. A new ``HashManager`` (parallel structure
      to ``DownloadManager`` from Layer 2) enqueues hash jobs,
      runs them in a worker thread, surfaces per-job progress
      (bytes hashed / total) + cancel via the same UI shape.
      Default parallelism is **1**: two simultaneous hashes
      saturate CPU + IO on small hardware and both finish at
      half speed; serial uses the same wall clock without
      tanking responsiveness. Env-overridable via
      ``BTY_HASH_MAX_PARALLEL`` for operators on fast hosts.
      New endpoints: ``POST /catalog/hashes`` to enqueue,
      ``GET /catalog/hashes`` to list, ``DELETE
      /catalog/hashes/{name}`` to cancel. UI: "Hash" button on
      unhashed dir-scan rows, status row in the same downloads
      pane (or a sibling pane with a Job Type column).
    - **``machines.image`` becomes ``machines.image_sha256``**.
      One-shot migration on bty-web startup: rows with
      ``image IS NOT NULL AND image_sha256 IS NULL`` get the
      SHA computed from the named file (or marked unresolved
      if the file is gone, with a UI banner asking the
      operator to re-bind). iPXE rendering looks up by SHA,
      falls back to ``ipxe_unknown`` if the cache is empty
      and no manifest entry can supply it.
    - `bty-web` integration: merged `/images` listing returns
      one entry per SHA with ``names: [...]`` and ``sources:
      [...]`` arrays, plus ``cached: bool``. Lazy fetch on
      ``/images/{sha256_or_name}`` GET routed through the
      manager (so a flash request doesn't bypass the queue
      and start a Nth parallel download). New endpoints:
      ``POST /catalog/downloads`` to enqueue,
      ``GET /catalog/downloads`` to list active + recent,
      ``DELETE /catalog/downloads/{name}`` to cancel.
    - **Unified Images page** in the browser UI: drop the
      legacy ``/ui/images`` (filename-only) and the proposed
      ``/ui/catalog`` separate page; one ``/ui/images`` table
      with columns SHA prefix / Name(s) / Format / Source(s) /
      Cached / Action. Live downloads table at the bottom of
      the same page with progress bars + Cancel button.
      Auto-refreshes every ~2s via polling -- simpler than SSE
      for v1 and bty-web has no other websocket / event-stream
      dependency.
    - CLI: ``bty catalog list``, ``bty catalog fetch <name>``,
      ``bty catalog validate <file>`` for server-side manifest
      management. The local CLI (``bty images``,
      ``bty flash``) stays intentionally simple --
      directory scan only, no SHA / manifest awareness in
      operator-facing flags. (v0.6.0 briefly grew a
      ``ref:<prefix>`` resolver and a SHA column; v0.6.1
      reshaped that out: the catalog story is a server
      concern, not part of the local CLI surface.)
    - **Auto-import on bty-web startup** (added v0.6.1):
      walks ``BTY_IMAGE_ROOT`` once and enqueues a hash job
      for every dir-scan file without a sidecar. Files
      become flashable (visible in ``/images``) once
      imported. HashManager runs serially by default
      (``BTY_HASH_MAX_PARALLEL=1``); a Pi 4 with 10 unhashed
      images doesn't get hammered.
    - **Client/server shape (v0.6.1)**: ``GET /images``
      returns one entry per SHA with a single ``url`` field
      that the client (``bty tui --catalog URL``, any HTTP
      consumer) flashes from. Server URL when cached / imported,
      upstream URL when manifest+uncached. The client never
      reasons about cache state.
    - Public URLs only (HTTP / HTTPS, no auth).
    - Cache is unbounded; manual `rm` for eviction, documented
      under a hardening / maintenance section.
    - Walkthrough doc covering manifest authoring + the UI
      flow + the env knobs + the SHA-binding migration story
      for upgrading operators.

    **Out of scope for v1, captured in Future work**:

    - Streaming-with-SHA-tee for big images (faster first
      fetch; v1's block-and-serve is fine for most paths).
    - Authenticated `src` (bearer tokens for private GitHub
      repos, S3 credentials, etc.).
    - Signed catalogs (sigstore / cosign / PGP). v1 ships
      unsigned; trust model is "the operator authored / curated
      the manifest themselves, or trusts whoever did". Worth
      tightening before bty becomes attractive to attackers.
    - OCI artifact / OCI distribution spec as an alternate
      transport. Gives signing + content-addressing for free
      but is a significantly bigger lift.

    Estimated scope: ~1.5-2 days for the v1 slice.

## Future work (not yet scheduled)

These are forward-looking ideas captured for the roadmap; no
implementation is in flight.

- **`bty.dhcp` library (proxy-DHCP + TFTP in pure Python).** Not as
  a wholesale dnsmasq replacement -- dnsmasq has decades of
  edge-case wrangling for weird PXE ROMs, multi-arch (DHCP option
  93: BIOS / UEFI / UEFI-ARM), and the rogue-DHCP guard that keeps
  proxy mode from breaking other clients on the LAN. The appliance
  keeps using dnsmasq. But a focused ~700 LoC Python responder
  (parse DISCOVER, gate on PXEClient option 60, build OFFER with
  siaddr + boot-file, optional per-MAC) plus a small TFTP server
  would be valuable as a building block with two specific
  consumers: (1) a test fixture that lets ``make test-pxe`` and
  new unit tests drive PXE flows in pure Python with mocked sockets,
  vs. spinning up a full server VM with dnsmasq; and (2) an opt-in
  ``BTY_DHCP_BUILTIN=1`` mode for the Docker container, useful in
  the standalone-trial / single-laptop-reflash scenario where
  pulling in dnsmasq is awkward. Trade-off: ~700 LoC of carefully-
  tested code with focused fuzzing of the matched-on-vendor-class
  invariant so it never responds to non-PXE DISCOVERs. Worth doing
  if either (a) test-pxe slowness becomes a felt pain or (b) the
  standalone-trial Docker-with-PXE story grows into a real
  use-case; otherwise it's solving a problem that does not exist
  yet.

- **Multi-root image-root (parked, low-priority).** Today
  ``bty`` reads a single image root from ``BTY_IMAGE_ROOT`` (or
  ``/var/lib/bty/images``). Operators with images split across
  locations -- a stock USB stick's ``BTY_IMAGES`` plus a Ventoy
  partition mounted at ``/mnt``, a workstation install plus a
  downloads dir, etc. -- have to merge directories by hand or
  symlink into the single root. Future shape: an env var
  ``BTY_IMAGE_ROOTS`` (plural) accepting colon-separated paths
  and ``bty.images`` accepting an iterable; the wizard's image
  list shows the merged rows.
  Open question on name collisions: easiest rule is first-root-
  wins with a one-line ``foo.qcow2: shadowed in /mnt/ventoy``
  status note so accidents surface; alternatively show the
  parent dir as a column. Server-side ``BTY_IMAGE_ROOT`` stays
  singular for now -- multi-root would need URL-disambiguation
  rules at the ``/images/{name}`` endpoint, a bigger
  conversation. Estimated scope: ~2 hours including tests +
  welcome-panel update. Implement when an operator actually
  hits the multi-source pain; today's workaround is a manual
  ``cp`` or symlink.

- **`.btycatalog` marker for opt-in catalog auto-discovery.**
  Today bty finds image catalogs by either an explicit
  ``--image-root`` / ``BTY_IMAGE_ROOT`` / well-known appliance
  path, or by partition label (``BTY_IMAGES``). That covers
  stock USB sticks and the appliance, but breaks down for
  multi-mount scenarios -- Ventoy data partitions (labeled
  ``Ventoy``), IP-KVM mounts of additional disks, operators
  with several catalogs in different directories, etc. A
  ``.btycatalog`` sentinel file in any directory would mark it
  as an intended bty catalog; the live env's startup hook
  would scan attached USB partitions for the marker, mount
  any that have it, and merge their contents as catalog
  sources. Important nuance: extension scanning stays in
  charge inside already-pointed-at directories so a directly-
  configured ``--image-root /mnt`` does not need the marker.
  The marker is only meaningful for the auto-discovery path.
  Empty file is fine v1; the file can later carry metadata
  (display name, target-arch hint, default boot policy)
  the way ``Cargo.toml`` / ``package.json`` accreted features
  over time. Estimated scope: half-day -- ``bty.images``
  helper + live-env mount hook + tests + doc convention.
  Worth doing if the Ventoy / multi-source scenario actually
  shows up in operator practice; otherwise it solves a
  problem that does not yet exist.

- **PXE-boot a kernel image (no flash).** Today's PXE flow is
  always "boot the netboot live env, run `bty flash`, reboot
  into local disk". A complementary mode would PXE-boot a
  kernel + initrd + squashfs trio that the target *runs*
  persistently from the network, never writing to local disk.
  Use cases: diskless CI runners that fetch a fresh OS every
  job (no state to drift); rescue / recovery boots to a
  known-good environment; lab benchmarking on machines that
  have no disk or shouldn't be written to. The artifacts
  already exist (`netboot-pc` variant) and `bty-web`'s
  per-MAC `boot_mode` field already routes to different
  iPXE scripts; this mode would add a new mode value (e.g.
  `boot_mode=netrun`) that renders an iPXE script that
  hands off to the kernel without scheduling a flash. The
  trickier piece is what the hand-off OS *does* on the
  network-mounted root, which is downstream of bty itself.

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
