"""Image catalog discovery and inspection.

Recognises the supported on-disk image formats (``.qcow2``, ``.img``,
``.img.zst``), lists them under a configured image root, and extracts
detail metadata for individual images via the appropriate tool
(``qemu-img info`` for qcow2, ``zstd -l`` for zstd-compressed raws).
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

# Supported extensions, ordered most-specific first so ``.img.zst`` wins
# over ``.img``.
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    (".img.zst", "img.zst"),
    (".qcow2", "qcow2"),
    (".img", "img"),
)


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

    return info
