"""Flash plan: validate that an image can be written to a target disk.

Split into three layers so unit tests don't need to mock anything to
cover the validation logic:

- ``probe_image`` and ``probe_target`` do the I/O (reading file stats,
  shelling out to ``qemu-img info``, ``zstd -l``, ``lsblk``) and return
  plain :class:`ImageInfo` / :class:`TargetInfo` dataclasses.
- ``make_plan`` is pure: it bundles probed info into a :class:`FlashPlan`.
- ``validate_plan`` is pure: it returns a list of error strings.
- ``execute_plan`` does the destructive write (qemu-img convert /
  zstd -d / dd as appropriate for the image format) and applies
  the chosen post-flash provisioning mode (cloud-init, cijoe, none).

The CLI calls all four. Tests construct ``ImageInfo`` / ``TargetInfo``
directly and exercise ``make_plan`` / ``validate_plan`` without mocks.
The probe and write functions have their own targeted tests for the
subprocess-shelling-out parts; integration tests against a real
loop device live in ``tests/test_flash_integration.py``.
"""

from __future__ import annotations

import contextlib
import json
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, TypeAlias

from bty import images


@dataclass
class FlashProgress:
    """One lifecycle event from :func:`execute_plan` / ``cmd_flash``.

    The ``event`` field is a stable string callers dispatch on. Current
    events:

    - ``started``           - flash beginning; ``total_bytes`` is the
      image's virtual size when known.
    - ``writing``           - about to invoke the format-specific
      writer (``dd`` / ``zstd | dd`` / ``qemu-img convert``).
    - ``writing_progress``  - byte-level progress from the running
      writer; ``bytes_written`` is set, ``total_bytes`` carries
      through from ``started`` so consumers can compute percent /
      ETA without holding state. Emitted ~1/sec from a daemon
      thread that parses ``dd``'s ``status=progress`` stderr.
    - ``synced``            - kernel buffers flushed.
    - ``partprobed``        - partition table re-read; flash
      hardware-complete.
    - ``done``              - emitted by ``cmd_flash`` after the
      flash succeeded. v0.7.39 dropped the offline-provisioning
      step; the ``provisioning`` event is gone.
    - ``failed``            - emitted on any :class:`FlashError`;
      ``note`` carries the exception string. The exception is then
      re-raised.

    ``total_bytes`` is the image's virtual size when known (set on
    ``started`` and carried on ``writing_progress``). ``bytes_written``
    is set only on ``writing_progress``.
    """

    event: str
    note: str = ""
    total_bytes: int | None = None
    bytes_written: int | None = None


ProgressCallback: TypeAlias = Callable[[FlashProgress], None]


def _emit(progress: ProgressCallback | None, event: str, **fields: Any) -> None:
    """Call ``progress`` with a :class:`FlashProgress` if one was provided."""
    if progress is None:
        return
    progress(FlashProgress(event=event, **fields))


# ``dd status=progress`` writes a periodic line to stderr like:
#   13312000 bytes (13 MB, 13 MiB) copied, 0.103 s, 129 MB/s
# preceded by a ``\r`` so terminals overwrite the prior line. We parse
# the leading byte count and emit a ``writing_progress`` event ~1/sec.
_DD_PROGRESS_RE = re.compile(r"^(\d+)\s+bytes\b")


