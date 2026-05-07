# bty-media

Source content for the bty appliance images. Three variants:

- **USB live image** (`VARIANT=usb`) - bootable USB carrying the bty
  runtime and a bundled image set, for the direct-flash workflow.
  Lands in milestone 2.
- **Server image** (`VARIANT=server`) - installable disk image for the
  bty provisioning server (`bty-web` + PXE boot stack).
- **Network-flash live env** (`VARIANT=live`) - kernel + initrd +
  squashfs that PXE clients chain into. Carries the bty CLI plus a
  `bty-flash-on-boot.service` oneshot that reads `bty.*` parameters
  from `/proc/cmdline`, fetches the assigned image, runs `bty flash`,
  and reboots.

This directory holds the **content** baked into the images: cloud-init
base templates, rootfs trees that live-build / cloud-init fold in,
and the live-build config tree. The cijoe **orchestration** (configs,
tasks, scripts) that consumes this content lives at `cijoe/` at the
repo root.

Operators drive everything via the top-level Makefile:
`make build VARIANT=usb|server|live`.

## Layout

- `auxiliary/cloudinit-base-<variant>.user` - per-variant cloud-init
  base template (usb / server only).
- `auxiliary/cloudinit-metadata.meta` - shared cloud-init metadata.
- `rootfs/common/` - files baked into every disk-image variant.
- `rootfs/<variant>/` - files baked into a single disk-image variant.
  Each file becomes a cloud-init `write_files` entry whose `path`
  mirrors the file's path under the variant subdirectory. Binary
  files (anything that is not valid UTF-8) are emitted with
  `encoding: b64`.
- `live-build/` - live-build config tree consumed by the live
  variant's pipeline (`auto/config`, `config/package-lists/`,
  `config/includes.chroot/`, etc.).

## Pipeline

From the repo root:

```
make build VARIANT=usb|server|live
```

runs `cijoe tasks/build.yaml --monitor -c configs/$(VARIANT).toml`,
which executes four steps:

1. **`bty_wheel_stage`** (server variant only) - builds a `bty-lab`
   wheel from the parent repo via `uv build` and stages it under
   `rootfs/server/opt/bty/`. The wheel is base64-inlined into
   cloud-init by the next step and `pip install`ed into a system
   venv at `/opt/bty/venv` during the bake.
2. **`gen_userdata`** - assembles the cloud-init userdata file by
   inlining files under `rootfs/common/` and `rootfs/<variant>/` as
   `write_files` entries on top of `auxiliary/cloudinit-base-<variant>.user`.
3. **`diskimage_build`** - downloads the Debian 13 cloud image,
   resizes the qcow2 boot disk, builds the cloud-init seed.iso, and
   boots QEMU. cloud-init provisions the system and powers off; the
   baked qcow2 is compacted via `qemu-img convert -c`.
4. **`img_zst_publish`** - converts the qcow2 to raw and
   zstd-compresses the result into a `dd`-able `.img.zst`, alongside a
   sha256sum.

## Build prerequisites

Disk-image variants (usb / server):
- `qemu-system-x86_64` and `qemu-img` (Debian package
  `qemu-system-x86` and `qemu-utils`)
- `mkisofs` (Debian package `genisoimage`)
- `zstd`
- KVM acceleration (configured in `configs/<variant>.toml`); without
  it the cloud-init bake step is impractically slow
- `uv` (server variant only - used by `bty_wheel_stage` to build the
  bty-lab wheel; install with `pipx install uv` if needed)

Live variant:
- `live-build` (`sudo apt install live-build`)
- `debootstrap`, `squashfs-tools`, `xorriso` (pulled in by
  `live-build`'s recommends, or install explicitly)
- Passwordless `sudo` - live-build's chroot operations are
  privileged; CI runners have NOPASSWD by default

All variants:
- `cijoe` (install via `make media-deps`, which runs `pipx install cijoe`)

## Output

Disk-image variants (usb / server):
- `~/system_imaging/disk/bty-<variant>-x86_64.qcow2` - baked, compacted
  qcow2 (intermediate; useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-<variant>-x86_64.img.zst` - final
  artifact. Decompress with `zstd -d` and pipe to `dd` (or feed to a
  USB-imaging tool / VM disk that accepts `.img.zst`).

Live variant:
- `~/system_imaging/disk/bty-live-x86_64.vmlinuz` - kernel
- `~/system_imaging/disk/bty-live-x86_64.initrd` - initramfs
- `~/system_imaging/disk/bty-live-x86_64.squashfs` - overlay rootfs
- `~/system_imaging/disk/bty-live-x86_64.sha256` - manifest

## Status

All three variants are shipping. Each tagged release publishes the
finished artifacts to the [GitHub releases page](https://github.com/safl/bty/releases),
so most operators never run this build pipeline themselves - it
exists for contributors who want to modify the image.

- **USB variant.** The cooked `.img.zst` boots into a Debian live
  environment with `overlayroot` (RAM-tmpfs overlay over a read-only
  rootfs), the `bty` CLI + TUI installed into `/opt/bty/venv`, and an
  exFAT `BTY_IMAGES` partition for cooked images. End-to-end use
  case in [Walkthrough: USB](../docs/src/walkthrough-usb.md).
- **Live variant.** Kernel + initrd + squashfs trio used by PXE
  clients. The chroot ships `bty-flash-on-boot.service` (oneshot,
  after `network-online.target`); it reads `bty.server=`, `bty.mac=`,
  `bty.image_url=`, and `bty.provisioning=` from `/proc/cmdline`,
  downloads the image, runs `bty flash --yes`, signals
  `POST ${server}/pxe/${mac}/done`, and reboots. Without those
  cmdline keys it exits 0 and drops to a console.

  The end-to-end PXE chain (server hands a per-MAC iPXE plan, client
  loads the live trio, flashes a target disk, signals done) is
  exercised by `make test-pxe` and runs in CI on every push.
- **Server variant.** Bootable Debian cloud-image hosting `bty-web`
  with single-tenant PAM auth: the `bty` service user is the sole
  principal, default credential `bty / bty`, rotated on the appliance
  with `sudo passwd bty`. An `odus` admin user is also baked in (with
  passwordless sudo) for SSH-side maintenance. The server image's
  `bty-web-init.service` oneshot creates `BTY_STATE_DIR`, initialises
  the SQLite schema, and rewrites `/etc/issue` to point operators at
  `http://<ip>:8080/ui`. The dnsmasq PXE block is shipped commented
  out and activated from the browser UI's Settings page (no shell
  edits required).

  ### Operator first-boot

  1. Write the `.img.zst` to the server's disk (or attach as a VM
     disk). Pre-built artifacts at
     <https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.zst>.
  2. Boot. The login prompt's banner shows the browser UI URL.
  3. Log in to `/ui/login` with `bty / bty` (or rotate first via
     `sudo passwd bty` on the appliance).
  4. From `/ui/settings`, pick the interface + subnet for PXE and
     click Activate. dnsmasq restarts; PXE clients on that segment
     will now chain through bty-web.
