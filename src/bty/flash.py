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
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from bty import images


@dataclass
class FlashProgress:
    """One lifecycle event from :func:`execute_plan` / ``cmd_flash``.

    The ``event`` field is a stable string callers dispatch on. Current
    events:

    - ``started``      — flash beginning; ``total_bytes`` is the image's
      virtual size when known.
    - ``writing``      — about to invoke the format-specific writer
      (``dd`` / ``zstd | dd`` / ``qemu-img convert``).
    - ``synced``       — kernel buffers flushed.
    - ``partprobed``   — partition table re-read; flash hardware-complete.
    - ``provisioning`` — emitted by ``cmd_flash`` around an
      ``apply_cloud_init`` / ``apply_cijoe`` step (``note`` describes
      which mode).
    - ``done``         — emitted by ``cmd_flash`` after every step
      succeeded.
    - ``failed``       — emitted on any :class:`FlashError`; ``note``
      carries the exception string. The exception is then re-raised.

    ``total_bytes`` is the image's virtual size in bytes when known; it
    is set on the ``started`` event and may be carried on later events
    in a future byte-level-progress milestone.
    """

    event: str
    note: str = ""
    total_bytes: int | None = None


ProgressCallback = Callable[[FlashProgress], None]


def _emit(progress: ProgressCallback | None, event: str, **fields: Any) -> None:
    """Call ``progress`` with a :class:`FlashProgress` if one was provided."""
    if progress is None:
        return
    progress(FlashProgress(event=event, **fields))


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


# ---------- Real write -------------------------------------------------------


class FlashError(RuntimeError):
    """Raised when a flash-related operation cannot complete.

    Distinct subclasses exist for failure modes that callers (notably
    the CLI and TUI) may want to surface as different exit codes /
    user-facing messages:

    - :class:`FlashDependencyError` — a required external tool is missing.
    - :class:`FlashRaceError` — the target's state changed between the
      last successful probe and the attempted write (it became mounted,
      stopped being a block device, etc.).

    Plain :class:`FlashError` is the catch-all for everything else
    (invalid format, write subprocess returned non-zero, etc.).
    """


class FlashDependencyError(FlashError):
    """A required external tool (cijoe, mkfs.exfat, ...) is not installed."""


class FlashRaceError(FlashError):
    """The target changed state between probe and write (mounted, removed, ...)."""