def _pump_dd_progress(
    stream: IO[str],
    progress: ProgressCallback,
    total_bytes: int | None,
) -> None:
    """Read ``dd``'s stderr and emit ``writing_progress`` events.

    Designed to run in a daemon thread alongside the writer process.
    ``dd`` separates progress lines with ``\\r`` (so each line
    overwrites the previous one in a terminal); we replace ``\\r``
    with ``\\n`` before splitting so we get one progress line per
    chunk regardless of terminal-style behaviour.

    Returns when ``stream`` closes (i.e. dd has exited).
    """
    buf = ""
    while True:
        chunk = stream.read(256)
        if not chunk:
            # Drain whatever's left in the buffer.
            for line in buf.replace("\r", "\n").splitlines():
                m = _DD_PROGRESS_RE.match(line.strip())
                if m:
                    _emit(
                        progress,
                        "writing_progress",
                        bytes_written=int(m.group(1)),
                        total_bytes=total_bytes,
                    )
            return
        buf += chunk
        # Use the LAST progress line in the buffer as the most recent
        # snapshot. dd emits monotonically-increasing byte counts so
        # rendering only the latest is fine.
        lines = buf.replace("\r", "\n").splitlines()
        if not lines:
            continue
        # Keep the partial trailing line for the next read.
        if buf.endswith(("\n", "\r")):
            buf = ""
        else:
            buf = lines[-1]
            lines = lines[:-1]
        # Find the most recent line that matches the byte-count pattern.
        for line in reversed(lines):
            m = _DD_PROGRESS_RE.match(line.strip())
            if m:
                _emit(
                    progress,
                    "writing_progress",
                    bytes_written=int(m.group(1)),
                    total_bytes=total_bytes,
                )
                break


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
    """Probed metadata for an image source.

    Either ``path`` (a local file) or ``url`` (an HTTP/HTTPS URL) is
    set; never both. URL-sourced images stream through curl directly
    to the target disk for ``.img`` / ``.img.zst`` (no temp file); for
    ``.qcow2`` they get downloaded to a temp file first because qcow2
    is random-access.
    """

    path: Path | None
    format: str | None
    size_bytes: int
    virtual_size_bytes: int | None  # what would be written to disk; None = unknown
    url: str | None = None

    @property
    def display(self) -> str:
        """User-facing identifier (URL or path string)."""
        return self.url if self.url is not None else str(self.path)


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
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image": {
                "path": str(self.image.path) if self.image.path is not None else None,
                "url": self.image.url,
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


