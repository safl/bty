"""Flash plan: validate that an image can be written to a target disk.

Split into three layers so unit tests don't need to mock anything to
cover the validation logic:

- ``probe_image`` and ``probe_target`` do the I/O (reading file stats,
  shelling out to ``qemu-img info``, ``zstd -l``, ``lsblk``) and return
  plain :class:`ImageInfo` / :class:`TargetInfo` dataclasses.
- ``make_plan`` is pure: it bundles probed info into a :class:`FlashPlan`.
- ``validate_plan`` is pure: it returns a list of error strings.
- ``execute_plan`` does the destructive write (qemu-img convert /
  zstd -d / dd as appropriate for the image format). bty has no
  post-flash provisioning step -- first-boot bring-up belongs in
  the image builder (cloud-init / NoCloud); bty only writes bytes.

The ``bty`` wizard calls all four. Tests construct ``ImageInfo`` /
``TargetInfo`` directly and exercise ``make_plan`` / ``validate_plan``
without mocks. The probe and write functions have their own targeted
tests for the subprocess-shelling-out parts; integration tests against
a real loop device live in ``tests/test_flash_integration.py``.
"""

from __future__ import annotations

import contextlib
import json
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, TypeAlias

from bty import images, oras

# ``cancel`` callbacks return True to abort an in-flight flash. The
# flash code polls ~4Hz from a watchdog thread; on True it terminates
# all child subprocesses (curl + decompressor + dd) and the main
# pipeline raises :class:`FlashCancelled`.
CancelCheck: TypeAlias = Callable[[], bool]


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
      flash succeeded.
    - ``failed``            - emitted on any :class:`FlashError`;
      ``note`` carries the exception string. The exception is then
      re-raised.
    - ``subprocess_log``    - one line of stderr from an auxiliary
      pipeline subprocess (``zstd`` / ``gzip`` / ``xz`` / ``bzip2`` /
      ``curl``). ``note`` is the line, already prefixed with the
      source label (e.g. ``"zstd: ..."``). The ``bty`` wizard renders
      these above its progress widget; callers without a progress
      callback can ignore them (the subprocess's stderr is already
      inherited in that mode). Live updates that use carriage-return-
      only refresh (curl/zstd's own progress bars) don't show up
      live -- the pump reads newline-terminated lines, so only the
      final newline-terminated message lands here. That keeps the
      Rich progress bar uncluttered while still surfacing real
      errors + end-of-run stats.

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

    Either ``path`` (a local file) or ``url`` (an HTTP/HTTPS or
    ``oras://`` reference) is set; never both. URL-sourced images
    stream through curl directly to the target disk for ``.img`` /
    ``.img.{gz,zst,xz,bz2}`` (no temp file); for ``.qcow2`` they get
    downloaded to a temp file first because qcow2 is random-access.
    ``oras://`` URLs go through :mod:`bty.oras` first to resolve the
    layer digest and inject a bearer-token Authorization header into
    the curl call.
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


def probe_image_url(url: str, format_hint: str | None = None) -> ImageInfo:
    """Inspect an image at an HTTP/HTTPS or ``oras://`` URL.

    For http(s): HEAD request, format from URL path, size from
    ``Content-Length``. For ``oras://`` refs: resolve via :mod:`bty.oras`
    to a manifest layer, format inferred from the layer's title
    annotation (or ``img.gz`` default), size from the manifest's layer
    size. Virtual size (what gets written to disk) can only be
    determined for raw ``.img`` URLs from HEAD; compressed and qcow2
    URLs return ``virtual_size_bytes = None`` because computing it
    would require pulling part of the body. Validation handles
    ``None`` by skipping the size-fits-target check with a note.

    ``format_hint`` is the catalog entry's declared format
    (``CatalogEntry.format`` or ``ImageEntry.format``). When the URL
    path's filename has no recognised extension -- e.g. bty-web's
    ``/images/<sha>/<display-name>`` route where the trailing
    segment is human text without a file extension -- URL-based
    detection returns ``None`` and ``validate_plan`` rejects the
    plan with "image format not recognised". The hint lets the
    caller (which read the catalog and knows the format) supply
    it as a fallback so the probe doesn't fail just because the
    URL's decorative filename lacks an extension.

    Raises ``FileNotFoundError`` if the server doesn't respond or
    returns 4xx / 5xx for the HEAD (http) or any registry call
    (oras). Raises ``ValueError`` on an unsupported scheme.
    """
    if oras.is_oras_url(url):
        return _probe_image_url_oras(url)

    import urllib.error
    import urllib.parse
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"image URL must be http://, https://, or oras://: {url}")
    filename = Path(parsed.path).name or "image"
    fmt = images.detect_format(Path(filename))
    if fmt is None:
        # URL filename didn't carry a recognised extension. Fall
        # back to the caller-supplied hint (catalog entry's
        # ``format`` field) if any. ``validate_plan`` will still
        # reject ``None`` -> caller saw an "image format not
        # recognised" error when both fail.
        fmt = format_hint

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


def _probe_image_url_oras(url: str) -> ImageInfo:
    """Probe an ``oras://`` reference by resolving it to a manifest layer.

    Caller already verified the scheme. Format comes from the layer's
    title annotation (e.g. ``nosi-debian-sysdev-x86_64.img.gz`` ->
    ``img.gz``); falls back to ``img.gz`` if no usable title (nosi's
    publishing convention and the practical default for OCI-hosted
    disk images). Virtual size stays ``None`` -- determining it from
    a compressed blob would require pulling the whole image.
    """
    try:
        resolved = oras.resolve_ref(url)
    except oras.OrasError as exc:
        # Re-raise as FileNotFoundError so ``bty``'s existing
        # "image URL not reachable" path handles it uniformly with
        # plain HTTP failures.
        raise FileNotFoundError(f"oras ref not resolvable: {url} ({exc})") from exc
    fmt = images.detect_format(Path(resolved.title)) if resolved.title else "img.gz"
    return ImageInfo(
        path=None,
        url=url,
        format=fmt,
        size_bytes=resolved.size or 0,
        # Compressed: would need to pull (part of) the body. Caller
        # falls back to the "skip size-fits check" branch on None.
        virtual_size_bytes=None,
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


# ---------- Real write -------------------------------------------------------


class FlashError(RuntimeError):
    """Raised when a flash-related operation cannot complete.

    :class:`FlashRaceError` is a subclass for the specific case where
    the target's state changed between the last successful probe and
    the attempted write (it became mounted, stopped being a block
    device, etc.) -- ``bty`` surfaces that as exit code 5.
    """


class FlashRaceError(FlashError):
    """The target changed state between probe and write (mounted, removed, ...)."""


class FlashCancelled(FlashError):
    """Raised when the operator's ``cancel`` callback returns True.

    Distinct from :class:`FlashError` proper so callers (the
    ``bty`` wizard, tests) can branch on "operator-requested abort"
    vs "the underlying pipeline failed". Subclassing means callers
    that catch
    :class:`FlashError` still handle cancellation as a failure path
    if they don't care about the distinction.
    """


def _spawn_cancel_watchdog(
    procs: list[subprocess.Popen[Any]],
    cancel: CancelCheck | None,
    *,
    poll_interval: float = 0.25,
    terminate_grace: float = 1.0,
) -> threading.Thread | None:
    """Spawn a daemon thread that polls ``cancel()`` and kills the
    pipeline subprocesses on True.

    The watchdog exits naturally when all ``procs`` have finished
    (so it doesn't outlive a successful flash). On cancel: SIGTERM
    each live proc, give them ``terminate_grace`` seconds to drain
    cleanly, then SIGKILL anything still alive. The main pipeline
    will then see non-zero exit codes / EOF on its pipes; the caller
    re-checks ``cancel()`` after the pipeline returns and raises
    :class:`FlashCancelled` rather than :class:`FlashError`.
    """
    if cancel is None:
        return None

    def _watch() -> None:
        while True:
            if all(p.poll() is not None for p in procs):
                return  # natural completion: nothing left to kill
            if cancel():
                for p in procs:
                    if p.poll() is None:
                        with contextlib.suppress(ProcessLookupError):
                            p.terminate()
                deadline = time.monotonic() + terminate_grace
                for p in procs:
                    remaining = max(0.0, deadline - time.monotonic())
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        p.wait(timeout=remaining)
                for p in procs:
                    if p.poll() is None:
                        with contextlib.suppress(ProcessLookupError):
                            p.kill()
                return
            time.sleep(poll_interval)

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    return thread


def execute_plan(
    plan: FlashPlan,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
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

    If ``cancel`` is given (a zero-arg callable returning ``bool``), a
    watchdog thread polls it ~4Hz while a URL flash is streaming. On
    True, the pipeline's subprocesses (``curl`` / decompressor /
    ``dd``) are SIGTERM'd with a 1s grace then SIGKILL'd; the call
    then raises :class:`FlashCancelled` rather than
    :class:`FlashError`. Cancel applies only to the URL flash paths
    (where a slow remote can leave the operator waiting); the
    local-file dispatch finishes in a few seconds and isn't worth
    interrupting.

    Raises :class:`FlashError` for caller-visible failures (target no
    longer suitable, format unrecognised, write subprocess failed).
    Raises :class:`FlashCancelled` when the operator's cancel
    callback returned True.
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
                    cancel=cancel,
                )
            elif fmt == "img.zst":
                _flash_zst_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                    cancel=cancel,
                )
            elif fmt == "img.xz":
                _flash_xz_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                    cancel=cancel,
                )
            elif fmt == "img.gz":
                _flash_gz_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                    cancel=cancel,
                )
            elif fmt == "img.bz2":
                _flash_bz2_from_url(
                    plan.image.url,
                    plan.target.path,
                    progress=progress,
                    total_bytes=total_bytes,
                    cancel=cancel,
                )
            elif fmt == "qcow2":
                _flash_qcow2_from_url(plan.image.url, plan.target.path, cancel=cancel)
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


