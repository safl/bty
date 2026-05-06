# bty: Boot & Target Utility

[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![Documentation](https://img.shields.io/badge/docs-safl.dk%2Fbty-blue)](https://safl.dk/bty)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Bare-metal provisioning toolkit. Flashes pre-built ("cooked") system
images onto target disks (locally from a USB stick or remotely over
PXE) and configures them via cloud-init or CIJOE workflows. Designed
for both ad-hoc one-off provisioning and DevOps fleet operation.

bty is one Python package: the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/), with three
console-script entry points:

- `bty`: main CLI (image inspection, target discovery, flashing,
  provisioning).
- `bty-cli`: command-line client for a remote `bty-web` server
  (`bty-cli login`, `bty-cli logout`, future fleet ops).
- `bty-tui`: terminal UI (requires the `tui` extra).
- `bty-web`: HTTP server with browser UI (requires the `web` extra).

Plus a sibling appliance-image builder under `bty-media/` that produces
the bootable USB live image and the server appliance image.

## Install

```bash
pipx install bty-lab            # CLI + bty-cli, zero third-party Python deps
pipx install "bty-lab[tui]"     # adds the bty-tui terminal UI
pipx install "bty-lab[web]"     # adds the bty-web HTTP server
pipx install "bty-lab[all]"     # everything
```

The CLI flow (`bty list disks`, `bty inspect image`, `bty flash --dry-run`)
needs only Python 3.11+ and stdlib; full flashing (`bty flash --yes`)
relies on system binaries (`dd`, `qemu-img`, `zstd`, `lsblk`, etc.) the
operator's distribution is expected to provide.

## Status

This is the working tree of an in-progress rewrite. The original Flask
app and PXE/syslinux configuration have been removed; the new
foundation is being laid out per [`PLAN.md`](PLAN.md).

## Planning and design

- [`PLAN.md`](PLAN.md): roadmap and design intent.
- [`docs/`](docs/): full documentation (Sphinx + MyST).

## Development

`uv` is the project's dependency manager. Install it via pipx if you
don't already have it:

```bash
pipx install uv
```

Then sync the dev environment:

```bash
uv sync --all-extras --group dev
```

Run the test suite, linter, and type-checker:

```bash
uv run pytest
uv run ruff check
uv run mypy src
```

## Documentation

The docs tooling installs as a separate pipx app:

```bash
pipx install ./docs/tooling
```

Then, from inside `docs/`:

```bash
bty-docs-serve              # live-rebuild dev server on :8000
bty-docs-build-html         # one-shot HTML build
bty-docs-build-pdf          # one-shot PDF build (requires LaTeX)
```

## License

[GPL-3.0-only](LICENSE).
