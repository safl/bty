"""bty-web ramboot pre-warm worker.

Per-image pre-warm pipeline for ``boot_mode=ramboot``. When the
operator binds a machine to ramboot, this module's worker fetches
the bound catalog entry's bytes (through withcache when configured),
decompresses to ``<live_images_dir>/<ref>.img``, and POSTs the
result to the configured nbdmux daemon as a named export. The
ramboot iPXE chain then connects the target's initramfs to that
export over NBD; overlayfs on tmpfs takes care of writes.

State machine, persisted in ``ramboot_cache``:

  queued -> fetching -> decompressing -> registering -> ready

A failure in any step lands ``status=failed`` with the error
message and unblocks re-enqueue. The worker is single-threaded
deliberately: only one decompress runs at a time so a fleet of
ramboot machines bound to the same image converges on one file
on disk rather than racing through duplicate decompresses. The
catalog of ramboot-bound images is typically tiny (one per
operator workflow), and serial pre-warm matches how
DownloadManager handles the equivalent flash-side bytes path.

Idempotence: re-enqueuing a ref that's already ``ready`` is a
no-op (the row stays as-is; the existing nbdmux export keeps
serving). Re-enqueuing a ``failed`` ref restarts at ``queued``.

Audit events emitted (see ``_events_log.ALLOWED_KINDS``):
``ramboot.pre_warm.requested`` / ``.started`` / ``.completed`` /
``.failed``.
"""

from __future__ import annotations

