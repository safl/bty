<p align="center">
  <img src="docs/src/_static/bty-mascot.png" alt="bty mascot - a blue bat holding a PXE handshake card and a disk labelled .qcow2 / .img / .raw" width="240">
</p>

# bty - flash images onto target disks, offline or networked with and without PXE

> Pronounced "battie" (rhymes with "batty") - the blue bat up top is the
> mascot, so when in doubt say it like the critter.

[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![Documentation](https://img.shields.io/badge/docs-safl.dk%2Fbty-blue)](https://safl.dk/bty)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fsafl%2Fbty--web-blue)](https://github.com/safl/bty/pkgs/container/bty-web)

Flash a single bare-metal box ad-hoc with a USB stick, or reflash a
whole fleet remotely from a single controller -- bty works with or
without PXE and scales from one machine to a rack without changing how
you operate. The image is the source of truth: rebuild the image,
reflash the target. No imperative configuration management, no
idempotency mind games. Works equally well in homelabs, CI fleets, lab
benches, data-centre racks, and anywhere else bytes need to land on a
disk.

bty is a flasher, not an image builder:

- **Image creation is somebody else's project.** First-boot bring-up
  (users, network, packages, hostnames) gets baked into the image
  upstream with cloud-init / kickstart / preseed / your favourite
  image builder. Use the [companion image-builder](https://github.com/safl/nosi)
  (`safl/nosi` -- builds Debian / Ubuntu / Fedora sysdev images and
  publishes them to GHCR as ORAS artefacts that bty flashes via
  `oras://`), or your own. bty just writes the bytes.
- **No post-boot configuration management either.** Anything that
  needs to be true on the running target (users, hostnames, config
  files, packages) belongs in the image builder, not in bty. The server
  does not hold creds for any target it has provisioned -- that
  blast radius is intentionally absent.

```bash
# Local: USB stick into target, two arrows + Enter, done.
bty tui

# Remote: bind a MAC to an image, the next PXE boot reflashes itself.
# (See the bty-web HTTP API reference in the docs for the full surface.)

# Per-job CI: every job a clean OS, no drift, no snowflakes.
```

## Three delivery shapes, one runtime

| Shape | What it is | When it fits |
|---|---|---|
| **USB live stick** | bty boots from a flash drive, runs `bty tui`, flashes the box it's plugged into. Fresh sticks ship with four starter `.bri` pointers (Debian / Ubuntu / Fedora sysdev images via `oras://ghcr.io/safl/nosi/...`, plus bty-server) so the catalog is non-empty out of the box. | Single-machine local imaging |
| **USB + portable catalog** | Same stick, plus `bty tui --catalog <SOURCE>` pointed at a TOML catalog hosted anywhere (a local file, an HTTP URL, an `oras://` reference, or a bty-web instance's `/catalog.toml`). | A handful of boxes, shared image library |
| **PXE-boot appliance** | bty-web on a Pi or x86 box runs DHCP/TFTP/HTTP; targets PXE-chain into a netboot live env that flashes them unattended | CI fleets, racks, anything you don't want to walk to |

All three share the same Python codebase, the same image catalog, the
same SHA-keyed machine bindings.

The PXE-boot appliance also separates rootfs from image cache: drop a
2nd disk in, run `sudo bty-image-store-init /dev/sdX` once, and the
image library survives appliance reflashes. The new appliance auto-
mounts the labelled disk at `/var/lib/bty/images`; no operator action
required.

## ORAS-published images and portable catalogs

bty consumes images and catalogs as **OCI artefacts** published with
[ORAS](https://oras.land/) (OCI Registry As Storage -- the spec for
non-container artefacts in a container registry). The end-to-end
story:

- **Images live in a registry.** [`safl/nosi`](https://github.com/safl/nosi)
  publishes Debian / Ubuntu / Fedora disk images to `ghcr.io/safl/nosi/<variant>:latest`.
  `bty flash oras://ghcr.io/safl/nosi/debian-sysdev:latest /dev/sdX --yes`
  resolves the manifest, picks the disk-image layer, and streams the
  blob straight to the target via the same `curl | dd` pipeline as
  any HTTP URL. Anonymous-pull only -- no PAT, no docker login.
- **Catalogs are portable TOML files.** A catalog is a small TOML
  manifest listing named images with `src` URLs (any combination of
  `http(s)://`, `oras://`, or `file://`). `bty tui --catalog
  <SOURCE>` accepts a local path, an HTTP URL, or an `oras://`
  reference. Operators can publish a catalog on GitHub Releases, an
  S3 bucket, a private registry, or alongside images in GHCR --
  whatever they already have. `bty-web` instances serve the same
  shape at `GET /catalog.toml`, so a running server is "just another
  catalog source".
- **`.bri` descriptors are the per-stick analogue.** A USB stick's
  `BTY_IMAGES` partition can carry `.bri` files (one-image-per-file
  TOML pointers, including `oras://` URLs). The TUI merges them
  with whatever `--catalog` source the operator passed.

Why this shape: images and catalog metadata are content-addressed
artefacts, not container images. The OCI ecosystem already solves
"distribute signed, versioned, content-addressed blobs"; bty just
piggybacks on that without dragging in the docker / podman runtime.

## Why bty

- **Reflash on every CI job.** Per-job cadence: each job lands on a
  freshly-imaged target, runs, gets reflashed for the next job. No
  state leaks. No snowflakes. No "works on my machine" because the
  machine is bit-identical to the manifest every single boot.
- **Pre-built images, not recipes.** You build the image once (in your
  build system of choice), bty writes the bytes. Any first-boot
  bring-up (users, networking, hostnames) is baked into the image by
  the image builder upstream via cloud-init / NoCloud user-data.
  bty itself doesn't run a provisioning step -- no agent, no daemon,
  no convergence loops.
- **OS-agnostic by design.** Linux, FreeBSD, Windows - if it boots
  from a disk image, bty can flash it. macOS targets are out (Apple
  Silicon's boot story isn't friendly to imaging).
- **Trust model is explicit.** PXE / live-env routes are open
  (clients have no token); operator routes (`/machines`,
  `/catalog/*`, `/boot/releases`) require a session cookie. bty-web
  is for trusted networks (homelab, CI segment), not the open
  internet.

## Try it without flashing anything

A multi-arch container is published on every release:

```bash
docker run -d --name bty-web -p 8080:8080 -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
# -> http://localhost:8080/ui   (login: bty / bty)
```

HTTP-only - no TFTP daemon bundled in the container. The
container's lane is **UEFI HTTP Boot** (operator's DHCP serves
option 67 = `http://<bty>:8080/ipxe.efi`) or pairing with a
[`boots-from`](https://github.com/safl/boots-from) USB stick
(operator boots the stick, embedded iPXE chains to bty's HTTP
endpoint). For fleets that need TFTP (legacy BIOS + UEFI
firmware that only does TFTP option 67), use the
**`bty-server` appliance** -- it bundles dnsmasq for TFTP
serving alongside bty-web.
See [`docs/src/walkthrough-server-docker.md`](docs/src/walkthrough-server-docker.md)
for bind-mount permissions, env vars, and password rotation.

## Install

bty is one Python package - [`bty-lab`](https://pypi.org/project/bty-lab/) on
PyPI - with three console scripts:

```bash
pipx install bty-lab            # `bty` CLI, zero third-party deps
pipx install "bty-lab[tui]"     # adds `bty-tui` (Rich)
pipx install "bty-lab[web]"     # adds `bty-web` (FastAPI + Pydantic)
pipx install "bty-lab[all]"     # everything
```

`lsblk -d -e7`, `bty inspect`, `bty flash --dry-run` need only
Python 3.11+ and stdlib. `bty flash --yes` shells out to `dd`,
`qemu-img`, `zstd`, `lsblk`, `curl` (used by URL / `oras://` fetch),
and friends - your distro provides those.

For an appliance you can boot directly (USB stick, server image,
PXE-chain live env), grab the bake from
[GitHub Releases](https://github.com/safl/bty/releases). The
appliance builder lives under [`bty-media/`](bty-media/).

## Status

Pre-1.0 but actively shipping. Every tag publishes wheels (PyPI),
appliance images, and the bty-web container. The end-to-end PXE flow
(server + netboot live env + target flash + completion signal) runs
in CI on every push. CLI flags and wire formats may still shift
between minor versions until 1.0 - watch the `schema_version` field
on `--json` output and the `Machine` wire type. The
[`PLAN.md`](PLAN.md) tracks the roadmap milestone by milestone.

## Development

```bash
pipx install uv
uv sync --all-extras --group dev
uv run pytest                    # full suite
uv run ruff check                # lint
uv run mypy src                  # types
```

The docs tooling installs separately:

```bash
pipx install ./docs/tooling
cd docs
bty-docs-serve                   # live-rebuild dev server on :8000
bty-docs-build-html              # one-shot HTML build
bty-docs-build-pdf               # one-shot PDF (requires LaTeX)
```

## More

- [`PLAN.md`](PLAN.md) - roadmap and design intent.
- [`docs/`](docs/) - full documentation (Sphinx + MyST), also at
  [`safl.dk/bty`](https://safl.dk/bty).