def _start_subprocess_log_pump(
    proc: subprocess.Popen[Any],
    progress: ProgressCallback | None,
    label: str,
) -> threading.Thread | None:
    """Drain ``proc.stderr`` line-by-line and emit ``subprocess_log``
    events to the progress callback.

    Used for auxiliary pipeline processes (zstd / gzip / xz / bzip2 /
    curl) when a progress callback is set (the ``bty`` wizard). The
    wizard prints each line via ``console.print`` inside its ``with
    Progress():`` context; Rich routes the line above the progress
    widget without corrupting it.

    Lines are decoded as UTF-8 with replacement. The reader is
    newline-bound, so subprocesses that update via carriage-return-
    only refresh (curl's progress bar, zstd's --no-progress=auto)
    don't emit until they finally write a ``\\n`` -- exactly what we
    want, since those refresh streams would otherwise spam the
    progress widget.

    Returns the thread (caller ``.join()``s after the proc exits) or
    ``None`` if no callback is set.
    """
    if progress is None or proc.stderr is None:
        return None

    def _pump() -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            line = line.rstrip("\r\n")
            if not line:
                continue
            _emit(progress, "subprocess_log", note=f"{label}: {line}")

    thread = threading.Thread(target=_pump, daemon=True, name=f"bty-{label}-stderr")
    thread.start()
    return thread


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
    # When a progress callback is set (``bty`` wizard caller), pipe
    # the decompressor's stderr into a pump thread that emits
    # ``subprocess_log`` events; the wizard routes those through
    # Rich's console so they print above the progress widget without
    # corrupting it. Callers without a progress callback leave stderr
    # inherited (operator's tty sees zstd/gzip output natively).
    decomp_stderr = subprocess.PIPE if progress is not None else None
    decomp_proc = subprocess.Popen(decompress_cmd, stdout=subprocess.PIPE, stderr=decomp_stderr)
    decomp_log_pump = _start_subprocess_log_pump(decomp_proc, progress, decompress_name)
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
        if decomp_log_pump is not None:
            decomp_log_pump.join(timeout=2)

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

    Decompression is the slowest of the supported formats
    (~10-30 MB/s) and bz2 lacks a metadata header for uncompressed
    size, so ``virtual_size_bytes`` is always ``None`` for .img.bz2 and
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
# curl is the HTTP downloader: available on every Debian/Ubuntu/macOS
# host the project supports, and well-instrumented for progress
# reporting via ``--progress-bar`` to stderr. The pipelines mirror
# the local-file flash functions but with curl on the front instead
# of an open(file).


