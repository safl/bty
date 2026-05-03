# bty-media

Builds the two `bty` appliance images:

- **USB live image** — bootable USB carrying the `bty` runtime and a
  bundled image set, for the direct-flash workflow.
- **Server image** — installable disk image hosting `bty-web` and the
  network-flash infrastructure, for the DevOps workflow.

This directory is not a Python package. It is a sibling to the `bty`
Python project and follows the `jkab` (jellyfin-kiosk-appliance-builder)
pattern: cijoe-driven, Makefile-orchestrated, raw-image output.

The build wiring lands in milestone 2 (USB live) and milestone 13 (server
image). Until then this is a placeholder layout.

## Layout

- `Makefile` — entry points (`make build`, `make test`).
- `configs/` — TOML build configs, one per artifact variant.
- `rootfs/` — files staged into the built image.
- `scripts/` — helper scripts used by the build.
- `tasks/` — cijoe workflow files.
- `tests/` — image-validation tests.
