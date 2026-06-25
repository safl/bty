# Dependencies

What bty needs at runtime, organised by what you're trying to do. The
`bty-lab` PyPI package has no third-party Python dependencies in its bare
install; the bigger pieces (Rich for the wizard, FastAPI for the web
server) come in via extras and lazy-load only when those entry points are
used.

## To install bty itself

```bash
pipx install "bty-lab[tui]"     # `bty` wizard (Rich-based; the
                                #  operator-facing tool)
pipx install "bty-lab[web]"     # adds `bty-web` (FastAPI, Uvicorn)
pipx install "bty-lab[all]"     # everything
```

Python 3.11+ is the only hard requirement. PyPI hosts pure-Python wheels;
the `[tui]` / `[web]` extras pull in their own pure-Python deps with no
native build step on install.

## To run `bty` (the wizard)

The wizard shells out to a handful of system binaries that almost every
Linux distribution already has:

| Binary | Used for | Debian/Ubuntu pkg |
|---|---|---|
| `qemu-img` | inspect image format / virtual size | `qemu-utils` |
| `lsblk` | enumerate target disks | `util-linux` (always present) |
| `blockdev` | target disk size | `util-linux` |
| `dd` | the actual write | `coreutils` (always present) |
| `partprobe` | re-read partition table after flash | `parted` |
| `zstd` | decompress `.img.zst` images | `zstd` |
| `gzip` | decompress `.img.gz` images | `gzip` (always present) |
| `xz` | decompress `.img.xz` images | `xz-utils` |
| `curl` | stream URLs (`http://`, `https://`, `oras://`) into the flash pipeline | `curl` |

Flashing a real disk requires root (`sudo bty`, or running inside the bty
live env where root is already there).

The `--catalog SOURCE` mode (a local TOML path, HTTP URL, or `oras://`
reference) adds nothing host-side: it's a plain HTTP client, and `oras://`
uses stdlib urllib through `withcache.oras` (the OCI registry adapter
bty re-uses from its sibling [withcache](https://github.com/safl/withcache)
since v0.59.0).

## To run bty-web

The `[web]` extra (fastapi, uvicorn, jinja2 are pulled in
by pip) plus:

- `$BTY_ADMIN_PASSWORD` to gate the operator UI (constant-time compare).
  When it is unset the UI is open and bty-web logs a startup warning;
  rotate by changing the env var and restarting bty-web.
- `qemu-img` (above) so the server can inspect uploaded images.

## To run the PXE-flash server

Run `bty-web` (and [withcache](https://github.com/safl/withcache)) as
containers with podman; the only host dependency is the container runtime.
See [`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
for the compose / Quadlet layout.

| Service | Image | Used for |
|---|---|---|
| bty-web | `ghcr.io/safl/bty-web` | UI, per-MAC PXE plans, boot artifacts, and images over HTTP |
| withcache | `ghcr.io/safl/withcache` | URL-keyed artifact cache; bty's preferred image source |
| tftp *(profile `tftp`)* | `ghcr.io/safl/bty-tftp` | serves the ~1 MB iPXE bootfile over TFTP for BIOS / legacy clients |

UEFI HTTP-Boot targets fetch the iPXE binary from bty-web over HTTP, so no
TFTP is needed end-to-end; the `tftp` sidecar covers clients that bootstrap
over TFTP option 67. DHCP stays with the operator's LAN in both cases.

## To build the bty media yourself

`make build VARIANT=...` under `bty-media/` runs cijoe pipelines
that need:

| Dependency | Used by which variant |
|---|---|
| `live-build` + `debootstrap` + `squashfs-tools` + `xorriso` + `exfatprogs` | `usbboot-pc`, `netboot-pc`, `usbboot-rpi` |
| `mtools` | `usbboot-rpi` (mcopy populates the FAT32 firmware partition) |
| Native arm64 builder (e.g. GitHub's `ubuntu-24.04-arm` runner, or any Pi / Apple Silicon under Linux) | `usbboot-rpi` (qemu-user-static cross-debootstrap works but is 5-10x slower + breaks DKMS) |
| `cijoe` | all variants (orchestration) |
| Passwordless `sudo` | all variants (live-build / loopback mounts / mkfs need it) |

`make media-deps` installs `cijoe` via pipx; the rest are apt
packages (or your distro's equivalent) that the build will check
for at runtime.

## Environment variables

Every env var bty's runtime reads, with the consuming process and the
default. The wizard + web read from the same set, so a single ``ENV``
block (compose / Quadlet / Dockerfile) covers every component.

``bty-web``'s canonical config is a ``bty.toml`` file (pointed at via
``BTY_CONFIG_FILE`` / ``BTY_CONFIG_DIR``); per-key env overrides
follow the ``BTY_<SECTION>_<KEY>`` convention (e.g. ``BTY_SERVER_PORT``,
``BTY_PATHS_STATE_DIR``). See ``walkthrough-server-docker.md`` for
the full schema and the deploy-time emit.

The table below lists the env vars bty-web reads outside the
``bty.toml`` flow plus the legacy ``BTY_IMAGE_ROOT`` knob the ``bty``
wizard still honours for its local-images browser.

| Var | Read by | Default | Purpose |
|---|---|---|---|
| `BTY_IMAGE_ROOT` | `bty` | `/var/lib/bty/images` | TUI local-images browser root (no longer used by `bty-web`; v0.40+ `bty-web` is bytes-less) |
| `BTY_CONFIG_FILE` | `bty-web` | unset | Explicit `bty.toml` path; overrides the default `/etc/bty/bty.toml` + `<state_dir>/bty.toml` search |
| `BTY_CONFIG_DIR` | `bty-web` | unset | Explicit `conf.d/` directory; layered on top of the single-file config |
| `BTY_ADMIN_PASSWORD` | `bty-web` | `bty-lab` | Direct env shortcut for the admin password; otherwise authored as `[admin] password` in `bty.toml`. Constant-time compare; auth is always on |
| `BTY_BOOT_RELEASE_REPO` | `bty-web` | `safl/bty` | GitHub repo to fetch boot artifacts from; consulted directly by `bty.web._releases` |

``bty`` also accepts `--catalog SOURCE` to pre-load a catalog and `--server
X --mac Y` for server-driven dispatch. See `reference.md > CLI` for the
full surface.

## To run the test-pxe end-to-end check

```bash
make test-pxe
```

Spins up a server VM + a client VM sharing an L2 segment and runs
the full PXE chain against pre-built artifacts. Adds:

| Dependency | Used for |
|---|---|
| `qemu-system-x86_64` + KVM | both VMs |
| `cijoe` | orchestrating the test sequence |

Wall clock ~5-10 min per run.