# ``-fsSL``:
#   -f: fail on HTTP errors (4xx/5xx exit non-zero)
#   -s: silent (no progress meter, no diagnostic notes)
#   -S: but still show errors (without this, -s would also silence them)
#   -L: follow redirects
# The ``-s`` is deliberate: curl's progress meter is carriage-return-
# updated, which ``bty``'s newline-bound subprocess-log pump can
# only capture as the *initial* zero-state line (followed by silence
# as the same line gets overwritten in place). Operators saw "all 0"
# rows above the Rich progress bar; ``-s`` silences that, ``-S``
# keeps real error lines flowing through.
#
# NO ``--retry``: every curl invocation here streams into a running
# ``dd`` pipeline. If curl retries on a transient network failure,
# it re-fetches from byte 0; those bytes get written to disk a
# SECOND time, corrupting whatever was already there. Symptom
# observed on a Supermicro BMC flash: the Rich progress bar
# repeatedly hit 100% then "reset" as dd kept writing past the
# image's compressed-size total. For streaming-to-dd the right
# behaviour is fail-fast -- the operator gets a clean error and
# can re-flash from scratch instead of seeing a silently-corrupted
# target. ``--retry`` would only make sense if we also passed
# ``--continue-at`` and made dd resumable, which is a much bigger
# refactor for a much rarer win.
_CURL_BASE = ("curl", "-fsSL")


