<p align="center">
  <img src="docs/src/_static/bty-mascot.png" alt="bty mascot - a blue bat holding a PXE handshake card and a disk labelled .qcow2 / .img / .raw" width="240">
</p>

# bty - flash images onto target disks, locally or over PXE

[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![Documentation](https://img.shields.io/badge/docs-safl.dk%2Fbty-blue)](https://safl.dk/bty)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Image-flash provisioning toolkit for bare-metal and virtual targets.
Writes pre-built ("cooked") system images onto target disks - locally
from a USB live stick or remotely over PXE - and configures the
deployed system on first boot via cloud-init or CIJOE workflows.

bty is one Python package: the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/), with three
console-script entry points:

- `bty`: main CLI (image inspection, target discovery, flashing,
  provisioning).
- `bty-tui`: terminal UI (requires the `tui` extra). With
  `--server URL` it doubles as a remote-flash client against a
  running `bty-web`.
- `bty-web`: HTTP server with browser UI (requires the `web` extra).

Plus a sibling appliance-image builder under `bty-media/` that produces
the bootable USB live image and the server appliance image.

## Install

```bash
pipx install bty-lab            # CLI, zero third-party Python deps
pipx install "bty-lab[tui]"     # adds the bty-tui terminal UI
pipx install "bty-lab[web]"     # adds the bty-web HTTP server
pipx install "bty-lab[all]"     # everything
```

The CLI flow (`bty list disks`, `bty inspect image`, `bty flash --dry-run`)
needs only Python 3.11+ and stdlib; full flashing (`bty flash --yes`)
relies on system binaries (`dd`, `qemu-img`, `zstd`, `lsblk`, etc.) the
operator's distribution is expected to provide.

## Status

Pre-1.0 but actively shipping. Wheels and appliance images publish
to PyPI + [GitHub Releases](https://github.com/safl/bty/releases) on
every tag, and the server + client + PXE-chain end-to-end flow runs
in CI on every push. The CLI surface (`bty list`, `bty inspect`,
`bty flash`) and the bty-web HTTP/iPXE/PAM-auth surfaces are stable
enough to use in homelab / CI fleets. Wire formats and CLI flags
may still shift between minor versions until 1.0; the schema_version
field on `--json` output and the `Machine` wire type are the things
to watch. See [`PLAN.md`](PLAN.md) for the milestone-by-milestone
roadmap.

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
