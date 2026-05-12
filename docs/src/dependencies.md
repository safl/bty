# Dependencies

What bty needs at runtime, organised by what you're trying to do.
The `bty-lab` PyPI package itself has no third-party Python
dependencies for the core CLI, and lazy-loads the bigger pieces
(textual for the TUI, fastapi for the web server) only when those
entry points are actually used.

## To install bty itself

The Python package and one of its CLI / TUI / web entry points:

```bash
pipx install bty-lab            # bty CLI only, stdlib-only Python
pipx install "bty-lab[tui]"     # adds bty-tui (Textual)
pipx install "bty-lab[web]"     # adds bty-web (FastAPI, Uvicorn, pamela)
pipx install "bty-lab[all]"     # everything
```

Python 3.11+ is the only hard requirement. PyPI hosts pure-Python
wheels; the `[tui]` / `[web]` extras pull in their own pure-Python
deps with no native build step on install.

## To inspect or flash on a Linux host

`bty inspect` and `bty flash` shell out to a handful of system
binaries that almost every Linux distribution already has:

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

Flashing a real disk requires root (`sudo bty flash` or running
inside the bty live env where root is already there).

## To run bty-tui

Same set of binaries as above (the TUI delegates flashing to the
same `bty.flash` library), plus the `[tui]` extra. The `--catalog
SOURCE` mode (a local TOML path, HTTP URL, or `oras://` reference)
adds nothing host-side -- it's a plain HTTP client; `oras://` uses
stdlib urllib through `bty.oras`.

## To run bty-web

The `[web]` extra (fastapi, uvicorn, pamela, jinja2 are pulled in by
pip) plus:

- `libpam0g` + `libpam-modules` if you want PAM-based `/ui/login`
  (this is the default; the appliance and the Docker container both
  ship it).
- A user account on the host whose password you'll log in with
  (the appliance + container ship `bty / bty` and tell you to
  rotate it).
- `qemu-img` (above) so the server can inspect uploaded images.

## To use the bty-server appliance for PXE

The shipped `server-x86` and `server-rpi` appliance images bundle
everything needed; this list is for reference if you're rebuilding
your own.

| Binary / package | Used for |
|---|---|
| `dnsmasq` | proxy-DHCP + TFTP for the PXE chain |
| `ipxe` | the iPXE BIOS / UEFI ROMs (`undionly.kpxe`, `ipxe.efi`) chain-loaded by booting clients |
| `systemd-networkd` | NIC management on the appliance |
| `cloud-init` | first-boot user / password / network setup |
| `cloud-utils` | `growpart` for `bty-grow-rootfs.service` |
| `plymouth` + `plymouth-themes` | bty-themed boot splash on tty1 (USB live env only -- not on the headless server appliance) |

The appliance is the **only** delivery shape that exercises the PXE
stack. The Docker container (`ghcr.io/safl/bty-web`) deliberately
omits dnsmasq + iPXE because Docker bridge networking can't relay
the L2 broadcasts proxy-DHCP needs.

## To build the appliance images yourself

`make build VARIANT=...` under `bty-media/` runs cijoe pipelines
that need:

| Dependency | Used by which variant |
|---|---|
| `live-build` + `debootstrap` + `squashfs-tools` + `xorriso` + `exfatprogs` | `usb-x86`, `netboot-x86` |
| `qemu-system-x86_64` + KVM + `cloud-image-utils` | `server-x86` (cloud-init bake in QEMU) |
| `qemu-user-static` + `binfmt_misc` + `losetup` + `xz-utils` | `server-rpi` (mount + chroot Raspberry Pi OS Lite, which ships as `.img.xz`) |
| `cijoe` | all variants (orchestration) |
| Passwordless `sudo` | all variants (live-build / loopback mounts / mkfs need it) |

`make media-deps` installs `cijoe` via pipx; the rest are apt
packages (or your distro's equivalent) that the build will check
for at runtime.

## Environment variables

Quick reference of every env var bty's runtime reads, with which
process consumes it and the default. The CLI / TUI / web all read
from the same set so a single ``/etc/default/bty-web`` (appliance)
or ``ENV`` block (Dockerfile) covers every component.

| Var | Read by | Default | Purpose |
|---|---|---|---|
| `BTY_IMAGE_ROOT` | `bty`, `bty-tui`, `bty-web` | `/var/lib/bty/images` | Image catalog directory |
| `BTY_STATE_DIR` | `bty-web` | `/var/lib/bty` | Where `state.db`, `session-secret`, etc. live |
| `BTY_BOOT_DIR` | `bty-web` | `${BTY_STATE_DIR}/boot` | Kernel / initrd / squashfs (PXE boot artifacts) |
| `BTY_WEB_HOST` | `bty-web` | `0.0.0.0` | Listen address |
| `BTY_WEB_PORT` | `bty-web` | `8080` | Listen port |
| `BTY_SESSION_SECRET` | `bty-web` | (generated, persisted under `BTY_STATE_DIR`) | Cookie key override; useful for multi-instance |
| `BTY_BOOT_RELEASE_REPO` | `bty-web` | `safl/bty` | GitHub releases repo to fetch boot artifacts from |
| `BTY_CATALOG_FILE` | `bty-web`, `bty catalog` | `${BTY_STATE_DIR}/catalog.toml` | Manifest path (TOML; see walkthrough-catalog.md) |
| `BTY_CATALOG_CACHE_DIR` | `bty-web`, `bty catalog` | `${BTY_STATE_DIR}/cache` | Content-addressed blob cache |
| `BTY_CATALOG_MAX_PARALLEL` | `bty-web` | `2` | Concurrent catalog downloads |
| `BTY_HASH_MAX_PARALLEL` | `bty-web` | `1` | Concurrent SHA-256 hashes (low: Pi/NUC-friendly) |
| `BTY_MAX_UPLOAD_BYTES` | `bty-web` | `200 GiB` | Hard cap on `PUT /images/{name}` body size; rejected uploads land an `image.upload_failed` audit row |
| `BTY_TRUSTED_PROXY` | `bty-web` | unset | When set (any truthy), read client IP from `X-Forwarded-For`; only enable behind a reverse proxy that strips inbound X-F-F |
| `BTY_QUIET` | container entrypoint | unset | Suppress the start-up banner with default credentials |

`bty tui` also accepts `--image-root /path` and `--catalog SOURCE`
flags directly. On the CLI, `bty images --image-root /path`
overrides for that command; `bty inspect PATH` and
`bty flash IMAGE TARGET` take the image as a direct positional
argument (path for inspect; path, `http(s)://...`, `oras://...`,
or `.bri` descriptor path for flash's IMAGE) and don't read the
image root.

## To run the test-pxe end-to-end check

```bash
make test-pxe
```

Spins up a server VM + a client VM sharing an L2 segment and runs
the full PXE chain against pre-built artefacts. Adds:

| Dependency | Used for |
|---|---|
| `qemu-system-x86_64` + KVM | both VMs |
| `cijoe` | orchestrating the test sequence |

Wall clock ~5-10 min per run.
