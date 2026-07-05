"""Standalone helpers for :mod:`bty.web._app`.

Small utility functions that don't touch the FastAPI app state
directly. Extracted from ``_app.py`` so the app factory stays a
focused wiring layer; the pieces here are pure enough to import
from tests and from :mod:`bty.web._ui` without pulling in the
whole app graph.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse

from bty.web import _db, _models, _security


def row_to_machine(row: sqlite3.Row, labels: list[str]) -> _models.Machine:
    """Decode a sqlite3.Row into a ``_models.Machine``.

    ``known_disks`` is stored as a JSON string in the column;
    deserialise it lazily here so callers don't have to juggle the
    text/list distinction. A None or unparseable column means "no
    inventory yet"; missing fields don't crash the model.

    ``labels`` is sourced from the ``machine_labels`` side-table by
    the caller; it's plumbed in rather than fetched here so the
    list endpoint can read them in one batch (a JOIN) instead of
    N+1 queries.
    """
    raw_disks = row["known_disks"]
    parsed_disks: list[dict[str, object]] | None = None
    if raw_disks:
        try:
            decoded = json.loads(raw_disks)
            if isinstance(decoded, list):
                parsed_disks = decoded
        except (TypeError, ValueError):
            # Stale / malformed JSON in the column shouldn't crash
            # the listing endpoint; surface as "no inventory" and
            # the next /pxe/{mac}/inventory post replaces it cleanly.
            parsed_disks = None
    return _models.Machine(
        mac=row["mac"],
        bty_image_ref=row["bty_image_ref"],
        labels=labels,
        discovered_at=iso_or_none(row["discovered_at"]),
        last_seen_at=iso_or_none(row["last_seen_at"]),
        last_seen_ip=row["last_seen_ip"],
        boot_mode=row["boot_mode"],
        sanboot_drive=_db.row_value(row, "sanboot_drive"),
        last_flashed_at=iso_or_none(row["last_flashed_at"]),
        known_disks=parsed_disks,
        known_disks_at=iso_or_none(row["known_disks_at"]),
        target_disk_serial=row["target_disk_serial"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def iso_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def head_content_length(url: str, *, timeout: float = 10.0) -> int | None:
    """HEAD ``url`` and return the upstream ``Content-Length`` if
    the server provided one, else ``None``. Best-effort: any
    network error returns ``None`` rather than raising -- the
    operator's catalog-add doesn't fail if the upstream doesn't
    support HEAD or the network is flaky."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl is not None else None
    except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError, OSError):
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def request_host(request: Request) -> str:
    """Return the ``host:port`` the client used to reach this server.

    Prefers the ``Host`` header (what the client actually typed in the
    URL bar); falls back to the parsed request URL when the header is
    missing -- bare TestClient and tightly-curated reverse proxies can
    omit it. Default port mirrors the server's listen port.

    If both the Host header AND ``request.url.hostname`` are unset
    (synthetic Request constructed without scope, rare), returns a
    plausible loopback host instead of a string with ``"None"`` in
    it. The iPXE flash chain interpolates this value into a
    ``set bty-base http://{host}`` line, so a broken host would
    break the live env's HTTP fetches.
    """
    header_host = request.headers.get("host")
    if header_host:
        return header_host
    hostname = request.url.hostname or "127.0.0.1"
    port = request.url.port or 8080
    return f"{hostname}:{port}"


def seed_boot_dir(boot_root: Path) -> None:
    """Seed ``boot_root`` with baked bootstrap artifacts on startup.

    The container image bakes bty's custom iPXE binary (the one whose
    embedded script chains to ``/pxe-bootstrap.ipxe``, so the operator's
    DHCP only needs a single bootfile) under ``$BTY_BOOT_SEED_DIR``. Copy
    any file from there into ``boot_root`` when it isn't already present,
    so UEFI HTTP-Boot clients can fetch ``GET /boot/ipxe.efi`` out of the
    box.

    A no-op when ``BTY_BOOT_SEED_DIR`` is unset (host / dev installs) or
    its directory is absent. Existing files are never overwritten, so an
    operator-placed bootfile always wins.
    """
    import logging as _logging

    seed_dir = os.environ.get("BTY_BOOT_SEED_DIR")
    if not seed_dir:
        return
    src = Path(seed_dir)
    if not src.is_dir():
        return
    boot_root.mkdir(parents=True, exist_ok=True)
    seed_log = _logging.getLogger(__name__)
    for item in sorted(src.iterdir()):
        # Skip dotfiles so a ``.gitkeep`` placeholder in an
        # otherwise-empty seed dir (dev builds) isn't published.
        if item.name.startswith(".") or not item.is_file():
            continue
        dst = boot_root / item.name
        if dst.exists():
            continue
        try:
            shutil.copy2(item, dst)
            seed_log.info("seeded boot artifact %s into %s", item.name, boot_root)
        except OSError as exc:
            seed_log.warning("could not seed boot artifact %s: %s", item.name, exc)


