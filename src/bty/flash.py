"""Flash plan: validate that an image can be written to a target disk.

Split into three layers so unit tests don't need to mock anything to
cover the validation logic:

- ``probe_image`` and ``probe_target`` do the I/O (reading file stats,
  shelling out to ``qemu-img info``, ``zstd -l``, ``lsblk``) and return
  plain :class:`ImageInfo` / :class:`TargetInfo` dataclasses.
- ``make_plan`` is pure: it bundles probed info into a :class:`FlashPlan`.
- ``validate_plan`` is pure: it returns a list of error strings.

The CLI calls all four. Tests construct ``ImageInfo`` / ``TargetInfo``
directly and exercise ``make_plan`` / ``validate_plan`` without mocks.
The probe functions get their own targeted tests for the
subprocess-shelling-out parts.

The actual write step lands in milestone 6.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from bty import images

# Provisioning modes accepted by ``bty flash``. Validation only at this
# milestone; behaviour lands in milestones 7-9.
PROVISIONING_MODES: tuple[str, ...] = ("none", "cloud-init", "cijoe")

_ZSTD_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}

_ZSTD_SIZE_RE = re.compile(r"([\d.]+)\s+(B|KiB|MiB|GiB|TiB)")


@dataclass
class ImageInfo:
    """Probed metadata for an image file."""

    path: Path
    format: str | None
    size_bytes: int
    virtual_size_bytes: int | None  # what would be written to disk; None = unknown


@dataclass
class TargetInfo:
    """Probed metadata for a candidate target."""

    path: Path
    exists: bool
    is_block_device: bool
    size_bytes: int | None
    mountpoints: list[str]


@dataclass
class FlashPlan:
    """Inputs and computed metadata for a flash operation."""

    image: ImageInfo
    target: TargetInfo
    provisioning_mode: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image": {
                "path": str(self.image.path),
                "format": self.image.format,
                "size_bytes": self.image.size_bytes,
                "virtual_size_bytes": self.image.virtual_size_bytes,
            },
            "target": {
                "path": str(self.target.path),
                "exists": self.target.exists,
                "is_block_device": self.target.is_block_device,
                "size_bytes": self.target.size_bytes,
                "mountpoints": list(self.target.mountpoints),
            },
            "provisioning_mode": self.provisioning_mode,
            "notes": list(self.notes),
        }


# ---------- I/O: probing -----------------------------------------------------


def probe_image(path: Path) -> ImageInfo:
    """Inspect an image file on disk. Raises ``FileNotFoundError`` if missing."""
    if not path.exists():
        raise FileNotFoundError(f"image not found: {path}")
    fmt = images.detect_format(path)
    return ImageInfo(
        path=path,
        format=fmt,
        size_bytes=path.stat().st_size,
        virtual_size_bytes=_image_virtual_size(path, fmt),
    )


def probe_target(path: Path) -> TargetInfo:
    """Inspect a candidate target path. Never raises; returns a populated info."""
    if not path.exists():
        return TargetInfo(
            path=path,
            exists=False,
            is_block_device=False,
            size_bytes=None,
            mountpoints=[],
        )

    try:
        st = path.stat()
    except OSError:
        return TargetInfo(
            path=path,
            exists=True,
            is_block_device=False,
            size_bytes=None,
            mountpoints=[],
        )

    is_block = stat.S_ISBLK(st.st_mode)
    if not is_block:
        return TargetInfo(
            path=path,
            exists=True,
            is_block_device=False,
            size_bytes=None,
            mountpoints=[],
        )

    return TargetInfo(
        path=path,
        exists=True,
        is_block_device=True,
        size_bytes=_lsblk_target_size(path),
        mountpoints=_lsblk_target_mountpoints(path),
    )


# ---------- Pure plan building + validation ----------------------------------


def make_plan(
    image: ImageInfo,
    target: TargetInfo,
    provisioning_mode: str,
) -> FlashPlan:
    """Bundle probed info into a :class:`FlashPlan`. Pure; no I/O."""
    plan = FlashPlan(image=image, target=target, provisioning_mode=provisioning_mode)
    if image.virtual_size_bytes is None and image.format is not None:
        plan.notes.append(
            "image virtual size could not be determined; size-fits-target check skipped"
        )
    return plan


def validate_plan(plan: FlashPlan) -> list[str]:
    """Return a list of error messages describing why ``plan`` is invalid.

    Empty list = the plan would be safe to execute as a real flash.
    Pure; no I/O.
    """
    errors: list[str] = []

    if plan.image.format is None:
        errors.append(
            f"image format not recognised: {plan.image.path} (supported: .qcow2, .img, .img.zst)"
        )

    if not plan.target.exists:
        errors.append(f"target does not exist: {plan.target.path}")
    elif not plan.target.is_block_device:
        errors.append(f"target is not a block device: {plan.target.path}")

    if plan.target.mountpoints:
        errors.append(f"target has mounted partitions: {', '.join(plan.target.mountpoints)}")

    if (
        plan.target.size_bytes is not None
        and plan.image.virtual_size_bytes is not None
        and plan.image.virtual_size_bytes > plan.target.size_bytes
    ):
        errors.append(
            f"image ({plan.image.virtual_size_bytes} bytes) "
            f"is larger than target ({plan.target.size_bytes} bytes)"
        )

    if plan.provisioning_mode not in PROVISIONING_MODES:
        errors.append(
            f"unknown provisioning mode: {plan.provisioning_mode!r} "
            f"(supported: {', '.join(PROVISIONING_MODES)})"
        )

    return errors


def print_plan(
    plan: FlashPlan,
    errors: list[str],
    *,
    file: IO[str] | None = None,
) -> None:
    """Render ``plan`` and any ``errors`` for human consumption."""
    out = file if file is not None else sys.stdout

    virtual = _fmt_bytes(plan.image.virtual_size_bytes)
    target_size = _fmt_bytes(plan.target.size_bytes)
    mounts = ", ".join(plan.target.mountpoints) if plan.target.mountpoints else "(none)"

    print("Flash plan:", file=out)
    print(f"  image:               {plan.image.path}", file=out)
    print(f"  image format:        {plan.image.format}", file=out)
    print(f"  image size on disk:  {plan.image.size_bytes} bytes", file=out)
    print(f"  image virtual size:  {virtual}", file=out)
    print(f"  target:              {plan.target.path}", file=out)
    print(f"  target is block:     {plan.target.is_block_device}", file=out)
    print(f"  target size:         {target_size}", file=out)
    print(f"  target mountpoints:  {mounts}", file=out)
    print(f"  provisioning mode:   {plan.provisioning_mode}", file=out)

    if plan.notes:
        print(file=out)
        print("Notes:", file=out)
        for note in plan.notes:
            print(f"  - {note}", file=out)

    print(file=out)
    if errors:
        print("Validation: FAILED", file=out)
        for err in errors:
            print(f"  - {err}", file=out)
    else:
        print("Validation: OK (dry-run; no writes performed)", file=out)


# ---------- Internal helpers --------------------------------------------------


def _fmt_bytes(value: int | None) -> str:
    return f"{value} bytes" if value is not None else "(unknown) bytes"


def _image_virtual_size(path: Path, image_format: str | None) -> int | None:
    """Return the byte count an image would expand to on disk."""
    if image_format == "img":
        return path.stat().st_size

    if image_format == "qcow2":
        proc = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        size = payload.get("virtual-size")
        return size if isinstance(size, int) else None

    if image_format == "img.zst":
        proc = subprocess.run(
            ["zstd", "-l", "--no-progress", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return _parse_zstd_uncompressed(proc.stdout)

    return None


def _parse_zstd_uncompressed(zstd_output: str) -> int | None:
    """Best-effort extraction of the uncompressed size from ``zstd -l``."""
    for line in zstd_output.splitlines():
        if not line.strip() or line.lstrip().startswith(("Frames", "-")):
            continue
        cells = _ZSTD_SIZE_RE.findall(line)
        if len(cells) >= 2:
            value_str, unit = cells[1]
            try:
                value = float(value_str)
            except ValueError:
                return None
            multiplier = _ZSTD_SIZE_UNITS.get(unit)
            return int(value * multiplier) if multiplier is not None else None
    return None


def _lsblk_target_size(target: Path) -> int | None:
    """Return target size in bytes via ``lsblk -bndo SIZE`` (top-level only)."""
    proc = subprocess.run(
        ["lsblk", "-bndo", "SIZE", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    try:
        return int(line)
    except ValueError:
        return None


def _lsblk_target_mountpoints(target: Path) -> list[str]:
    """Return all mountpoints used by ``target`` and its partitions."""
    proc = subprocess.run(
        ["lsblk", "-no", "MOUNTPOINTS", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [mp for raw in proc.stdout.splitlines() if (mp := raw.strip())]