def probe_image_url(url: str) -> ImageInfo:
    """Inspect an image at an HTTP/HTTPS URL via a HEAD request.

    Format is derived from the URL path's filename extension. Source size
    is read from ``Content-Length`` if present. Virtual size (what gets
    written to disk) can only be determined for raw ``.img`` URLs from
    HEAD; ``.img.zst`` and ``.qcow2`` URLs return ``virtual_size_bytes
    = None`` because computing it would require pulling part of the
    body. Validation handles ``None`` by skipping the size-fits-target
    check with a note.

    Raises ``FileNotFoundError`` if the server doesn't respond or
    returns 4xx / 5xx for the HEAD.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"image URL must be http or https: {url}")
    filename = Path(parsed.path).name or "image"
    fmt = images.detect_format(Path(filename))

    size_bytes = 0
    virtual_size_bytes: int | None = None
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                size_bytes = int(cl)
                if fmt == "img":
                    # Raw .img: source size == virtual size.
                    virtual_size_bytes = size_bytes
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise FileNotFoundError(f"image URL not reachable: {url} ({exc})") from exc

    return ImageInfo(
        path=None,
        url=url,
        format=fmt,
        size_bytes=size_bytes,
        virtual_size_bytes=virtual_size_bytes,
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


def make_plan(image: ImageInfo, target: TargetInfo) -> FlashPlan:
    """Bundle probed info into a :class:`FlashPlan`. Pure; no I/O."""
    plan = FlashPlan(image=image, target=target)
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
        # Specific guidance when the operator dropped a tarball on
        # BTY_IMAGES: those wrap the actual image inside per-file TAR
        # headers, and bty's flash code is single-stream-only. A
        # generic "format not recognised" would leave operators
        # confused; tell them what to do.
        if images.is_tarball_extension(plan.image.display):
            errors.append(
                f"image is a tarball, not a single-file image: "
                f"{plan.image.display}. Extract first "
                f"(``tar -xf {plan.image.display}``) and drop the "
                f"resulting .img / .qcow2 / .img.zst / .img.xz / "
                f".img.gz / .img.bz2 onto BTY_IMAGES."
            )
        else:
            errors.append(
                f"image format not recognised: {plan.image.display} "
                f"(supported: .qcow2, .img, .img.zst, .img.xz, "
                f".img.gz, .img.bz2)"
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
    print(f"  image:               {plan.image.display}", file=out)
    print(f"  image format:        {plan.image.format}", file=out)
    print(f"  image size on disk:  {plan.image.size_bytes} bytes", file=out)
    print(f"  image virtual size:  {virtual}", file=out)
    print(f"  target:              {plan.target.path}", file=out)
    print(f"  target is block:     {plan.target.is_block_device}", file=out)
    print(f"  target size:         {target_size}", file=out)
    print(f"  target mountpoints:  {mounts}", file=out)

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

    - :class:`FlashDependencyError` - a required external tool is missing.
    - :class:`FlashRaceError` - the target's state changed between the
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
        total_bytes = plan.image.virtual_size_bytes
        _emit(progress, "writing", note=fmt or "?")
        if plan.image.url is not None:
            # Streaming pipeline: curl URL | (optional zstd -d) | dd -> target.
            # qcow2 can't stream-convert (random-access), so it's downloaded
            # to a temp file first and then handed to the existing local
            # qcow2 path.
            if fmt == "img":
                _flash_img_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.zst":
                _flash_zst_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.xz":
                _flash_xz_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.gz":
                _flash_gz_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.bz2":
                _flash_bz2_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "qcow2":
                _flash_qcow2_from_url(plan.image.url, plan.target.path)
            else:
                raise FlashError(f"cannot flash image of format {fmt!r}")
        else:
            assert plan.image.path is not None  # typer narrows; validate_plan guarantees
            if fmt == "img":
                _flash_img(
                    plan.image.path,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.zst":
                _flash_zst(
                    plan.image.path,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.xz":
                _flash_xz(
                    plan.image.path,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.gz":
                _flash_gz(
                    plan.image.path,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
            elif fmt == "img.bz2":
                _flash_bz2(
                    plan.image.path,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                )
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


def _start_dd_progress_thread(
    proc: subprocess.Popen[str],
    progress: ProgressCallback | None,
    total_bytes: int | None,
) -> threading.Thread | None:
    """Spawn the dd-stderr pump if a progress callback is provided.

    Returns the thread (so the caller can ``.join()`` after dd exits)
    or ``None`` if no callback was given. When ``progress`` is ``None``
    the caller leaves dd's stderr inherited and dd's status=progress
    output goes to the operator's terminal as before.
    """
    if progress is None or proc.stderr is None:
        return None
    thread = threading.Thread(
        target=_pump_dd_progress,
        args=(proc.stderr, progress, total_bytes),
        daemon=True,
    )
    thread.start()
    return thread


def _flash_img(
    image: Path,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Write a raw .img to a block device with ``dd``."""
    cmd = [
        "dd",
        f"if={image}",
        f"of={target}",
        "bs=4M",
        "conv=fsync",
        "status=progress",
    ]
    stderr = subprocess.PIPE if progress is not None else None
    proc = subprocess.Popen(cmd, stderr=stderr, text=True)
    pump = _start_dd_progress_thread(proc, progress, total_bytes)
    rc = proc.wait()
    if pump is not None:
        pump.join(timeout=2)
    if rc != 0:
        raise FlashError(f"dd exited {rc} writing {image} -> {target}")


