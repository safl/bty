# bty - flash images onto target disks, locally or over PXE

Bare-metal provisioning toolkit. Flashes pre-built ("cooked") system
images onto target disks - locally from a USB stick or remotely over
PXE - and configures them via cloud-init or CIJOE workflows.

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
MAC-keyed assignment model, the no-SSH web UX, the iPXE network flash, the
state export/import - exists to make those three cadences cheap, fast, and
boring.

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
image catalog metadata, server settings) is persisted on disk and exposed
through the UI as **export** (download a single archive) and **import**
(upload to restore). This makes disaster recovery and migration between
server hosts a two-click operation.

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
`safl/jellyfin-kiosk-appliance-builder` (jkab): cijoe-driven Debian
appliance build, Makefile-orchestrated, with `configs/`, `rootfs/`,
`scripts/`, `tasks/`, and `tests/` subdirectories.

Two artifacts:

**USB live image.** A bootable USB stick carrying the `bty` CLI, `bty-tui`,
and a bundled set of system images. The operator plugs it into a target
machine, boots it, and runs `bty flash` against the target's local disk
using images sourced from the stick itself. Self-contained and offline -
no network or external server required. This is the direct-flash flow's
delivery vehicle.

**Server image.** An installable disk image that, when written to a host's
disk and booted, runs the bty provisioning server: `bty-web`, the iPXE /
TFTP / HTTP services that PXE clients chain through, the network-flash live
environment those clients boot into, and a storage layout for the image
library. One artifact, ready to serve a fleet. This is the network-flash
flow's delivery vehicle.

The intended operator experience is appliance-grade:

1. `dd` (or `bty flash`) the image onto the server host's disk.
2. Boot. Network comes up via DHCP; cloud-init handles the bare minimum
   (hostname, SSH key) on first boot.
3. Open the web UI in a browser. A first-boot wizard captures the handful
   of options that cannot be sensibly defaulted (image library location,
   network interface for PXE serving, admin credential).
4. From that point on, the server is configured entirely through the web
   UI - no SSH, no config files, no package installs. State is persisted
   on disk and recoverable via the export/import flow described under
   `bty-web`.

*Hardware targets.* The server image is built for `amd64` only. Targeted
hardware is the kind of small x86 box that already lives in homelabs and
labs: older Intel NUCs, discarded 1U servers, recent GMKtec mini-PCs, and
similar. The same artifact also boots as a VM disk for operators who would
rather not dedicate hardware. The image format is `.img.zst` so a single
download covers `dd`-to-disk and virtual-disk deployment alike.

*On `arm64`.* Building an arm64 variant (for Raspberry Pi or other SBCs)
is a feasible extension - the runtime itself is portable - but it requires
a separate base image and build pipeline, so it is intentionally out of
scope for the initial roadmap. If there is interest, it can be added as a
parallel artifact later.

Both artifacts are produced by the same `bty-media/` directory - they
share the Debian-based build pipeline and the embedded `bty` runtime.

## Image formats

`.qcow2`, `.img`, `.img.zst`

## Provisioning modes

After the image is written to disk, bty can hand off to a first-boot
configuration mechanism. Three modes:

- **`none`** - no post-flash configuration. Reboot into the cooked image
  as-is.
- **`cloud-init`** - populate the OS's cloud-init seed (NoCloud
  datasource) with operator-supplied user-data and meta-data; the OS picks
  it up on first boot. Linux and FreeBSD today; the Windows analogue
  (unattend) occupies the same slot when Windows lands.
- **`cijoe`** - run a CIJOE workflow that adjusts the deployed system to a
  known-good state. CIJOE is bty's official extension point for deviations
  from a stock image: vendor-specific tweaks, licence files, IPMI
  credentials, fleet-specific tuning that should not be baked into the
  image itself.

`cijoe` runs in one of two execution modes depending on the deployment
vehicle:

- **Offline (USB live).** The workflow runs from the live environment
  after the flash, against the freshly-written filesystem (mount, edit,
  unmount), before the target reboots. Customisation is constrained to
  what is possible by manipulating the filesystem from the outside - file
  edits, package staging, seed-file drops.
- **Online (PXE / server).** After the target first-boots into its own
  OS, `bty-web` triggers a CIJOE workflow against the running machine and
  records the post-workflow state as that machine's known-good baseline.
  The server - not the image - becomes the source of truth for *"what this
  box is supposed to look like,"* which is what closes the loop on the
  per-job and on-failure cadences from the Motivation section.

## Concepts

