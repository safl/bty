"""Image catalog discovery and inspection.

Recognises the supported on-disk image formats (``.qcow2``, ``.img``,
``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``), lists them
under a configured image root, and extracts detail metadata for
individual images via the appropriate tool (``qemu-img info`` for
qcow2, ``zstd -l`` / ``xz -l`` / ``gzip -l`` for the corresponding
compressed raws; bzip2 has no listing tool so .img.bz2 has no
detail block).

Format-choice rationale (load-bearing for the CI-pipeline use case
that drove bty's design):

- The **USB stick image** ships as ``.iso.gz``. Operators write
  it host-side via Etcher / Rufus / Raspberry Pi Imager, which
  decompress .gz natively in every flasher we tested (xz tripped
  Etcher's bundled decompressor regardless of how the file was
  shaped; gzip is the universally-supported lowest common
  denominator). Stick prep is a one-shot, host-side cost;
  decompression speed at flash time doesn't matter (the host
  decompresses on its own beefy CPU once, not in a hot loop).
- The **target images** bty ships
  (``bty-server-x86_64.img.zst``,
  ``bty-server-rpi-arm64.img.zst``) are zstd-compressed because
  flash-time decompression is on the hot path. For per-job
  CI reflash (the primary bty use case: every CI job that
  starts on a target machine reflashes it to a known clean
  baseline first), the flash time is added to every job's
  bring-up. zstd decompresses at ~800-1500 MB/s and saturates
  the target disk; xz decompresses at ~50-100 MB/s and
  bottlenecks the flash by ~7x (~80 seconds extra per job in
  absolute terms). At even 50 CI jobs/day per target that's
  ~70 minutes/day of wasted compute time per target, scaling
  linearly with target count. The cost is real and recurrent;
  zstd is the right call for the hot path.
- For parallel PXE fleet flash (one-time ``new-image`` reflash
  across N machines): the difference is just the slowest
  per-machine wall-clock, ~80s. Each target decompresses on its
  own CPU in parallel, so the cost doesn't multiply by N.
- bty's flash code accepts ``.img``, ``.img.zst``, AND
  ``.img.xz`` regardless of what bty itself ships, so operators
  who arrive with their own xz-compressed images aren't blocked.
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
