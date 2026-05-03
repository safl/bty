"""Flash plan: validate that an image can be written to a target disk.

Milestone 5 surface: ``plan_flash`` builds a :class:`FlashPlan`,
``validate_plan`` returns a list of error strings (empty list means the
plan is good), and ``print_plan`` renders the plan and any errors for
human consumption. The actual write step lands in milestone 6.
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

# Human-readable size units recognised when parsing zstd's textual output.
_ZSTD_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}

# Match a "12.34 MiB"-style size cell in zstd -l output.
_ZSTD_SIZE_RE = re.compile(r"([\d.]+)\s+(B|KiB|MiB|GiB|TiB)")


@dataclass
class FlashPlan:
    """Captures the inputs and computed metadata for a flash operation."""

    image: Path
    image_format: str | None
    image_size_bytes: int
    image_virtual_size_bytes: int | None  # what gets written; None = unknown
    target: Path
    target_size_bytes: int | None
    target_is_block_device: bool
    target_mountpoints: list[str]
    provisioning_mode: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image": {
                "path": str(self.image),
                "format": self.image_format,
                "size_bytes": self.image_size_bytes,
                "virtual_size_bytes": self.image_virtual_size_bytes,
            },
            "target": {
                "path": str(self.target),
                "size_bytes": self.target_size_bytes,
                "is_block_device": self.target_is_block_device,
                "mountpoints": list(self.target_mountpoints),
            },
            "provisioning_mode": self.provisioning_mode,
            "notes": list(self.notes),
        }


def plan_flash(image: Path, target: Path, provisioning_mode: str) -> FlashPlan:
    """Gather the metadata needed to validate a flash without writing.

    Raises ``FileNotFoundError`` if ``image`` does not exist; the target
    is allowed to not exist so the dry-run can report a useful error.
    """
    if not image.exists():
        raise FileNotFoundError(f"image not found: {image}")

    image_format = images.detect_format(image)
    image_size_bytes = image.stat().st_size
    virtual_size = _image_virtual_size(image, image_format)

    target_is_block, target_size, target_mounts = _probe_target(target)

    return FlashPlan(
        image=image,
        image_format=image_format,
        image_size_bytes=image_size_bytes,
        image_virtual_size_bytes=virtual_size,
        target=target,
        target_size_bytes=target_size,
        target_is_block_device=target_is_block,
        target_mountpoints=target_mounts,
        provisioning_mode=provisioning_mode,
    )


def validate_plan(plan: FlashPlan) -> list[str]:
    """Return a list of error messages describing why ``plan`` is invalid.

    An empty list means the plan would be safe to execute as a real
    flash (modulo any race conditions between dry-run and execution).
    """
    errors: list[str] = []

    if plan.image_format is None:
        errors.append(
            f"image format not recognised: {plan.image} (supported: .qcow2, .img, .img.zst)"
        )

    if not plan.target.exists():
        errors.append(f"target does not exist: {plan.target}")
    elif not plan.target_is_block_device:
        errors.append(f"target is not a block device: {plan.target}")

    if plan.target_mountpoints:
        errors.append(f"target has mounted partitions: {', '.join(plan.target_mountpoints)}")

    if (
        plan.target_size_bytes is not None
        and plan.image_virtual_size_bytes is not None
        and plan.image_virtual_size_bytes > plan.target_size_bytes
    ):
        errors.append(
            f"image ({plan.image_virtual_size_bytes} bytes) "
            f"is larger than target ({plan.target_size_bytes} bytes)"
        )

    if plan.provisioning_mode not in PROVISIONING_MODES:
        errors.append(
            f"unknown provisioning mode: {plan.provisioning_mode!r} "
            f"(supported: {', '.join(PROVISIONING_MODES)})"
        )

    if plan.image_virtual_size_bytes is None:
        plan.notes.append(
            "image virtual size could not be determined; size-fits-target check skipped"
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

    virtual = _fmt_bytes(plan.image_virtual_size_bytes)
    target_size = _fmt_bytes(plan.target_size_bytes)
    mounts = ", ".join(plan.target_mountpoints) if plan.target_mountpoints else "(none)"

    print("Flash plan:", file=out)
    print(f"  image:               {plan.image}", file=out)
    print(f"  image format:        {plan.image_format}", file=out)
    print(f"  image size on disk:  {plan.image_size_bytes} bytes", file=out)
    print(f"  image virtual size:  {virtual}", file=out)
    print(f"  target:              {plan.target}", file=out)
    print(f"  target is block:     {plan.target_is_block_device}", file=out)
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
        if isinstance(size, int):
            return size
        return None

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
    """Best-effort extraction of the uncompressed size from ``zstd -l``.

    The output is a small table; the data row has compressed and
    uncompressed sizes formatted as e.g. ``500 MiB``. We extract the
    second size cell on the first non-header line.
    """
    matches: list[tuple[str, str]] = []
    for line in zstd_output.splitlines():
        if not line.strip() or line.lstrip().startswith(("Frames", "-")):
            continue
        cells = _ZSTD_SIZE_RE.findall(line)
        if len(cells) >= 2:
            matches.append(cells[1])
            break
    if not matches:
        return None
    value_str, unit = matches[0]
    try:
        value = float(value_str)
    except ValueError:
        return None
    multiplier = _ZSTD_SIZE_UNITS.get(unit)
    if multiplier is None:
        return None
    return int(value * multiplier)


def _probe_target(target: Path) -> tuple[bool, int | None, list[str]]:
    """Inspect a candidate target. Returns (is_block, size_bytes, mountpoints)."""
    if not target.exists():
        return (False, None, [])

    try:
        st = target.stat()
    except OSError:
        return (False, None, [])

    is_block = stat.S_ISBLK(st.st_mode)
    if not is_block:
        return (False, None, [])

    size_bytes = _lsblk_target_size(target)
    mountpoints = _lsblk_target_mountpoints(target)
    return (True, size_bytes, mountpoints)


def _lsblk_target_size(target: Path) -> int | None:
    """Return target size in bytes via ``lsblk -bno SIZE`` (top-level only)."""
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
    out: list[str] = []
    for raw in proc.stdout.splitlines():
        mp = raw.strip()
        if mp:
            out.append(mp)
    return out
