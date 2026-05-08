"""Image catalog discovery and inspection.

Recognises the supported on-disk image formats (``.qcow2``, ``.img``,
``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``), lists them
under a configured image root, and extracts detail metadata for
individual images via the appropriate tool (``qemu-img info`` for
qcow2, ``zstd -l`` / ``xz -l`` / ``gzip -l`` for the corresponding
compressed raws; bzip2 has no listing tool so .img.bz2 has no
detail block).

Format-choice rationale: bty-shipped images all use **gzip** for
universal flasher / OS / tooling support. The flash code accepts
**any** of ``.img``, ``.img.zst``, ``.img.xz``, ``.img.gz``,
``.img.bz2`` for operator-supplied images so format choice is
not forced on operators with their own pipelines.

- The **USB stick image** ships as ``.iso.gz``. Operators write
  it host-side via Etcher / Rufus / Raspberry Pi Imager, which
  decompress .gz natively (xz tripped Etcher's bundled
  decompressor regardless of how the file was shaped; gzip has
  no equivalent quirk). Stick prep is a one-shot, host-side cost.
- The **server appliance images** ship as ``.img.gz``
  (``bty-server-x86_64.img.gz``,
  ``bty-server-rpi-arm64.img.gz``). The earlier rationale that
  drove .img.zst here -- "flash-time decompression is on the hot
  path of per-job CI reflash" -- conflated two different cases:
  the per-job reflash hot path applies to operator-supplied
  target images (any of the 4 compressed forms work), NOT to
  the bty-server appliance itself, which is flashed once during
  initial setup. Universal flasher compat wins for one-shot
  setup; the speed advantage of zstd was buying nothing for the
  bty-shipped artifacts.
- Operators running per-job CI reflash on a fast disk can pick
  ``.img.zst`` for their own images and the flash code will
  stream-decompress at zstd's ~800-1500 MB/s. zstd's only
  downside is the version-cliff in some host-side flasher
  ecosystems, which doesn't apply to bty's flash code -- it
  shells out to the system ``zstd`` binary, which is universal
  on Linux.
- Decompression speed ranking (rough): zstd > gzip > xz > bzip2.
  Pick based on workload: gzip for one-shot delivery, zstd for
  hot-path reflash.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default image root. Operators override via ``--image-root`` or the
# ``BTY_IMAGE_ROOT`` environment variable. The USB live appliance mounts
# the BTY_IMAGES partition here.
DEFAULT_IMAGE_ROOT = Path("/var/lib/bty/images")

# Supported extensions, ordered most-specific first so multi-suffix
# variants (``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``)
# win over the bare ``.img``.
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    (".img.zst", "img.zst"),
    (".img.xz", "img.xz"),
    (".img.gz", "img.gz"),
    (".img.bz2", "img.bz2"),
    (".qcow2", "qcow2"),
    (".img", "img"),
)

# Extensions explicitly NOT supported by the single-stream flash
# pipeline. Tarballs wrap the actual image inside per-file headers;
# decompressing the gzip/xz layer doesn't yield raw image bytes,
# it yields a tar stream. dd'ing that into a target disk would
# write tar headers into the MBR. Operators with these files must
# extract first (``tar -xzf foo.tar.gz`` etc.) and drop the
# resulting .img onto BTY_IMAGES.
_TARBALL_HINT_EXTS: tuple[str, ...] = (
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tgz",
    ".txz",
    ".tbz2",
    ".tzst",
)


def is_tarball_extension(name: str) -> bool:
    """Return True if ``name`` looks like a tar archive that bty
    cannot flash directly (caller should hint the operator to
    extract first)."""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _TARBALL_HINT_EXTS)


@dataclass(frozen=True)
class Image:
    """A discovered image file. Plain bytes-on-disk metadata only."""

    name: str
    path: Path
    format: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "format": self.format,
            "size_bytes": self.size_bytes,
        }


def default_image_root() -> Path:
    """Resolve the configured image root.

    Precedence: ``BTY_IMAGE_ROOT`` env var, then ``DEFAULT_IMAGE_ROOT``.
    """
    env = os.environ.get("BTY_IMAGE_ROOT")
    return Path(env) if env else DEFAULT_IMAGE_ROOT


def detect_format(path: Path) -> str | None:
    """Return the image format identifier for ``path``, or ``None``."""
    name = path.name.lower()
    for ext, fmt in _EXTENSIONS:
        if name.endswith(ext):
            return fmt
    return None


def list_images(root: Path) -> list[Image]:
    """List supported images directly under ``root`` (non-recursive)."""
    if not root.exists() or not root.is_dir():
        return []

    out: list[Image] = []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        fmt = detect_format(p)
        if fmt is None:
            continue
        out.append(
            Image(
                name=p.name,
                path=p,
                format=fmt,
                size_bytes=p.stat().st_size,
            )
        )
    return out


def inspect_image(path: Path) -> dict[str, Any]:
    """Return detailed metadata for a single image file.

    Always includes ``path``, ``format``, and ``size_bytes``. Adds a
    format-specific ``detail`` block when the relevant tool succeeds:

    - ``qcow2`` -> the JSON output of ``qemu-img info --output=json``
    - ``img.zst`` -> the textual output of ``zstd -l``
    - ``img.xz`` -> the textual output of ``xz -l``
    - ``img.gz`` -> the textual output of ``gzip -l``
    - ``img.bz2`` -> nothing (bzip2 has no listing tool)
    """
    if not path.exists():
        raise FileNotFoundError(path)

    fmt = detect_format(path)
    info: dict[str, Any] = {
        "path": str(path),
        "format": fmt,
        "size_bytes": path.stat().st_size,
    }

    if fmt == "qcow2":
        proc = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = json.loads(proc.stdout)
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.zst":
        proc = subprocess.run(
            ["zstd", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.xz":
        proc = subprocess.run(
            ["xz", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    elif fmt == "img.gz":
        proc = subprocess.run(
            ["gzip", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            info["detail"] = proc.stdout.strip()
        else:
            info["detail_error"] = proc.stderr.strip()
    # img.bz2: no listing tool ships with bzip2; ``detail`` block
    # is intentionally omitted.

    return info