def _curl_args_for_source(url: str) -> tuple[list[str], int | None]:
    """Build curl arguments for a fetch source.

    Plain http(s) URLs pass through unchanged. ``oras://`` references
    go through :mod:`bty.oras` to resolve the manifest layer, and the
    resulting bearer token is injected as a ``-H Authorization``
    header on the curl call. Returns ``(argv, expected_size_or_None)``
    -- the size is the manifest's declared layer size when known, so
    callers can use it as a fallback ``total_bytes`` when HEAD wasn't
    run beforehand.
    """
    if not oras.is_oras_url(url):
        return [*_CURL_BASE, url], None
    resolved = oras.resolve_ref(url)
    args = [*_CURL_BASE]
    for header_name, header_value in resolved.headers.items():
        args.extend(["-H", f"{header_name}: {header_value}"])
    args.append(resolved.blob_url)
    return args, resolved.size


def _flash_img_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
    cancel: CancelCheck | None = None,
) -> None:
    """Stream a raw .img from URL straight to a block device with dd."""
    curl_args, resolved_size = _curl_args_for_source(url)
    if total_bytes is None:
        total_bytes = resolved_size
    # Pipe curl's stderr through the subprocess-log pump so ``bty``
    # can surface curl's lines (errors + final status) above its
    # progress widget. curl's live progress bar uses ``\r``-only
    # refresh which the newline-bound pump intentionally skips; the
    # operator sees errors + the end-of-run line, not the noisy
    # real-time bar.
    curl_stderr = subprocess.PIPE if progress is not None else None
    curl_proc = subprocess.Popen(curl_args, stdout=subprocess.PIPE, stderr=curl_stderr)
    curl_log_pump = _start_subprocess_log_pump(curl_proc, progress, "curl")
    try:
        stderr = subprocess.PIPE if progress is not None else None
        dd_proc = subprocess.Popen(
            ["dd", f"of={target}", "bs=4M", "conv=fsync", "status=progress"],
            stdin=curl_proc.stdout,
            stderr=stderr,
            text=True,
        )
        watchdog = _spawn_cancel_watchdog([curl_proc, dd_proc], cancel)
        pump = _start_dd_progress_thread(dd_proc, progress, total_bytes)
        # Hand the read end fully to dd; closing our copy lets the kernel
        # propagate EOF / SIGPIPE correctly when one end finishes first.
        if curl_proc.stdout is not None:
            curl_proc.stdout.close()
        dd_rc = dd_proc.wait()
        if pump is not None:
            pump.join(timeout=2)
        if watchdog is not None:
            watchdog.join(timeout=2)
    finally:
        curl_rc = curl_proc.wait()
        if curl_log_pump is not None:
            curl_log_pump.join(timeout=2)
    # Cancel takes precedence over non-zero exit codes: SIGTERM
    # leaves curl/dd with nonzero status which would otherwise be
    # mis-reported as a transport failure.
    if cancel is not None and cancel():
        raise FlashCancelled("flash cancelled by operator")
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
    cancel: CancelCheck | None = None,
) -> None:
    """Pipeline ``curl URL | <decompress_cmd> | dd of=TARGET ...``.

    Generic version of the URL-streaming compressed flash path used
    by every ``.img.<algo>`` URL writer. ``decompress_cmd`` reads
    from stdin (no positional file arg).

    Same single-file caveat as ``_flash_compressed``: tarballs and
    other multi-file containers must NOT be flashed through here.

    Progress denominator note: ``dd`` reports OUTPUT bytes (the
    decompressed bytes written to the target disk), but the upstream
    compressed blob's size is generally smaller -- often dramatically
    so for a sparse raw image. We pass ``total_bytes`` through
    unchanged: the caller (``probe_image_url`` -> ``ImageInfo
    .virtual_size_bytes``) supplies the decompressed size when it
    can derive it, and ``None`` otherwise. We deliberately do NOT
    fall back to ``_curl_args_for_source``'s ``resolved_size``
    (the compressed blob size) here -- that mismatch makes the
    progress bar overshoot to ~6x for highly compressible .img.gz
    inputs.
    """
    curl_args, _resolved_compressed_size = _curl_args_for_source(url)
    # Pipe both curl + decompressor stderr through subprocess-log
    # pumps. The ``bty`` wizard prints each line above its progress
    # widget via Rich's ``console.print`` (which Rich routes around
    # the live display). Newline-bound reads mean curl's/zstd's CR-only
    # real-time refresh doesn't fire; only meaningful lines do.
    pipeline_stderr = subprocess.PIPE if progress is not None else None
    curl_proc = subprocess.Popen(curl_args, stdout=subprocess.PIPE, stderr=pipeline_stderr)
    curl_log_pump = _start_subprocess_log_pump(curl_proc, progress, "curl")
    try:
        decomp_proc = subprocess.Popen(
            decompress_cmd,
            stdin=curl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=pipeline_stderr,
        )
        decomp_log_pump = _start_subprocess_log_pump(decomp_proc, progress, decompress_name)
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
            watchdog = _spawn_cancel_watchdog([curl_proc, decomp_proc, dd_proc], cancel)
            pump = _start_dd_progress_thread(dd_proc, progress, total_bytes)
            if decomp_proc.stdout is not None:
                decomp_proc.stdout.close()
            dd_rc = dd_proc.wait()
            if pump is not None:
                pump.join(timeout=2)
            if watchdog is not None:
                watchdog.join(timeout=2)
        finally:
            decomp_rc = decomp_proc.wait()
            if decomp_log_pump is not None:
                decomp_log_pump.join(timeout=2)
    finally:
        curl_rc = curl_proc.wait()
        if curl_log_pump is not None:
            curl_log_pump.join(timeout=2)
    # Cancel takes precedence over non-zero exit codes: the SIGTERM
    # the watchdog sends leaves all three subprocesses with nonzero
    # status, which would otherwise be misread as a transport /
    # decode failure.
    if cancel is not None and cancel():
        raise FlashCancelled("flash cancelled by operator")
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
    cancel: CancelCheck | None = None,
) -> None:
    """Pipeline ``curl URL | zstd -d --stdout | dd of=TARGET ...``.

    Decompresses on the fly. The compressed bytes never land on
    the local filesystem; only the raw image touches the target
    disk. This is the path the PXE-driven ``bty`` flow uses by
    default since bty's own target images ship as ``.img.zst``.
    """
    _flash_compressed_from_url(
        url,
        target,
        ["zstd", "-d", "--stdout"],
        "zstd",
        progress=progress,
        total_bytes=total_bytes,
        cancel=cancel,
    )