def _flash_compressed(
    image: Path,
    target: Path,
    decompress_cmd: list[str],
    decompress_name: str,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``<decompress_cmd> | dd of=TARGET ...``.

    Generic single-file-decompressor + dd pipeline used by every
    ``.img.<algo>`` writer. ``decompress_cmd`` reads the image
    (typically as a positional arg or via ``--stdout``-style flag)
    and writes raw decompressed bytes to its stdout, which dd
    consumes. ``decompress_name`` is used in error messages.

    NOTE: this only handles SINGLE-FILE compression streams (zstd,
    xz, gzip, bzip2). It does NOT handle ``.tar.gz`` /
    ``.tar.xz`` / ``.zip`` containers -- those wrap one-or-many
    files inside metadata, and dd'ing a decompressed tar stream
    would write tar headers into the target's MBR. Format
    detection in ``images.py`` deliberately rejects tarball
    extensions.
    """
    decomp_proc = subprocess.Popen(decompress_cmd, stdout=subprocess.PIPE)
    try:
        stderr = subprocess.PIPE if progress is not None else None
        dd_proc = subprocess.Popen(
            [
                "dd",
                f"of={target}",
                "bs=4M",
                "conv=fsync",
                "status=progress",
            ],
            stdin=decomp_proc.stdout,
            stderr=stderr,
            text=True,
        )
        pump = _start_dd_progress_thread(dd_proc, progress, total_bytes)
        # Let the decompressor see SIGPIPE if dd exits early.
        if decomp_proc.stdout is not None:
            decomp_proc.stdout.close()
        dd_rc = dd_proc.wait()
        if pump is not None:
            pump.join(timeout=2)
    finally:
        decomp_rc = decomp_proc.wait()

    if dd_rc != 0:
        raise FlashError(f"dd exited {dd_rc} writing {image} -> {target}")
    if decomp_rc != 0:
        raise FlashError(f"{decompress_name} exited {decomp_rc} decompressing {image}")


def _flash_zst(
    image: Path,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``zstd -d --stdout IMG | dd of=TARGET ...``."""
    _flash_compressed(
        image,
        target,
        ["zstd", "-d", "--stdout", str(image)],
        "zstd",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_xz(
    image: Path,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``xz -d --stdout IMG | dd of=TARGET ...``.

    xz decompresses at ~50-100 MB/s vs zstd's ~800-1500 MB/s;
    bty's own target images ship as .img.zst for the per-job
    CI reflash hot path, but this writer accepts operator-supplied
    .img.xz so neither format is forced on operators.
    """
    _flash_compressed(
        image,
        target,
        ["xz", "-d", "--stdout", str(image)],
        "xz",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_gz(
    image: Path,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``gzip -d --stdout IMG | dd of=TARGET ...``.

    gzip is universally available and many older distro images
    still ship as .img.gz (Raspberry Pi OS pre-2022, older
    Ubuntu Server cloud images, vendor appliance bundles).
    Decompression is fast (~300-500 MB/s) but compression ratio
    is weaker than xz/zstd on zero-heavy images.
    """
    _flash_compressed(
        image,
        target,
        ["gzip", "-d", "--stdout", str(image)],
        "gzip",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_bz2(
    image: Path,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``bzip2 -d --stdout IMG | dd of=TARGET ...``.

    bzip2 is mostly legacy at this point but appears occasionally
    in older appliance image bundles. Decompression is the
    slowest of the supported formats (~10-30 MB/s) and bz2 lacks
    a metadata header for uncompressed size, so
    ``virtual_size_bytes`` is always ``None`` for .img.bz2 and
    validate_plan skips the size-fits-target check with a note.
    """
    _flash_compressed(
        image,
        target,
        ["bzip2", "-d", "--stdout", str(image)],
        "bzip2",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_qcow2(image: Path, target: Path) -> None:
    """Write a qcow2 to a block device by converting to raw in place.

    qemu-img convert ``-p`` emits percentage-based progress to stderr
    in a different format than ``dd``; byte-level progress for qcow2
    is not yet plumbed through to the ``writing_progress`` event.
    """
    cmd = ["qemu-img", "convert", "-p", "-O", "raw", str(image), str(target)]
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        raise FlashError(f"qemu-img convert exited {rc} writing {image} -> {target}")


# ---------- URL-streaming variants -------------------------------------------
#
# curl is used as the HTTP downloader: it's the same tool the live env's
# bty-flash-on-boot service uses to fetch images, it's available on every
# Debian/Ubuntu/macOS host the project supports, and its ``--retry`` flag
# handles flaky network gracefully. The pipelines mirror the local-file
# flash functions but with curl on the front instead of an open(file).


_CURL_BASE = ("curl", "-fSL", "--retry", "3", "--retry-connrefused")


def _flash_img_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Stream a raw .img from URL straight to a block device with dd."""
    curl_proc = subprocess.Popen([*_CURL_BASE, url], stdout=subprocess.PIPE)
    try:
        stderr = subprocess.PIPE if progress is not None else None
        dd_proc = subprocess.Popen(
            ["dd", f"of={target}", "bs=4M", "conv=fsync", "status=progress"],
            stdin=curl_proc.stdout,
            stderr=stderr,
            text=True,
        )
        pump = _start_dd_progress_thread(dd_proc, progress, total_bytes)
        # Hand the read end fully to dd; closing our copy lets the kernel
        # propagate EOF / SIGPIPE correctly when one end finishes first.
        if curl_proc.stdout is not None:
            curl_proc.stdout.close()
        dd_rc = dd_proc.wait()
        if pump is not None:
            pump.join(timeout=2)
    finally:
        curl_rc = curl_proc.wait()
    if curl_rc != 0:
        raise FlashError(f"curl exited {curl_rc} fetching {url}")
    if dd_rc != 0:
        raise FlashError(f"dd exited {dd_rc} writing {url} -> {target}")


def _flash_compressed_from_url(
    url: str,
    target: Path,
    decompress_cmd: list[str],
    decompress_name: str,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``curl URL | <decompress_cmd> | dd of=TARGET ...``.

    Generic version of the URL-streaming compressed flash path used
    by every ``.img.<algo>`` URL writer. ``decompress_cmd`` reads
    from stdin (no positional file arg).

    Same single-file caveat as ``_flash_compressed``: tarballs and
    other multi-file containers must NOT be flashed through here.
    """
    curl_proc = subprocess.Popen([*_CURL_BASE, url], stdout=subprocess.PIPE)
    try:
        decomp_proc = subprocess.Popen(
            decompress_cmd,
            stdin=curl_proc.stdout,
            stdout=subprocess.PIPE,
        )
        if curl_proc.stdout is not None:
            curl_proc.stdout.close()
        try:
            stderr = subprocess.PIPE if progress is not None else None
            dd_proc = subprocess.Popen(
                ["dd", f"of={target}", "bs=4M", "conv=fsync", "status=progress"],
                stdin=decomp_proc.stdout,
                stderr=stderr,
                text=True,
            )
            pump = _start_dd_progress_thread(dd_proc, progress, total_bytes)
            if decomp_proc.stdout is not None:
                decomp_proc.stdout.close()
            dd_rc = dd_proc.wait()
            if pump is not None:
                pump.join(timeout=2)
        finally:
            decomp_rc = decomp_proc.wait()
    finally:
        curl_rc = curl_proc.wait()
    if curl_rc != 0:
        raise FlashError(f"curl exited {curl_rc} fetching {url}")
    if decomp_rc != 0:
        raise FlashError(f"{decompress_name} -d exited {decomp_rc} decompressing {url}")
    if dd_rc != 0:
        raise FlashError(f"dd exited {dd_rc} writing {url} -> {target}")


def _flash_zst_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``curl URL | zstd -d --stdout | dd of=TARGET ...``.

    Decompresses on the fly. The compressed bytes never land on
    the local filesystem; only the raw image touches the target
    disk. This is the path the TUI-on-PXE flow uses by default
    since bty's own target images ship as ``.img.zst``.
    """
    _flash_compressed_from_url(
        url,
        target,
        ["zstd", "-d", "--stdout"],
        "zstd",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_xz_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``curl URL | xz -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["xz", "-d", "--stdout"],
        "xz",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_gz_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``curl URL | gzip -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["gzip", "-d", "--stdout"],
        "gzip",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_bz2_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
) -> None:
    """Pipeline ``curl URL | bzip2 -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["bzip2", "-d", "--stdout"],
        "bzip2",
        progress=progress,
        total_bytes=total_bytes,
    )


def _flash_qcow2_from_url(url: str, target: Path) -> None:
    """Download a qcow2 to a temp file, then ``qemu-img convert`` it.

    qcow2 is random-access (the converter seeks all over the source),
    so it cannot stream. We download the whole file to a temp location
    first and reuse the existing local-qcow2 flash path.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        rc = subprocess.run(
            [*_CURL_BASE, "--output", str(tmp_path), url],
            check=False,
        ).returncode
        if rc != 0:
            raise FlashError(f"curl exited {rc} fetching {url}")
        _flash_qcow2(tmp_path, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def _sync_target(target: Path) -> None:
    """Flush kernel buffers; ``target`` accepted for symmetry with the partprobe sibling."""
    del target  # informational only at this stage
    subprocess.run(["sync"], check=False)


def _partprobe_target(target: Path) -> None:
    """Ask the kernel to re-read ``target``'s partition table, then settle udev.

    ``udevadm settle`` is run after ``partprobe`` so subsequent ``lsblk``
    queries see the new partition tree. Without it, an immediate
    follow-up (e.g. an external tool looking at partition labels)
    can race the kernel's partition scan and find no children.
    """
    subprocess.run(["partprobe", str(target)], check=False)
    subprocess.run(["udevadm", "settle"], check=False)


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
        return _parse_compressed_listing(proc.stdout, header_prefix="Frames")

    if image_format == "img.xz":
        proc = subprocess.run(
            ["xz", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        # ``xz -l`` shares the ``Compressed Uncompressed`` two-cell
        # column layout with ``zstd -l``, so the same parser works.
        # Header line for xz is ``Strms Blocks Compressed Uncompressed
        # Ratio Check Filename``.
        return _parse_compressed_listing(proc.stdout, header_prefix="Strms")

    if image_format == "img.gz":
        # ``gzip -l`` (a.k.a. ``gunzip -l``) emits unit-less byte
        # counts in two columns: ``compressed uncompressed ratio
        # name``. Note: gzip stores the uncompressed size mod 4 GiB
        # in the trailer, so for files >= 4 GiB the reported size
        # wraps and is wrong. validate_plan treats the result as a
        # best-effort hint; if wrong the size-fits-target check
        # might miss but the actual flash still proceeds correctly
        # since dd reads the real stream.
        proc = subprocess.run(
            ["gzip", "-l", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return _parse_gzip_listing(proc.stdout)

    if image_format == "img.bz2":
        # bzip2 stores no uncompressed size header. Returning None
        # tells validate_plan to skip the size-fits-target check
        # with a note; flash itself proceeds normally.
        return None

    return None


def _parse_compressed_listing(listing: str, *, header_prefix: str) -> int | None:
    """Best-effort extraction of the uncompressed size from
    ``zstd -l`` or ``xz -l`` output.

    Both tools emit a header line (``Frames Skips Compressed Uncompressed
    ...`` for zstd, ``Strms Blocks Compressed Uncompressed ...`` for xz)
    followed by a row whose 2nd ``<value> <unit>`` pair is the
    uncompressed size. ``header_prefix`` selects which header line
    to skip when scanning for the data row.
    """
    for line in listing.splitlines():
        if not line.strip() or line.lstrip().startswith((header_prefix, "-")):
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


def _parse_gzip_listing(gzip_output: str) -> int | None:
    """Best-effort uncompressed-size extraction from ``gzip -l`` output.

    Output shape (no units, just decimal bytes):

        compressed        uncompressed  ratio uncompressed_name
                73                  37 -34.4% file

    Skips the header line and any lines that don't have at least
    two integer columns. Returns the second integer column.
    Returns ``None`` if parsing fails. Note: gzip stores the
    uncompressed size mod 4 GiB in the file trailer, so for files
    >= 4 GiB this returns a wrapped (wrong) value -- the caller
    treats it as a best-effort hint, not authoritative.
    """
    for line in gzip_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("compressed", "-")):
            continue
        cells = stripped.split()
        if len(cells) < 2:
            continue
        try:
            return int(cells[1])
        except ValueError:
            continue
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
