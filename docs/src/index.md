```{image} _static/bty-mascot.png
:alt: bty mascot - a blue bat holding a PXE handshake card and a disk labelled .qcow2 / .img / .raw
:width: 240px
:align: center
```

# bty - flash images onto target disks, offline or networked with and without PXE

> Pronounced "battie" (rhymes with "batty") - the blue bat up top is the
> mascot, so when in doubt say it like the critter.

```{only} html
[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/safl/bty/blob/main/LICENSE)
```

Flash a single bare-metal box ad-hoc with a USB stick, or reflash a whole
fleet remotely from a single controller. bty works with or without PXE and
scales from one machine to a rack without changing how you operate. The
image is the source of truth: rebuild the image, reflash the target. No
imperative configuration management, no idempotency mind games. Works in
homelabs, CI fleets, lab benches, data-centre racks, and anywhere else
bytes need to land on a disk.

bty is a flasher, not an image builder:

- **Image creation is somebody else's project.** First-boot bring-up
  (users, network, packages, hostnames) gets baked into the image upstream
  with cloud-init / kickstart / preseed / your favourite image builder. Use
  the [companion image-builder](https://github.com/safl/nosi) (`safl/nosi`,
  which builds Debian / Ubuntu / Fedora sysdev images and publishes them to
  GHCR as ORAS artifacts bty flashes via `oras://`), or your own. bty just
  writes the bytes.
- **No post-boot configuration management either.** Anything that must be
  true on the running target (users, hostnames, config files, packages)
  belongs in the image builder, not bty. The server holds no creds for any
  target it provisioned; that blast radius is intentionally absent.

```bash
# Local: USB stick into target, two arrows + Enter, done.
bty

# Remote: bind a MAC to an image, the next PXE boot reflashes itself.
# (See the bty-web HTTP API reference for the full surface.)

# Per-job CI: every job a clean OS, no drift, no snowflakes.
```

## Three delivery shapes, one runtime

| Shape | What it is | When it fits |
|---|---|---|
| **USB live stick** | bty boots from a flash drive, runs `bty`, flashes the box it's plugged into. Fresh sticks ship with four starter `.bri` pointers (Debian / Ubuntu / Fedora sysdev images via `oras://ghcr.io/safl/nosi/...`, plus bty-server) so the catalog is non-empty out of the box. | Single-machine local imaging |
| **USB + portable catalog** | Same stick, plus `bty --catalog <SOURCE>` pointed at a TOML catalog hosted anywhere (a local file, an HTTP URL, an `oras://` reference, or a bty-web instance's `/catalog.toml`). | A handful of boxes, shared image library |
| **PXE-boot appliance** | bty-web on a Pi or x86 box runs DHCP/TFTP/HTTP; targets PXE-chain into a netboot live env that runs `bty --server X --mac Y` on tty1, which fetches a per-MAC plan and either auto-flashes or drops the operator into the wizard | CI fleets, racks, anything you don't want to walk to |

All three share the same Python codebase, the same image catalog, the
same SHA-keyed machine bindings.

## ORAS-published images and portable catalogs

bty consumes images and catalogs as **OCI artifacts** published with
[ORAS](https://oras.land/) (OCI Registry As Storage, the spec for
non-container artifacts in a container registry). The end-to-end story:

- **Images live in a registry.**
  [`safl/nosi`](https://github.com/safl/nosi) publishes Debian / Ubuntu /
  Fedora disk images to `ghcr.io/safl/nosi/<variant>:latest`. `bty`
  resolves an `oras://ghcr.io/safl/nosi/...` source, picks the disk-image
  layer, and streams the blob to the target via the same `curl | dd`
  pipeline as any HTTP URL. Anonymous-pull only: no PAT, no docker login.
- **Catalogs are portable TOML files.** A catalog is a small TOML manifest
  listing named images with `src` URLs (any mix of `http(s)://`,
  `oras://`, or `file://`). `bty --catalog <SOURCE>` accepts a local path,
  an HTTP URL, or an `oras://` reference. Operators can publish a catalog
  on GitHub Releases, an S3 bucket, a private registry, or alongside images
  in GHCR. `bty-web` instances serve the same shape at `GET /catalog.toml`,
  so a running server is "just another catalog source".
- **`.bri` descriptors are the per-stick analogue.** A USB stick's
  `BTY_IMAGES` partition can carry `.bri` files (one-image-per-file TOML
  pointers, including `oras://` URLs). `bty` merges them with whatever
  `--catalog` source the operator passed.

Why this shape: images and catalog metadata are content-addressed
artifacts, not container images. The OCI ecosystem already solves
"distribute signed, versioned, content-addressed blobs"; bty piggybacks on
that without dragging in the docker / podman runtime.

## Why bty

- **Reflash on every CI job.** Each job lands on a freshly-imaged target,
  runs, gets reflashed for the next. No state leaks, no snowflakes, no
  "works on my machine" because the machine is bit-identical to the
  manifest every boot.
- **Pre-built images, not recipes.** You build the image once (in your
  build system of choice), bty writes the bytes. Any first-boot bring-up
  (users, networking, hostnames) is baked into the image upstream via
  cloud-init / NoCloud user-data. bty runs no provisioning step: no agent,
  no daemon, no convergence loops.
- **OS-agnostic by design.** Linux, FreeBSD, Windows: if it boots from a
  disk image, bty can flash it. macOS targets are out (Apple Silicon's boot
  story isn't friendly to imaging).
- **Trust model is explicit.** PXE / live-env routes are open (clients have
  no token); operator routes (`/machines`, `/catalog/*`, `/boot/releases`)
  require a session cookie. bty-web is for trusted networks (homelab, CI
  segment), not the open internet.

```{toctree}
:maxdepth: 2
:caption: Get started

overview
quickstart
walkthrough-usb
walkthrough-server
walkthrough-server-docker
walkthrough-catalog
walkthrough-image-store
```

```{toctree}
:maxdepth: 2
:caption: Reference

concepts
flows
components
operations
dependencies
related
reference
```