def execute_plan(
    plan: FlashPlan,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    """Write ``plan.image`` to ``plan.target``.

    Re-probes the target immediately before writing to catch races
    (target gets mounted, swapped, or removed between the dry-run and
    the real flash). Dispatches to the right write strategy based on
    image format. Synchronises and re-reads the partition table on
    success.

    If ``progress`` is given, lifecycle :class:`FlashProgress` events
    are emitted: ``started``, ``writing``, ``synced``, ``partprobed``.
    On any :class:`FlashError`, a ``failed`` event is emitted with the
    exception string in ``note`` and the exception re-raised.

    Raises :class:`FlashError` for caller-visible failures (target no
    longer suitable, format unrecognised, write subprocess failed).
    """
    _emit(progress, "started", total_bytes=plan.image.virtual_size_bytes)

    try:
        fresh_target = probe_target(plan.target.path)
        if not fresh_target.exists or not fresh_target.is_block_device:
            raise FlashRaceError(f"target is no longer a block device: {plan.target.path}")
        if fresh_target.mountpoints:
            raise FlashRaceError(
                f"target now has mounted partitions: {', '.join(fresh_target.mountpoints)}"
            )

        fmt = plan.image.format
        _emit(progress, "writing", note=fmt or "?")
        if fmt == "img":
            _flash_img(plan.image.path, plan.target.path)
        elif fmt == "img.zst":
            _flash_zst(plan.image.path, plan.target.path)
        elif fmt == "qcow2":
            _flash_qcow2(plan.image.path, plan.target.path)
        else:
            raise FlashError(f"cannot flash image of format {fmt!r}")

        _sync_target(plan.target.path)
        _emit(progress, "synced")

        _partprobe_target(plan.target.path)
        _emit(progress, "partprobed")
    except FlashError as exc:
        _emit(progress, "failed", note=str(exc))
        raise


def _flash_img(image: Path, target: Path) -> None:
    """Write a raw .img to a block device with ``dd``."""
    cmd = [
        "dd",
        f"if={image}",
        f"of={target}",
        "bs=4M",
        "conv=fsync",
        "status=progress",
    ]
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        raise FlashError(f"dd exited {rc} writing {image} -> {target}")


def _flash_zst(image: Path, target: Path) -> None:
    """Pipeline ``zstd -d --stdout IMG | dd of=TARGET ...``."""
    zstd_proc = subprocess.Popen(
        ["zstd", "-d", "--stdout", str(image)],
        stdout=subprocess.PIPE,
    )
    try:
        dd_proc = subprocess.Popen(
            [
                "dd",
                f"of={target}",
                "bs=4M",
                "conv=fsync",
                "status=progress",
            ],
            stdin=zstd_proc.stdout,
        )
        # Let zstd see SIGPIPE if dd exits early.
        if zstd_proc.stdout is not None:
            zstd_proc.stdout.close()
        dd_rc = dd_proc.wait()
    finally:
        zstd_rc = zstd_proc.wait()

    if dd_rc != 0:
        raise FlashError(f"dd exited {dd_rc} writing {image} -> {target}")
    if zstd_rc != 0:
        raise FlashError(f"zstd exited {zstd_rc} decompressing {image}")


def _flash_qcow2(image: Path, target: Path) -> None:
    """Write a qcow2 to a block device by converting to raw in place."""
    cmd = ["qemu-img", "convert", "-p", "-O", "raw", str(image), str(target)]
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        raise FlashError(f"qemu-img convert exited {rc} writing {image} -> {target}")


def _sync_target(target: Path) -> None:
    """Flush kernel buffers; ``target`` accepted for symmetry with the partprobe sibling."""
    del target  # informational only at this stage
    subprocess.run(["sync"], check=False)


def _partprobe_target(target: Path) -> None:
    """Ask the kernel to re-read ``target``'s partition table, then settle udev.

    ``udevadm settle`` is run after ``partprobe`` so subsequent ``lsblk``
    queries see the new partition tree. Without it, an immediate
    follow-up (e.g. ``apply_cloud_init`` looking for the rootfs partition)
    can race the kernel's partition scan and find no children.
    """
    subprocess.run(["partprobe", str(target)], check=False)
    subprocess.run(["udevadm", "settle"], check=False)


# ---------- Provisioning: cloud-init ----------------------------------------


def apply_cloud_init(
    target: Path,
    user_data: Path,
    meta_data: Path | None = None,
) -> None:
    """Drop NoCloud seed files into the target's cloud-init-enabled rootfs.

    Mounts the partition on ``target`` whose rootfs contains ``/etc/cloud/``
    (the unambiguous "cloud-init lives here" marker), writes
    ``user-data`` and ``meta-data`` under
    ``/var/lib/cloud/seed/nocloud-net/`` on it, then unmounts. cloud-init
    picks the seed up on first boot via the NoCloud datasource.

    Raises :class:`FlashError` when the target has no cloud-init-enabled
    rootfs partition, or when mounting / writing fails.
    """
    if not user_data.exists():
        raise FlashError(f"user-data file not found: {user_data}")
    if meta_data is not None and not meta_data.exists():
        raise FlashError(f"meta-data file not found: {meta_data}")

    rootfs = _find_cloud_init_rootfs(target)

    with tempfile.TemporaryDirectory(prefix="bty-cloud-init-") as mp:
        mount_point = Path(mp)
        rc = subprocess.run(["mount", str(rootfs), str(mount_point)], check=False).returncode
        if rc != 0:
            raise FlashError(f"failed to mount {rootfs} at {mount_point}")
        try:
            seed_dir = mount_point / "var" / "lib" / "cloud" / "seed" / "nocloud-net"
            seed_dir.mkdir(parents=True, exist_ok=True)

            shutil.copy2(user_data, seed_dir / "user-data")
            if meta_data is not None:
                shutil.copy2(meta_data, seed_dir / "meta-data")
            else:
                (seed_dir / "meta-data").write_text(_default_meta_data())

            subprocess.run(["sync"], check=False)
        finally:
            subprocess.run(["umount", str(mount_point)], check=False)


def _default_meta_data() -> str:
    """Synthesise a minimal NoCloud meta-data with a unique instance-id."""
    instance_id = "bty-" + uuid.uuid4().hex[:12]
    return f"instance-id: {instance_id}\nlocal-hostname: bty-host\n"


def _find_cloud_init_rootfs(target: Path) -> Path:
    """Return the partition device on ``target`` that has cloud-init installed.

    Iterates partitions reported by ``lsblk -J``, mounts each read-only,
    and returns the first whose rootfs contains ``/etc/cloud/``. Raises
    :class:`FlashError` if no such partition is found.

    A ``udevadm settle`` is issued first so a freshly-partitioned target
    is fully visible in sysfs by the time we query it.
    """
    subprocess.run(["udevadm", "settle"], check=False)

    proc = subprocess.run(
        ["lsblk", "-J", "-o", "PATH,TYPE", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise FlashError(f"lsblk failed for {target}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)
    partitions = _collect_partitions(payload.get("blockdevices", []))

    for part_path in partitions:
        if _partition_has_cloud_init(part_path):
            return part_path

    raise FlashError(
        f"no partition on {target} appears to have cloud-init installed "
        f"(checked for /etc/cloud/ on each partition); lsblk reported: "
        f"{proc.stdout.strip()!r}"
    )


def _collect_partitions(entries: list[dict[str, Any]]) -> list[Path]:
    """Walk an ``lsblk -J`` tree and return every entry of type ``part``.

    Different ``lsblk`` versions / option combinations sometimes return
    a tree (parent with ``children``) and sometimes a flat sibling list
    when the user passes a device path; both shapes are handled.
    """
    out: list[Path] = []
    for entry in entries:
        if entry.get("type") == "part" and entry.get("path"):
            out.append(Path(entry["path"]))
        children = entry.get("children")
        if children:
            out.extend(_collect_partitions(children))
    return out


def _partition_has_cloud_init(part: Path) -> bool:
    """Mount ``part`` read-only briefly; return True if ``/etc/cloud/`` exists."""
    with tempfile.TemporaryDirectory(prefix="bty-probe-") as mp:
        rc = subprocess.run(
            ["mount", "-r", str(part), mp],
            capture_output=True,
            check=False,
        ).returncode
        if rc != 0:
            return False
        try:
            return (Path(mp) / "etc" / "cloud").is_dir()
        finally:
            subprocess.run(["umount", mp], capture_output=True, check=False)


# ---------- Provisioning: cijoe (offline) ------------------------------------


def apply_cijoe(
    target: Path,
    workflow: Path,
    config: Path | None = None,
) -> None:
    """Run a CIJOE workflow against the target's mounted rootfs.

    Mounts the largest partition on ``target`` (heuristic for the
    rootfs), exports ``BTY_ROOTFS`` pointing at the mount, then invokes
    ``cijoe <workflow> -c <config> --monitor``. The workflow's tasks
    can read / mutate the rootfs through ``$BTY_ROOTFS``; bty itself
    does not interpret what the workflow does.

    cijoe requires a config file even for trivial workflows. When the
    operator does not supply ``--cijoe-config``, bty synthesises a
    minimal default into the working tempdir so the workflow can run.

    Raises :class:`FlashError` if ``cijoe`` is not installed, the
    workflow / config files are missing, the rootfs cannot be mounted,
    or the workflow exits non-zero.
    """
    if not workflow.exists():
        raise FlashError(f"cijoe workflow not found: {workflow}")
    if config is not None and not config.exists():
        raise FlashError(f"cijoe config not found: {config}")
    if shutil.which("cijoe") is None:
        raise FlashDependencyError(
            "cijoe is not installed; install with `pipx install cijoe` and re-run"
        )

    rootfs = _find_largest_partition(target)

    with tempfile.TemporaryDirectory(prefix="bty-cijoe-") as workdir:
        workdir_path = Path(workdir)
        mount_point = workdir_path / "rootfs"
        mount_point.mkdir()

        if config is not None:
            effective_config = config
        else:
            effective_config = workdir_path / "cijoe-config.toml"
            effective_config.write_text(_default_cijoe_config())

        rc = subprocess.run(["mount", str(rootfs), str(mount_point)], check=False).returncode
        if rc != 0:
            raise FlashError(f"failed to mount {rootfs} at {mount_point}")
        try:
            env = os.environ.copy()
            env["BTY_ROOTFS"] = str(mount_point)

            cmd = [
                "cijoe",
                str(workflow),
                "--monitor",
                "-c",
                str(effective_config),
            ]
            rc = subprocess.run(cmd, env=env, check=False).returncode
            if rc != 0:
                raise FlashError(f"cijoe workflow exited {rc}")

            subprocess.run(["sync"], check=False)
        finally:
            subprocess.run(["umount", str(mount_point)], capture_output=True, check=False)


def _default_cijoe_config() -> str:
    """Synthesise the minimum cijoe config that satisfies cijoe's loader."""
    return "[cijoe.workflow]\nfail_fast = true\n"


def _find_largest_partition(target: Path) -> Path:
    """Return the largest partition device on ``target``.

    Heuristic for "the rootfs" — works for typical cooked images where
    the root partition dominates the disk. Operators who need a
    different partition will get an explicit selector when one
    becomes necessary.
    """
    subprocess.run(["udevadm", "settle"], check=False)

    proc = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "PATH,TYPE,SIZE", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise FlashError(f"lsblk failed for {target}: {proc.stderr.strip()}")

    payload = json.loads(proc.stdout)
    parts = _collect_partition_entries(payload.get("blockdevices", []))
    if not parts:
        raise FlashError(
            f"no partitions found on {target}; lsblk reported: {proc.stdout.strip()!r}"
        )

    parts.sort(key=lambda p: int(p.get("size") or 0), reverse=True)
    return Path(parts[0]["path"])


def _collect_partition_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk an ``lsblk -J`` tree; return raw entries of type ``part``.

    Variant of :func:`_collect_partitions` that yields the full entry
    so callers can read additional fields (e.g. SIZE) — not just the path.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") == "part":
            out.append(entry)
        children = entry.get("children")
        if children:
            out.extend(_collect_partition_entries(children))
    return out


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