- **Image** - a system image file in one of the supported formats, residing
  in a configured image root.
- **Target** - a block device on the machine being provisioned.
- **Provisioning mode** - what (if anything) runs on first boot.
- **Machine record** (web only) - MAC-address-keyed assignment of image +
  provisioning mode + optional hostname.

## Flows

### Direct flash (CLI/TUI)

Operator boots the target machine from bty live media (USB or network),
then runs `bty` locally:

```
sudo bty flash --image IMG --target /dev/sda --provision cloud-init ...
```

### Network flash (web)

1. Operator assigns `MAC -> image + provisioning` in the web UI.
2. Target machine PXE-boots; iPXE chains into the bty live environment over
   HTTP.
3. bty live env contacts the server, fetches a per-MAC bootstrap, flashes the
   target disk, applies provisioning.
4. Server rewrites the per-MAC iPXE config to "boot local disk" so subsequent
   reboots do not reflash.

Both BIOS and UEFI clients are supported via iPXE.

## Repository layout

One Python project at the repo root (src layout, hatchling build backend),
plus a sibling appliance-image builder and a docs tree. `uv` manages the
project venv and the lockfile.

```
bty/
+-- pyproject.toml          # one [project] = "bty-lab" with optional extras
+-- uv.lock                 # committed
+-- PLAN.md
+-- README.md
+-- LICENSE                 # GPL-3.0-only
+-- src/
|   \-- bty/                # the Python package
|       +-- __init__.py
|       +-- cli.py          # bty console script
|       +-- tui/            # bty-tui console script (extra: tui)
|       \-- web/            # bty-web console script (extra: web)
+-- tests/
+-- docs/
|   +-- README.md
|   +-- src/                # MyST + Sphinx sources
|   \-- tooling/            # bty-docs-* commands (pipx install ./tooling)
+-- bty-media/              # sibling appliance builder, NOT a Python pkg
|   +-- README.md
|   +-- Makefile
|   +-- configs/
|   +-- rootfs/
|   +-- scripts/
|   +-- tasks/              # cijoe workflows
|   \-- tests/
\-- .github/
    \-- workflows/
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

- **`v*` tags** - single unified release. `uv build` produces the wheel
  and sdist (PyPI publish via trusted publishing); the same workflow
  builds all three `bty-media` variants in parallel (usb, server, live)
  and attaches every artifact to the GitHub release at the same tag.
  Operators get one release page covering the whole stack at one
  version; `/ui/boot`'s "fetch latest" pulls the matching live trio.

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

1. Repo skeleton - clear out legacy; lay out the single Python package
   (src layout) with `bty`/`bty-tui`/`bty-web` console scripts and
   optional extras; sibling `bty-media/` directory in the jkab pattern;
   `docs/` wired up to the aisio-style tooling (pipx-installed); reusable
   CI workflows (lint, type-check, test, docs build, release skeleton).
2. `bty-media` USB live build pipeline - cijoe-driven, mirrors the jkab
   pattern (Debian cloud-image base, cloud-init bake in QEMU,
   `qcow2 -> raw -> .img.zst`). Triggered via `workflow_dispatch` and
   on `media-*` tags rather than every push, since the build is heavy
   (~15-25 minutes with KVM, ~1 GB compressed artifact). Bty content
   can be stub-level at this stage; the goal is to materialise the
   build pipeline first and fill it as later milestones land.
3. `bty list disks` - block-device discovery.
4. `bty list images` and `bty inspect image IMAGE` - image catalog and
   metadata.
5. `bty flash --dry-run` - validation without writing.
6. `bty flash` - real flashing for `.qcow2`, `.img`, `.img.zst`. From this
   milestone onward the USB live image produced by milestone 2 is genuinely
   useful for direct flashing.
7. Provisioning: `none`.
8. Provisioning: `cloud-init`.
9. Provisioning: `cijoe` (offline mode - filesystem manipulation from the
   USB live env).
10. `bty-tui`.
11. `bty-web` server - MAC-keyed assignment, per-MAC iPXE config rendering.
12. `bty-web` UI - browser front-end for the server.
13. `bty-media` server image - installable disk image hosting `bty-web`,
    iPXE/TFTP/HTTP services, the network-flash live environment, and the
    image library.
14. Network-flash end-to-end - iPXE -> bty live -> flash -> reboot,
    BIOS + UEFI.
15. Provisioning: `cijoe` (online mode - `bty-web` triggers a workflow
    against the booted target and records the post-workflow state as the
    machine's known-good baseline).

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