def safe_path(root: Path, name: str) -> Path:
    """Resolve ``root / name`` with path-traversal checks, return the path.

    Rejects names with slashes, ``..``, NULs, etc. Caller decides
    what to do with the resolved path (404 vs. open-for-write).
    """
    # Single-source the "is this a bare basename?" rule via _security;
    # keep this endpoint's own wording so the message stays stable.
    try:
        _security.validate_basename(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid name {name!r}: must be a bare filename "
            "(no '/', '\\', '..', or NUL bytes)",
        ) from exc
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid name {name!r}: resolves outside the allowed directory",
        ) from exc
    return candidate


def serve_safe_file(root: Path, name: str) -> FileResponse:
    """Return a FileResponse for ``root / name`` after path-traversal checks."""
    candidate = safe_path(root, name)
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no such file: {name}")
    return FileResponse(candidate, filename=name)


# Default max upload-body size (200 GiB). Generous for plausible
# real OS images (decompressed Windows is the largest target at
# ~50 GiB; everything Linux-y fits in single-digit GB) but caps
# the worst case at "the disk fills up before bty-web does
# anything useful". Operators can raise via ``BTY_TUNING_MAX_UPLOAD_BYTES``
# if they have a legitimate use case for bigger images.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024 * 1024

# Hard cap for ``/ui/catalog/upload``. A catalog.toml is plain TOML
# (typically a few KB; a fleet manifest with hundreds of entries
# stays well under 100 KB). 1 MiB is generous enough to never block
# a legitimate manifest and small enough to reject the "wrong form
# target" case (operator dropped an ISO / image into the catalog
# form by mistake) before parsing it as text.
CATALOG_UPLOAD_MAX_BYTES = 1 * 1024 * 1024


def max_upload_bytes() -> int:
    """Resolve the upload size cap from ``[tuning] max_upload_bytes``
    (env override ``BTY_TUNING_MAX_UPLOAD_BYTES``) or the schema
    default. Non-positive values clamp to the default -- a
    pathological ``0`` would otherwise reject every upload."""
    from bty.web._config import cfg as _cfg

    value = _cfg().tuning.max_upload_bytes
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


async def stream_upload(request: Request, root: Path, name: str) -> dict[str, object]:
    """Stream the request body to ``root / name`` and return basic metadata.

    Atomic via a sibling ``.partial`` file + rename so a torn upload
    can't leave a half-written image where a previous good copy used
    to be. The destination directory is created if it doesn't exist
    (server image's first-boot init creates it for the prod paths,
    but tests pass tmp_path and we want this to work without an
    init step).

    On any exception during the stream (client disconnect, write
    failure, etc.), the ``.partial`` file is unlinked so it cannot
    pollute future ``list_images`` / hash auto-import passes. The
    only path that survives is the success path: rename ``.partial``
    -> final name.

    Caps the body at :data:`DEFAULT_MAX_UPLOAD_BYTES` (200 GiB by
    default; ``BTY_TUNING_MAX_UPLOAD_BYTES`` overrides). A runaway script
    or hostile request that streams forever otherwise fills the
    image-root partition; the cap kills the upload + unlinks the
    partial well before that. The partial is also unlinked
    upfront so a prior aborted upload doesn't leak.
    """
    candidate = safe_path(root, name)
    root.mkdir(parents=True, exist_ok=True)
    partial = candidate.with_suffix(candidate.suffix + ".partial")
    max_bytes = max_upload_bytes()
    size = 0
    try:
        with partial.open("wb") as fh:
            async for chunk in request.stream():
                if chunk:
                    fh.write(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                f"upload exceeded {max_bytes} bytes "
                                f"(BTY_TUNING_MAX_UPLOAD_BYTES). Aborted at {size} bytes."
                            ),
                        )
        partial.replace(candidate)
    except BaseException:
        # ``BaseException`` so an asyncio.CancelledError (client
        # dropped the connection) also triggers cleanup. The
        # ``with contextlib.suppress(FileNotFoundError)`` covers
        # the rare case where the .partial was never created
        # (mkdir succeeded but open() failed before any write).
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise
    return {"name": name, "size_bytes": size, "path": str(candidate)}