def _flash_xz_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
    cancel: CancelCheck | None = None,
) -> None:
    """Pipeline ``curl URL | xz -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["xz", "-d", "--stdout"],
        "xz",
        progress=progress,
        total_bytes=total_bytes,
        cancel=cancel,
    )


def _flash_gz_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
    cancel: CancelCheck | None = None,
) -> None:
    """Pipeline ``curl URL | gzip -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["gzip", "-d", "--stdout"],
        "gzip",
        progress=progress,
        total_bytes=total_bytes,
        cancel=cancel,
    )


def _flash_bz2_from_url(
    url: str,
    target: Path,
    *,
    progress: ProgressCallback | None = None,
    total_bytes: int | None = None,
    cancel: CancelCheck | None = None,
) -> None:
    """Pipeline ``curl URL | bzip2 -d --stdout | dd of=TARGET ...``."""
    _flash_compressed_from_url(
        url,
        target,
        ["bzip2", "-d", "--stdout"],
        "bzip2",
        progress=progress,
        total_bytes=total_bytes,
        cancel=cancel,
    )


def _flash_qcow2_from_url(url: str, target: Path, *, cancel: CancelCheck | None = None) -> None:
    """Download a qcow2 to a temp file, then ``qemu-img convert`` it.

    qcow2 is random-access (the converter seeks all over the source),
    so it cannot stream. We download the whole file to a temp location
    first and reuse the existing local-qcow2 flash path.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        curl_args, _ = _curl_args_for_source(url)
        curl_argv = [*curl_args[:-1], "--output", str(tmp_path), curl_args[-1]]
        # Popen + watchdog (rather than subprocess.run) so the cancel
        # callback can terminate the download mid-stream. qcow2 can't
        # stream-flash, so the download phase is the bulk of the wall
        # time; cancelling there is what matters most for the
        # operator experience.
        curl_proc = subprocess.Popen(curl_argv)
        watchdog = _spawn_cancel_watchdog([curl_proc], cancel)
        rc = curl_proc.wait()
        if watchdog is not None:
            watchdog.join(timeout=2)
        if cancel is not None and cancel():
            raise FlashCancelled("flash cancelled by operator")
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


def _probe_run(cmd: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str] | None:
    """Run a bounded metadata-probe command; return the completed
    process, or ``None`` if it timed out.

    The size / mountpoint probes here run during ``validate_plan``;
    bounding them keeps a stuck IO subsystem (failing disk, slow
    network mount, corrupt image) from wedging the pre-flash check.
    Callers already treat a failed probe as "unknown" (returning
    ``None`` / ``[]``), so a timeout folds cleanly into that best-
    effort contract. Mirrors the defensive timeouts in
    :mod:`bty.disks` and :func:`bty.images.inspect_image`.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _image_virtual_size(path: Path, image_format: str | None) -> int | None:
    """Return the byte count an image would expand to on disk."""
    if image_format == "img":
        return path.stat().st_size

    if image_format == "qcow2":
        proc = _probe_run(["qemu-img", "info", "--output=json", str(path)])
        if proc is None or proc.returncode != 0:
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        size = payload.get("virtual-size")
        return size if isinstance(size, int) else None

    if image_format == "img.zst":
        proc = _probe_run(["zstd", "-l", "--no-progress", str(path)])
        if proc is None or proc.returncode != 0:
            return None
        return _parse_compressed_listing(proc.stdout, header_prefix="Frames")

    if image_format == "img.xz":
        proc = _probe_run(["xz", "-l", str(path)])
        if proc is None or proc.returncode != 0:
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
        proc = _probe_run(["gzip", "-l", str(path)])
        if proc is None or proc.returncode != 0:
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
    proc = _probe_run(["lsblk", "-bndo", "SIZE", str(target)])
    if proc is None or proc.returncode != 0:
        return None
    stdout = proc.stdout.strip()
    line = stdout.splitlines()[0] if stdout else ""
    try:
        return int(line)
    except ValueError:
        return None


def _lsblk_target_mountpoints(target: Path) -> list[str]:
    """Return all mountpoints used by ``target`` and its partitions."""
    proc = _probe_run(["lsblk", "-no", "MOUNTPOINTS", str(target)])
    if proc is None or proc.returncode != 0:
        return []
    return [mp for raw in proc.stdout.splitlines() if (mp := raw.strip())]
