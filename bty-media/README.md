# bty-media

Builds the bty appliance images. Two variants:

- **USB live image** (`VARIANT=usb`) — bootable USB carrying the bty
  runtime and a bundled image set, for the direct-flash workflow.
  Lands in milestone 2.
- **Server image** (`VARIANT=server`) — installable disk image for the
  bty provisioning server. Phase A (this milestone) is the bootable
  Debian scaffold; bty-web bakes in next.

This directory is not a Python package. It mirrors the `jkab`
(jellyfin-kiosk-appliance-builder) pattern: cijoe-driven, Debian
cloud-image based, Makefile-orchestrated, with `configs/` per variant
and `rootfs/` files inlined into the image as cloud-init `write_files`.

## Layout

- `Makefile` — entry points (`make deps`, `make build`, `make clean`).
- `configs/<variant>.toml` — cijoe config per variant
  (`usb.toml`, `server.toml`).
- `tasks/build.yaml` — variant-agnostic cijoe workflow. Picks the
  variant up from `[bty].variant` in the chosen config.
- `scripts/` — Python steps invoked by the cijoe workflow.
- `auxiliary/cloudinit-base-<variant>.user` — per-variant cloud-init
  base template (hostname/timezone substitutions, packages, runcmd).
- `auxiliary/cloudinit-metadata.meta` — shared cloud-init metadata.
- `rootfs/common/` — files baked into every variant.
- `rootfs/<variant>/` — files baked into a single variant. Each file
  becomes a cloud-init `write_files` entry whose `path` mirrors the
  file's path under the variant subdirectory. Binary files (anything
  that is not valid UTF-8) are emitted with `encoding: b64`.

## Pipeline

```
make build [VARIANT=usb|server]
```

runs `cijoe tasks/build.yaml --monitor -c configs/$(VARIANT).toml`,
which executes four steps:

1. **`bty_wheel_stage`** (server variant only) — builds a `bty-lab`
   wheel from the parent repo via `uv build` and stages it under
   `rootfs/server/opt/bty/`. The wheel is base64-inlined into
   cloud-init by the next step and `pip install`ed into a system
   venv at `/opt/bty/venv` during the bake.
2. **`gen_userdata`** — assembles the cloud-init userdata file by
   inlining files under `rootfs/common/` and `rootfs/<variant>/` as
   `write_files` entries on top of `auxiliary/cloudinit-base-<variant>.user`.
3. **`diskimage_build`** — downloads the Debian 13 cloud image,
   resizes the qcow2 boot disk, builds the cloud-init seed.iso, and
   boots QEMU. cloud-init provisions the system and powers off; the
   baked qcow2 is compacted via `qemu-img convert -c`.
4. **`img_zst_publish`** — converts the qcow2 to raw and
   zstd-compresses the result into a `dd`-able `.img.zst`, alongside a
   sha256sum.

## Build prerequisites

- `qemu-system-x86_64` and `qemu-img` (Debian package
  `qemu-system-x86` and `qemu-utils`)
- `mkisofs` (Debian package `genisoimage`)
- `zstd`
- `cijoe` (install via `make deps`, which runs `pipx install cijoe`)
- `uv` (server variant only — used by `bty_wheel_stage` to build
  the bty-lab wheel; install with `pipx install uv` if needed)
- KVM acceleration (configured in `configs/<variant>.toml`); without
  it the cloud-init bake step is impractically slow

## Output

- `~/system_imaging/disk/bty-<variant>-x86_64.qcow2` — baked, compacted
  qcow2 (intermediate; useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-<variant>-x86_64.img.zst` — final
  artifact. Decompress with `zstd -d` and pipe to `dd` (or feed to a
  USB-imaging tool / VM disk that accepts `.img.zst`).

## Status

- USB variant: milestone 2 scaffold. Pipeline materialised, the cooked
  image carries `overlayroot` and a placeholder banner. The actual
  `bty` runtime gets baked into the image starting in milestone 6.
- Server variant: milestone 13 phase B. Bootable Debian cloud-image
  hosting `bty-web` from the locally-built `bty-lab` wheel. A
  `bty-web-init.service` oneshot runs on first boot to generate a
  random bearer token, write `/etc/default/bty-web`, create the state
  directory, and rewrite `/etc/issue` so the operator sees the URL
  and token on the bare-metal/VM console at the login prompt.

  ### Operator first-boot

  1. Write the `.img.zst` to the server's disk (or attach as a VM
     disk).
  2. Boot. The login prompt's banner shows
     `Browser UI: http://<ip>:8080/ui` and `Bearer token: <value>`.
  3. Open the URL in a browser, paste the token, you're in.
  4. Rotate the token by removing `/etc/default/bty-web` and
     rebooting (a future first-boot wizard will replace this flow).
