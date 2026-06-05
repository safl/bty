<p align="center">
  <img src="docs/src/_static/bty-mascot.png" alt="bty mascot - a blue bat holding a PXE handshake card and a disk labelled .qcow2 / .img / .raw" width="240">
</p>

# bty - flash images onto target disks, offline or networked with and without PXE

> Pronounced "battie" (rhymes with "batty") - the blue bat up top is the
> mascot, so when in doubt say it like the critter.

[![CI](https://github.com/safl/bty/actions/workflows/ci-cd.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci-cd.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![Documentation](https://img.shields.io/badge/docs-safl.dk%2Fbty-blue)](https://safl.dk/bty)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fsafl%2Fbty--web-blue)](https://github.com/safl/bty/pkgs/container/bty-web)
[![Changelog](https://img.shields.io/badge/changelog-CHANGELOG.md-blue)](https://github.com/safl/bty/blob/main/CHANGELOG.md)

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
  (`safl/nosi` -- builds Debian / Ubuntu / Fedora / FreeBSD headless
  images (plus a Fedora desktop) and
  publishes them to GHCR as ORAS artifacts that bty flashes via
  `oras://`), or your own. bty just writes the bytes.
- **No post-boot configuration management either.** Anything that
  needs to be true on the running target (users, hostnames, config
  files, packages) belongs in the image builder, not in bty. The server
  does not hold creds for any target it has provisioned -- that
  blast radius is intentionally absent.

```bash
# Local: USB stick into target, two arrows + Enter, done.
bty

# Remote: bind a MAC to an image, the next PXE boot reflashes itself.
# (See the bty-web HTTP API reference in the docs for the full surface.)

# Per-job CI: every job a clean OS, no drift, no snowflakes.
```

## Three delivery shapes, one runtime

| Shape | What it is | When it fits |
|---|---|---|
| **USB live stick** | bty boots from a flash drive, runs `bty`, flashes the box it's plugged into. Fresh sticks ship with a starter `catalog.toml` (Debian / Ubuntu / Fedora / FreeBSD headless images plus a Fedora desktop, via `oras://ghcr.io/safl/nosi/...`) so the wizard's image picker is non-empty out of the box. | Single-machine local imaging |
| **USB + portable catalog** | Same stick, plus `bty --catalog <SOURCE>` pointed at a TOML catalog hosted anywhere (a local file, an HTTP URL, an `oras://` reference, or a bty-web instance's `/catalog.toml`). | A handful of boxes, shared image library |
| **PXE-boot server** | `uvx bty-lab init ./bty-host && cd bty-host && cp .env.example .env && "${EDITOR:-vi}" .env && podman compose up -d` brings up bty-web + withcache on a Pi or x86 box -- no clone required. An optional tftp sidecar covers legacy BIOS, and your LAN DHCP points PXE clients at the host. Targets PXE-chain into a netboot live env that runs `bty --server X --mac Y` on tty1, which fetches a per-MAC plan and either auto-flashes or drops the operator into the wizard. See [`deploy/README.md`](deploy/README.md). | CI fleets, racks, anything you don't want to walk to |

All three share the same Python codebase, the same image catalog, the
same SHA-keyed machine bindings.

The container deploy keeps rootfs separate from the image cache:
`/var/lib/bty` is a named volume that survives container restarts and
re-pulls, and the image cache can be delegated to the withcache
sidecar so multiple targets pull each image once. See
[`deploy/README.md`](deploy/README.md) for the volume layout.

## ORAS-published images and portable catalogs

bty consumes images and catalogs as **OCI artifacts** published with
[ORAS](https://oras.land/) (OCI Registry As Storage -- the spec for
non-container artifacts in a container registry). The end-to-end
story:

- **Images live in a registry.** [`safl/nosi`](https://github.com/safl/nosi)
  publishes Debian / Ubuntu / Fedora disk images to
  `ghcr.io/safl/nosi/<variant>:latest`. `bty` resolves an
  `oras://ghcr.io/safl/nosi/...` source from a catalog entry, picks
  the disk-image layer, and streams the blob straight to the target
  via the same `curl | dd` pipeline as any HTTP URL. Anonymous-pull
  only -- no PAT, no docker login.
- **Catalogs are portable TOML files.** A catalog is a small TOML
  manifest listing named images with `src` URLs (any combination of
  `http(s)://`, `oras://`, or `file://`). `bty --catalog <SOURCE>`
  accepts a local path, an HTTP URL, or an `oras://` reference.
  Operators can publish a catalog on GitHub Releases, an S3 bucket,
  a private registry, or alongside images in GHCR -- whatever they
  already have. `bty-web` instances serve the same shape at
  `GET /catalog.toml`, so a running server is "just another catalog
  source".
- **One catalog format end to end.** A USB stick's `BTY_IMAGES`
  partition can carry a `catalog.toml` alongside image files; the
  wizard discovers + merges the local catalog with whatever
  `--catalog` source the operator passed. Same schema as the
  server-published catalog, no separate per-stick format.

Why this shape: images and catalog metadata are content-addressed
artifacts, not container images. The OCI ecosystem already solves
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

  Note that interactive picks (operator chooses an image at tty1)
  are not reported back to the server: bty-server tracks "what
  image is this MAC supposed to have" only when a flash policy
  (`boot_policy=bty-flash-always` / `bty-flash-once`) binds it.
  Interactive runs are operator-driven and stay local.
  See `docs/src/concepts.md` for the asymmetry.
- **OS-agnostic by design.** Linux, FreeBSD, Windows - if it boots
  from a disk image, bty can flash it. macOS targets are out (Apple
  Silicon's boot story isn't friendly to imaging).
- **Trust model is explicit.** PXE / live-env routes are open
  (clients have no token); operator routes (`/machines`,
  `/catalog/*`, `/boot/releases`) require a session cookie. bty-web
  is for trusted networks (homelab, CI segment), not the open
  internet.

## Stand up a bty server

The canonical deploy is two containers (`bty-web` + `withcache`, plus
an optional `bty-tftp` sidecar). With `uv` (or `pipx`) on the host, no
clone required:

```bash
uvx bty-lab init ./bty-host             # writes compose.yml + .env.example + README
cd bty-host
cp .env.example .env
"${EDITOR:-vi}" .env                            # set HOST_ADDR + WITHCACHE_ADMIN_PASSWORD
podman compose up -d
#   bty-web:   http://<host>:8080/ui   (UI gated by BTY_ADMIN_PASSWORD)
#   withcache: http://<host>:3000/

# BIOS PXE clients also need TFTP (UEFI HTTP Boot does not):
podman compose --profile tftp up -d
```

`init` pins the `bty-web` and `bty-tftp` image tags to the bty CLI
version that ran it -- compose and image bytes are guaranteed to
match. Re-run with `--force` to refresh against a newer release.
Add `--systemd` for Podman Quadlet units that auto-start on boot. See
[`deploy/README.md`](deploy/README.md) and
[`docs/src/walkthrough-server-docker.md`](docs/src/walkthrough-server-docker.md)
for bind-mount layout, env vars, and Quadlet install steps.

## Install

bty is one Python package - [`bty-lab`](https://pypi.org/project/bty-lab/) on
PyPI - with two console scripts:

```bash
pipx install "bty-lab[tui]"     # `bty` (Rich-based wizard, the
                                #  operator-facing tool)
pipx install "bty-lab[web]"     # adds `bty-web` (FastAPI + Pydantic,
                                #  the HTTP controller)
pipx install "bty-lab[all]"     # everything
```

`bty` shells out to `dd`, `qemu-img`, `zstd`, `lsblk`, `curl` (used
by URL / `oras://` fetch), and friends - your distro provides
those. The dispatch surface is intentionally narrow: bare `bty`
launches the local-image wizard, `bty --catalog URL` pre-loads a
catalog, `bty --server X --mac Y` fetches a per-MAC plan from the
server and dispatches (auto-flash / interactive / no-op).

For media you can boot directly (USB flasher stick, PXE-chain
netboot live env), grab the bake from
[GitHub Releases](https://github.com/safl/bty/releases). The
media builder lives under [`bty-media/`](bty-media/). To run
bty-web as a controller, use the container deploy under
[`deploy/`](deploy/).

## Status

Pre-1.0 but actively shipping. Every tag publishes wheels (PyPI),
boot media (USB flasher + netboot live env), and the bty-web
container. The end-to-end PXE flow
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