import gzip
import logging
import queue
import shutil
import sqlite3
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bty.web import _db, _settings_store
from bty.web._events_log import record as _log_event

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RambootCacheRow:
    """In-process view of a ``ramboot_cache`` row.

    The DB row stays the source of truth; this is just a typed read
    helper for callers that don't want to touch ``sqlite3.Row``
    directly.
    """

    ref: str
    status: str
    image_path: str | None
    export_name: str | None
    decompressed_size: int | None
    error: str | None
    enqueued_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RambootCacheRow:
        return cls(
            ref=str(row["ref"]),
            status=str(row["status"]),
            image_path=row["image_path"],
            export_name=row["export_name"],
            decompressed_size=row["decompressed_size"],
            error=row["error"],
            enqueued_at=str(row["enqueued_at"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=str(row["updated_at"]),
        )


def get_row(conn: sqlite3.Connection, ref: str) -> RambootCacheRow | None:
    row = conn.execute("SELECT * FROM ramboot_cache WHERE ref = ?", (ref,)).fetchone()
    return RambootCacheRow.from_row(row) if row is not None else None


def list_rows(conn: sqlite3.Connection) -> list[RambootCacheRow]:
    rows = conn.execute("SELECT * FROM ramboot_cache ORDER BY enqueued_at DESC").fetchall()
    return [RambootCacheRow.from_row(r) for r in rows]


def is_ready(conn: sqlite3.Connection, ref: str) -> bool:
    """Convenience for the iPXE/plan gates: is the bytes path
    actually live? Cheaper than parsing the whole row when callers
    only need the boolean."""
    row = conn.execute("SELECT status FROM ramboot_cache WHERE ref = ?", (ref,)).fetchone()
    return row is not None and str(row["status"]) == "ready"


def statuses_by_ref(conn: sqlite3.Connection) -> dict[str, str]:
    """Map every known ramboot_cache ref to its current status.
    Used by the machine listing renderer to surface per-row
    pre-warm progress without one query per row."""
    return {
        str(row["ref"]): str(row["status"])
        for row in conn.execute("SELECT ref, status FROM ramboot_cache").fetchall()
    }


def _set_status(
    conn: sqlite3.Connection,
    ref: str,
    status: str,
    *,
    error: str | None = None,
    image_path: str | None = None,
    export_name: str | None = None,
    decompressed_size: int | None = None,
    set_started: bool = False,
    set_completed: bool = False,
) -> None:
    """Atomic update of the row's status + optional companion fields.

    Caller owns the transaction. Companion fields are folded into
    one UPDATE so the row's ``updated_at`` matches the status
    transition (callers reading status + image_path see a
    consistent snapshot).
    """
    now = _now_iso()
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now]
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if image_path is not None:
        sets.append("image_path = ?")
        params.append(image_path)
    if export_name is not None:
        sets.append("export_name = ?")
        params.append(export_name)
    if decompressed_size is not None:
        sets.append("decompressed_size = ?")
        params.append(decompressed_size)
    if set_started:
        sets.append("started_at = ?")
        params.append(now)
    if set_completed:
        sets.append("completed_at = ?")
        params.append(now)
    params.append(ref)
    conn.execute(
        f"UPDATE ramboot_cache SET {', '.join(sets)} WHERE ref = ?",
        params,
    )


def enqueue(
    conn: sqlite3.Connection,
    ref: str,
    *,
    actor: str = "system",
    source_ip: str | None = None,
) -> RambootCacheRow:
    """Insert (or refresh) a ``queued`` row for ``ref``. Audit-logs
    a ``ramboot.pre_warm.requested`` event. Idempotent for
    ``status=ready`` rows: the existing row is returned untouched
    so a no-op enqueue from a machine-edit save doesn't bounce
    a serving export.

    Caller owns the transaction.
    """
    existing = get_row(conn, ref)
    if existing is not None and existing.status == "ready":
        return existing
    now = _now_iso()
    if existing is None:
        conn.execute(
            "INSERT INTO ramboot_cache "
            "(ref, status, enqueued_at, updated_at) "
            "VALUES (?, 'queued', ?, ?)",
            (ref, now, now),
        )
    else:
        # Re-queue a stuck / failed row. Reset status + clear
        # transient fields so the worker starts from ``queued``.
        conn.execute(
            "UPDATE ramboot_cache SET "
            "status = 'queued', error = NULL, "
            "started_at = NULL, completed_at = NULL, "
            "enqueued_at = ?, updated_at = ? "
            "WHERE ref = ?",
            (now, now, ref),
        )
    _log_event(
        conn,
        kind="ramboot.pre_warm.requested",
        summary=f"ramboot pre-warm requested for ref={ref[:8]}...",
        subject_kind="ramboot_cache",
        subject_id=ref,
        actor=actor,
        source_ip=source_ip,
        details={"ref": ref},
    )
    row = get_row(conn, ref)
    assert row is not None
    return row


def _fetch_source_url(conn: sqlite3.Connection, ref: str) -> tuple[str, str]:
    """Resolve the bytes URL + the decompressed file basename for
    ``ref``. Reads the catalog entry's ``src`` (preferred; the
    canonical URL for withcache routing) and falls back to
    ``resolved_src`` for entries that don't carry one. Raises
    ``ValueError`` if the ref isn't bound to a catalog entry.

    Returns ``(fetch_url, dest_basename)`` where ``fetch_url`` is
    rewritten through withcache when one is configured, and
    ``dest_basename`` is the unsuffixed name used for the
    decompressed image (``<ref>.img``). Same base name is used as
    the nbdmux export name so the operator's audit trail keys on
    the ref everywhere.
    """
    row = conn.execute(
        "SELECT src, resolved_src, format FROM catalog_entries WHERE bty_image_ref = ?",
        (ref,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no catalog entry bound to ref={ref}")
    src = (row["src"] or row["resolved_src"] or "").strip()
    if not src:
        raise ValueError(f"catalog entry for ref={ref} has neither src nor resolved_src")
    # Route through withcache when configured. The withcache.oras
    # path handles oras:// scheme automatically; HTTP URLs are
    # wrapped as ``{withcache}/b/{b64(src)}/...`` by the
    # cache-host. Re-use the same helper the flash path uses.
    withcache_url = _settings_store.resolve_withcache_url(conn)
    if withcache_url:
        from bty.web._withcache import blob_url

        fetch_url = blob_url(withcache_url, src) or src
    else:
        fetch_url = src
    return fetch_url, f"{ref}.img"


def _decompress_to(image_path: Path, body_stream: Iterator[bytes], format_hint: str) -> int:
    """Stream ``body_stream`` through the format-appropriate
    decompressor into ``image_path``. Returns the number of
    decompressed bytes written.

    Format hint comes from the catalog entry; we accept the values
    bty.images supports: ``img`` (no decompression), ``img.gz``,
    ``img.zst``. Anything else raises.
    """
    image_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = image_path.with_suffix(image_path.suffix + ".inflight")
    written = 0
    if format_hint == "img":
        with tmp.open("wb") as out:
            for chunk in body_stream:
                out.write(chunk)
                written += len(chunk)
    elif format_hint in ("img.gz", "gz"):
        # Stream gzip-decompress without staging the .gz on disk first.
        # ``gzip.decompress`` doesn't help (whole-buffer); ``GzipFile``
        # takes a file-like, so wrap the iterator in an ``io.RawIOBase``
        # subclass to satisfy the type-system + the readinto protocol.
        import io

        class _IterReader(io.RawIOBase):
            def __init__(self, it: Iterator[bytes]) -> None:
                self._it = it
                self._buf = bytearray()

            def readable(self) -> bool:
                return True

            def readinto(self, b: Any) -> int:
                while not self._buf:
                    try:
                        self._buf.extend(next(self._it))
                    except StopIteration:
                        return 0
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                del self._buf[:n]
                return n

        with (
            tmp.open("wb") as out,
            gzip.GzipFile(fileobj=_IterReader(body_stream), mode="rb") as gz,
        ):
            shutil.copyfileobj(gz, out)
            written = out.tell()
    elif format_hint in ("img.zst", "zst"):
        # zstd needs the third-party ``zstandard`` package or
        # similar; the bty flash path uses ``zstd``-binary as a
        # subprocess. Mirror that pattern: stream into a
        # subprocess that writes the inflated bytes back to us.
        import subprocess

        with tmp.open("wb") as out:
            proc = subprocess.Popen(
                ["zstd", "-d", "-c"],
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None
            for chunk in body_stream:
                proc.stdin.write(chunk)
            proc.stdin.close()
            _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"zstd decompression failed (rc={proc.returncode}): "
                    f"{err.decode(errors='replace')}"
                )
            written = out.tell()
    else:
        raise ValueError(f"unsupported format for ramboot pre-warm: {format_hint!r}")
    tmp.replace(image_path)
    return written


def _stream_body(url: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield the response body in chunks. Uses urllib so the worker
    stays stdlib-only; httpx would add an async dependency for the
    same wire format."""
    with urllib.request.urlopen(url) as resp:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _register_with_nbdmux(nbdmux_url: str, ref: str, image_path: Path) -> None:
    """POST the decompressed image to nbdmux as a named export.
    Re-raises any client error so the caller can mark the row
    ``failed`` with the upstream message in ``error``."""
    from nbdmux.client import add_export

    add_export(name=ref, file=str(image_path), readonly=True, server=nbdmux_url)


@dataclass
class _Job:
    ref: str


class RambootCacheManager:
    """Single-thread pre-warm worker. One instance per bty-web
    process; spawned by the FastAPI startup hook in
    :mod:`bty.web._app`.
    """

    def __init__(self, state_path: Path, live_images_dir: Path) -> None:
        self._state_path = state_path
        self._live_images_dir = live_images_dir
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ramboot-cache-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        # Unblock the queue.get() in the worker loop.
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None

    def enqueue(self, ref: str) -> None:
        """Drop ``ref`` onto the work queue. The DB row should
        already be in ``status=queued``; the caller (typically
        the machine-upsert handler) is responsible for the
        :func:`enqueue` DB call that creates / refreshes the row
        plus the audit event."""
        self._queue.put(_Job(ref=ref))

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                # Sentinel; either a stop() call or a spurious put.
                if self._stop.is_set():
                    return
                continue
            try:
                self._process(job.ref)
            except Exception:
                _log.exception("ramboot pre-warm worker: unhandled error on ref=%s", job.ref)

    def _process(self, ref: str) -> None:
        nbdmux_url: str | None
        format_hint: str
        fetch_url: str
        dest_basename: str
        with _db.open_db(self._state_path) as conn:
            row = get_row(conn, ref)
            if row is None:
                # Caller deleted the row between enqueue and us;
                # nothing to do.
                _log.info("ramboot pre-warm: ref=%s gone before worker picked up", ref)
                return
            if row.status not in ("queued", "failed"):
                # Idempotent re-process is harmless but unusual;
                # log and bail rather than double-fetch.
                _log.info(
                    "ramboot pre-warm: ref=%s status=%s, skipping (not queued)",
                    ref,
                    row.status,
                )
                return
            nbdmux_url = _settings_store.resolve_nbdmux_url(conn)
            if not nbdmux_url:
                _set_status(
                    conn,
                    ref,
                    "failed",
                    error="nbdmux URL not configured",
                    set_started=True,
                    set_completed=True,
                )
                _log_event(
                    conn,
                    kind="ramboot.pre_warm.failed",
                    summary=(
                        f"ramboot pre-warm failed for ref={ref[:8]}...: nbdmux URL not configured"
                    ),
                    subject_kind="ramboot_cache",
                    subject_id=ref,
                    actor="ramboot-worker",
                    details={"ref": ref, "reason": "nbdmux_unset"},
                )
                conn.commit()
                return
            try:
                fetch_url, dest_basename = _fetch_source_url(conn, ref)
            except ValueError as exc:
                _set_status(
                    conn,
                    ref,
                    "failed",
                    error=str(exc),
                    set_started=True,
                    set_completed=True,
                )
                _log_event(
                    conn,
                    kind="ramboot.pre_warm.failed",
                    summary=f"ramboot pre-warm failed for ref={ref[:8]}...: {exc}",
                    subject_kind="ramboot_cache",
                    subject_id=ref,
                    actor="ramboot-worker",
                    details={"ref": ref, "reason": "no_catalog_entry"},
                )
                conn.commit()
                return
            entry = conn.execute(
                "SELECT format FROM catalog_entries WHERE bty_image_ref = ?",
                (ref,),
            ).fetchone()
            format_hint = str(entry["format"]) if entry and entry["format"] else "img"
            _set_status(conn, ref, "fetching", set_started=True)
            _log_event(
                conn,
                kind="ramboot.pre_warm.started",
                summary=f"ramboot pre-warm started for ref={ref[:8]}... (format={format_hint})",
                subject_kind="ramboot_cache",
                subject_id=ref,
                actor="ramboot-worker",
                details={"ref": ref, "format": format_hint, "fetch_url": fetch_url},
            )
            conn.commit()

        image_path = self._live_images_dir / dest_basename
        try:
            with _db.open_db(self._state_path) as conn:
                _set_status(conn, ref, "decompressing")
                conn.commit()
            written = _decompress_to(image_path, _stream_body(fetch_url), format_hint)
            with _db.open_db(self._state_path) as conn:
                _set_status(
                    conn,
                    ref,
                    "registering",
                    image_path=str(image_path),
                    decompressed_size=written,
                )
                conn.commit()
            _register_with_nbdmux(nbdmux_url, ref, image_path)
            with _db.open_db(self._state_path) as conn:
                _set_status(
                    conn,
                    ref,
                    "ready",
                    export_name=ref,
                    set_completed=True,
                )
                _log_event(
                    conn,
                    kind="ramboot.pre_warm.completed",
                    summary=(
                        f"ramboot pre-warm completed for ref={ref[:8]}... "
                        f"({written} bytes, export={ref[:8]}...)"
                    ),
                    subject_kind="ramboot_cache",
                    subject_id=ref,
                    actor="ramboot-worker",
                    details={
                        "ref": ref,
                        "image_path": str(image_path),
                        "decompressed_size": written,
                        "export_name": ref,
                    },
                )
                conn.commit()
        except Exception as exc:
            _log.exception("ramboot pre-warm: ref=%s failed", ref)
            # Best-effort cleanup of the partial file so a retry
            # starts clean.
            import contextlib

            for candidate in (image_path, image_path.with_suffix(image_path.suffix + ".inflight")):
                with contextlib.suppress(OSError):
                    candidate.unlink(missing_ok=True)
            with _db.open_db(self._state_path) as conn:
                _set_status(
                    conn,
                    ref,
                    "failed",
                    error=str(exc),
                    set_completed=True,
                )
                _log_event(
                    conn,
                    kind="ramboot.pre_warm.failed",
                    summary=f"ramboot pre-warm failed for ref={ref[:8]}...: {exc}",
                    subject_kind="ramboot_cache",
                    subject_id=ref,
                    actor="ramboot-worker",
                    details={"ref": ref, "error": str(exc)},
                )
                conn.commit()
