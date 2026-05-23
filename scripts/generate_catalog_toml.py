#!/usr/bin/env python3
"""Generate the default ``catalog.toml`` shipped as a bty release asset.

Reads the checked-in template ``starter_catalog.toml.in`` (next to
this script), substitutes ``{version}`` with the current bty-lab
version from ``pyproject.toml``, and writes the result to the output
path (default: ``catalog.toml`` in the repo root).

``bty --catalog`` consumers point at::

    https://github.com/safl/bty/releases/latest/download/catalog.toml

The release workflow runs this script and uploads the result
alongside the wheels and appliance images.

The catalog file is a release artifact, NOT something baked onto the
USB stick -- the USB stick's BTY_IMAGES partition is plain operator-
managed local image files (.qcow2 / .img.gz / .img / .iso / .iso.gz).
The wizard merges local files + an optional ``--catalog`` overlay;
the starter catalog is that overlay (the TUI's SELECT_CATALOG offers
it as ``[d] default``).

Rolling tags stay rolling: ``oras://`` refs are *not* pre-resolved
to digests at generate-time. ``bty.oras.resolve_ref`` handles the
manifest fetch + layer-digest verification at flash time.

Run directly:

    python scripts/generate_catalog_toml.py [OUTPUT_PATH]

Defaults to writing ``catalog.toml`` in the repo root.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = Path(__file__).resolve().parent / "starter_catalog.toml.in"


def _read_bty_version() -> str:
    """Read the bty-lab version from ``pyproject.toml`` at repo root."""
    pyproject = REPO_ROOT / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "catalog.toml"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    bty_version = _read_bty_version()
    rendered = template.format(version=bty_version)
    # Parse-back round-trip: catches a malformed template / bad
    # substitution before publishing. tomllib.loads raises on invalid
    # TOML; ``bty.catalog.Catalog``'s schema check happens at the
    # consistency-test layer, not here, so the release workflow
    # doesn't drag in optional package extras.
    tomllib.loads(rendered)
    out_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {out_path} ({len(rendered)} bytes, bty v{bty_version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
