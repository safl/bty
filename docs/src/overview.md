# Overview

bty is shaped around one job: take a pre-built system image and put it on
a target disk, fast and repeatably. The driving use case is CI
infrastructure where reflashing is a routine event
at three different cadences:

- **Per-job** - wipe and reflash between CI runs so each job starts from a
  bit-identical baseline.
- **On new image** - promote a freshly-built image and roll it out across
  the relevant fleet members.
- **On failure** - a deployed instance has gone bad; reflash recovers it
  without operator hand-holding.

Every design choice in bty exists to make those three cadences cheap,
fast, and boring.

## Three deployment shapes

bty serves both ends of the operator spectrum, with a middle shape
that bridges the two:

- **Self-contained USB.** USB live image carrying the `bty` runtime
  and bundled images on its own exFAT partition. Plug in, boot, flash,
  walk away. No server to set up. Best for the field-tech / one-off
  reflash.
- **USB + portable catalog.** Same USB live image, plus
  `bty tui --catalog <SOURCE>` pointed at a TOML catalog hosted
  anywhere -- a local file, an HTTP URL, an `oras://` reference, or
  a bty-web instance's `/catalog.toml` endpoint. Flash still happens
  locally on the operator's hardware; only the catalog is centralised.
  Best for a small team sharing pre-built images without setting up
  a full PXE server.
- **PXE-driven (no operator).** Full `bty-server` appliance running
  `bty-web` and the iPXE/TFTP/HTTP services. Fleet members are
  registered by MAC address; reflashes happen on schedule, on demand,
  or on failure without operator involvement at the target. Best for
  CI fleets and lab automation.

All three wrap the same `bty` runtime - same image catalog format,
same target-disk operations. The difference is whether the catalog
ships on the stick, lives on a server clients pull from, or drives
the whole flash unattended.

## OS scope

bty is intentionally OS-agnostic. The image is a sealed pre-built
artifact; bty puts it on disk and hands off to a first-boot mechanism if
any. Targeted: Linux of any flavor (including vendor-pinned stacks like
Ubuntu+NVIDIA), FreeBSD, and Windows. Not in scope: macOS - Apple does
not provide practical automation hooks at the disk-image level.

See the [related work](related.md) chapter for how bty positions against
NixOS, MAAS, FOG, iVentoy, and others.

## Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with three console-script
entry points, plus a sibling appliance-image builder:

- `bty` - main CLI for image inspection, target discovery, flashing.
- `bty-tui` - terminal UI for interactive use from a live environment.
  With ``--catalog SOURCE`` (a local TOML path, HTTP URL, or
  ``oras://`` reference) it overlays a portable catalog on top of
  the local image-root scan.
- `bty-web` - HTTP server + browser UI for fleet image flashing.
- `bty-media/` - sibling directory (not a Python package); a
  cijoe-driven Debian appliance-image builder that produces the USB live
  and server images.

See the [components](components.md) chapter for details on each.
