# bty-media

Builds the bty appliance images. Three variants:

- **USB live image** (`VARIANT=usb`) — bootable USB carrying the bty
  runtime and a bundled image set, for the direct-flash workflow.
  Lands in milestone 2.
- **Server image** (`VARIANT=server`) — installable disk image for the
  bty provisioning server (`bty-web` + PXE boot stack).
- **Network-flash live env** (`VARIANT=live`) — kernel + initrd +
  squashfs that PXE clients chain into. Carries the bty CLI plus a
  `bty-flash-on-boot.service` oneshot that reads `bty.*` parameters
  from `/proc/cmdline`, fetches the assigned image, runs `bty flash`,
  and reboots. The iPXE chain that supplies the cmdline params is
  built in Phase D-3 (server side).

This directory is not a Python package. It mirrors the `jkab`
(jellyfin-kiosk-appliance-builder) pattern: cijoe-driven, Debian
cloud-image based, Makefile-orchestrated, with `configs/` per variant
and `rootfs/` files inlined into the image as cloud-init `write_files`.

## Layout

- `Makefile` — entry points (`make deps`, `make build`, `make clean`).
  Dispatches to `tasks/build.yaml` (usb / server) or `tasks/live.yaml`
  (live) based on `VARIANT`.
- `configs/<variant>.toml` — cijoe config per variant
  (`usb.toml`, `server.toml`, `live.toml`).
- `tasks/build.yaml` — usb / server pipeline (cloud-init bake in QEMU,
  emits `.img.zst`).
- `tasks/live.yaml` — live pipeline (live-build → kernel + initrd +
  squashfs).
- `scripts/` — Python steps invoked by either pipeline.
- `auxiliary/cloudinit-base-<variant>.user` — per-variant cloud-init
  base template (usb / server only).
- `auxiliary/cloudinit-metadata.meta` — shared cloud-init metadata.
- `rootfs/common/` — files baked into every disk-image variant.
- `rootfs/<variant>/` — files baked into a single disk-image variant.
  Each file becomes a cloud-init `write_files` entry whose `path`
  mirrors the file's path under the variant subdirectory. Binary
  files (anything that is not valid UTF-8) are emitted with
  `encoding: b64`.
- `live-build/` — live-build config tree consumed by the live
  variant's pipeline (`auto/config`, `config/package-lists/`,
  `config/includes.chroot/`, etc.).

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

Disk-image variants (usb / server):
- `qemu-system-x86_64` and `qemu-img` (Debian package
  `qemu-system-x86` and `qemu-utils`)
- `mkisofs` (Debian package `genisoimage`)
- `zstd`
- KVM acceleration (configured in `configs/<variant>.toml`); without
  it the cloud-init bake step is impractically slow
- `uv` (server variant only — used by `bty_wheel_stage` to build the
  bty-lab wheel; install with `pipx install uv` if needed)

Live variant:
- `live-build` (`sudo apt install live-build`)
- `debootstrap`, `squashfs-tools`, `xorriso` (pulled in by
  `live-build`'s recommends, or install explicitly)
- Passwordless `sudo` — live-build's chroot operations are
  privileged; CI runners have NOPASSWD by default

All variants:
- `cijoe` (install via `make deps`, which runs `pipx install cijoe`)

## Output

Disk-image variants (usb / server):
- `~/system_imaging/disk/bty-<variant>-x86_64.qcow2` — baked, compacted
  qcow2 (intermediate; useful for QEMU smoke tests).
- `~/system_imaging/disk/bty-<variant>-x86_64.img.zst` — final
  artifact. Decompress with `zstd -d` and pipe to `dd` (or feed to a
  USB-imaging tool / VM disk that accepts `.img.zst`).

Live variant:
- `~/system_imaging/disk/bty-live-x86_64.vmlinuz` — kernel
- `~/system_imaging/disk/bty-live-x86_64.initrd` — initramfs
- `~/system_imaging/disk/bty-live-x86_64.squashfs` — overlay rootfs
- `~/system_imaging/disk/bty-live-x86_64.sha256` — manifest

## Status

- USB variant: milestone 2 scaffold. Pipeline materialised, the cooked
  image carries `overlayroot` and a placeholder banner. The actual
  `bty` runtime gets baked into the image starting in milestone 6.
- Live variant: milestone 14 phase D-2. The chroot carries the bty
  CLI installed from a locally-built wheel into `/opt/bty/venv`,
  plus `bty-flash-on-boot.service` (oneshot, after
  `network-online.target`). The service reads `bty.server=`,
  `bty.mac=`, `bty.image_url=`, and `bty.provisioning=` from
  `/proc/cmdline`; with all three required keys present it
  downloads the image, runs `bty flash --yes`, signals `POST
  ${server}/pxe/${mac}/done` (best-effort), and reboots. Without
  cmdline keys it exits 0 and the env drops to its console.

  Smoke-test recipe (no PXE / iPXE involved): build the artifacts,
  then in two QEMU windows -

  ```
  qemu-system-x86_64 -enable-kvm -m 2G -nographic -append \
    "boot=live components quiet bty.server=http://10.0.2.2:8080 \
     bty.mac=aa-bb-cc-dd-ee-ff bty.image_url=http://10.0.2.2:8000/test.img \
     console=ttyS0" \
    -kernel ~/system_imaging/disk/bty-live-x86_64.vmlinuz \
    -initrd ~/system_imaging/disk/bty-live-x86_64.initrd \
    -netdev user,id=n0 -device virtio-net,netdev=n0 \
    -drive file=blank.qcow2,if=virtio
  ```

  Phase D-3 will replace this manual recipe with iPXE chain
  templates that the server hands to PXE clients.
- Server variant: milestone 13 phase B + milestone 14 phase C.
  Bootable Debian cloud-image hosting `bty-web` from the
  locally-built `bty-lab` wheel, plus the PXE boot-stack scaffold
  (dnsmasq + iPXE binaries). A `bty-web-init.service` oneshot runs
  on first boot to generate a random bearer token, write
  `/etc/default/bty-web`, create the state directory, and rewrite
  `/etc/issue` so the operator sees the URL and token on the
  bare-metal/VM console at the login prompt.

  ### Operator first-boot

  1. Write the `.img.zst` to the server's disk (or attach as a VM
     disk).
  2. Boot. The login prompt's banner shows
     `Browser UI: http://<ip>:8080/ui` and `Bearer token: <value>`.
  3. Open the URL in a browser, paste the token, you're in.
  4. Rotate the token by removing `/etc/default/bty-web` and
     rebooting (a future first-boot wizard will replace this flow).

  ### PXE boot stack (Phase C)

  TFTP is up by default and serves `undionly.kpxe` (BIOS) and
  `ipxe.efi` (UEFI) from `/var/lib/tftpboot/`. The proxy-DHCP +
  chain directives in `/etc/dnsmasq.d/bty-pxe.conf` are **commented
  out by default** to avoid disrupting an existing DHCP server on
  the network. Activate them by uncommenting the block and
  substituting the operator's PXE subnet, then
  `systemctl restart dnsmasq`. Phase E (first-boot wizard) will
  surface this in the browser UI.
