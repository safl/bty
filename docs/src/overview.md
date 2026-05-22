# Overview

bty does one job: take a pre-built system image and put it on a target
disk, fast and repeatably. The driving use case is CI infrastructure
where reflashing happens at three cadences:

- **Per-job** - wipe and reflash between CI runs so each job starts from a
  bit-identical baseline.
- **On new image** - promote a freshly-built image across the relevant
  fleet members.
- **On failure** - reflash recovers a deployed instance that has gone bad,
  without operator hand-holding.

Every design choice exists to make those three cadences cheap, fast, and
boring.

## Three deployment shapes

bty serves both ends of the operator spectrum, with a middle shape that
bridges the two:

- **Self-contained USB.** USB live image carrying the `bty` runtime and
  bundled images on its own exFAT partition. Plug in, boot, flash, walk
  away. No server. Best for the field-tech / one-off reflash.
- **USB + portable catalog.** Same USB live image, plus
  `bty --catalog <SOURCE>` pointed at a TOML catalog hosted anywhere: a
  local file, an HTTP URL, an `oras://` reference, or a bty-web instance's
  `/catalog.toml` endpoint. Flash still happens locally; only the catalog
  is centralised. Best for a small team sharing pre-built images without a
  full PXE server.
- **PXE-driven (no operator).** Full `bty-server` appliance running
  `bty-web` and the iPXE/TFTP/HTTP services. Fleet members are registered
  by MAC address; reflashes happen on schedule, on demand, or on failure
  with no operator at the target. Best for CI fleets and lab automation.

All three wrap the same `bty` runtime: same image catalog format, same
target-disk operations. The difference is whether the catalog ships on the
stick, lives on a server clients pull from, or drives the whole flash
unattended.

## OS scope

bty is intentionally OS-agnostic. The image is a sealed pre-built
artifact; bty puts it on disk and hands off to a first-boot mechanism if
any. Targeted: Linux of any flavor (including vendor-pinned stacks like
Ubuntu+NVIDIA), FreeBSD, and Windows. Out of scope: macOS - Apple provides
no practical automation hooks at the disk-image level.

See [related work](related.md) for how bty positions against NixOS, MAAS,
FOG, iVentoy, and others.

## Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with two console-script
entry points, plus a sibling appliance-image builder:

- `bty` - the operator-facing wizard (Rich-based) + library.
  ``--catalog SOURCE`` (a local TOML path, HTTP URL, or ``oras://``
  reference) overlays a portable catalog on the local image-root scan.
  ``--server X --mac Y`` switches to server-driven mode (GETs
  ``<X>/pxe/<Y>/plan`` and dispatches on the JSON response).
- `bty-web` - HTTP server + browser UI for fleet image flashing.
- `bty-media/` - sibling directory (not a Python package); a cijoe-driven
  Debian appliance-image builder that produces the USB live and server
  images.

See the [components](components.md) chapter for details on each.
