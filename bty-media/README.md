# bty-media

Builds the bty appliance images. Two artifacts are planned (only the
first lands in milestone 2):

- **USB live image** — bootable USB carrying the bty runtime and a
  bundled image set, for the direct-flash workflow. Variant: `usb`.
- **Server image** — installable disk image for the bty provisioning
  server. Variant: `server`. Lands in milestone 13.

This directory is not a Python package. It mirrors the `jkab`
(jellyfin-kiosk-appliance-builder) pattern: cijoe-driven, Debian
cloud-image based, Makefile-orchestrated, with `configs/` per variant
and `rootfs/` files inlined into the image as cloud-init `write_files`.

## Layout

- `Makefile` — entry points (`make deps`, `make build`, `make clean`).
- `configs/` — TOML config per variant (currently `usb.toml`).
- `tasks/` — cijoe workflow files (`build.yaml`).
- `scripts/` — Python steps invoked by the cijoe workflow.
- `auxiliary/` — cloud-init base userdata + metadata.
- `rootfs/` — files staged into the built image (overlayroot config,
  `/etc/issue` banner, getty autologin).

## Pipeline

`make build` runs `cijoe tasks/build.yaml --monitor -c configs/usb.toml`,
which executes three steps:

1. **`gen_userdata`** — assembles the cloud-init userdata file by
   inlining every file under `rootfs/` as a `write_files` entry on top
   of `auxiliary/cloudinit-base.user`.
2. **`diskimage_build`** — downloads the Debian 13 cloud image,
   resizes the qcow2 boot disk, builds the cloud-init seed.iso, and
   boots QEMU. cloud-init provisions the system and powers off; the
   baked qcow2 is compacted via `qemu-img convert -c`.
3. **`img_zst_publish`** — converts the qcow2 to raw and
   zstd-compresses the result into a `dd`-able `.img.zst`, alongside a
   sha256sum.

## Build prerequisites

- `qemu-system-x86_64` and `qemu-img` (Debian package
  `qemu-system-x86` and `qemu-utils`)
- `mkisofs` (Debian package `genisoimage`)
- `zstd`
- `cijoe` (install via `make deps`, which runs `pipx install cijoe`)
- KVM acceleration (configured in `configs/usb.toml`); without it the
  cloud-init bake step is impractically slow

## Output

- `~/system_imaging/disk/bty-<variant>-x86_64.qcow2` — baked, compacted
  qcow2 (intermediate; useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-<variant>-x86_64.img.zst` — final
  artifact. Decompress with `zstd -d` and pipe to `dd` (or feed to a
  USB-imaging tool that accepts `.img.zst`).

## Status

Milestone 2 scaffold: pipeline materialised, the cooked image carries
`overlayroot` and a placeholder banner. The actual `bty` runtime gets
baked into the image starting in milestone 6 (when `bty flash` is
real).
