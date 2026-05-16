"""bty-web download manager for the catalog.

Routes catalog fetches through an asyncio-supervised worker pool
so the operator can:

  * watch live progress (bytes downloaded / total / percent) for
    every active fetch via ``GET /catalog/downloads``,
  * cancel an in-flight fetch via ``DELETE /catalog/downloads/{name}``
    -- the worker checks the cancel flag between 1 MiB chunks and
    aborts within seconds, leaving no half-written cache,
  * cap parallelism so a typo on the catalog page doesn't trigger
    five simultaneous multi-GiB downloads (limit via
    ``BTY_CATALOG_MAX_PARALLEL``, default 2).

State is in-memory; on server restart, in-flight downloads die with
the worker and the cache directory is the source of truth for "what
is cached" (the no-half-written invariant from
``bty.catalog.fetch_to_cache`` survives restart -- a partial download
leaves no file).

Module is layered on top of ``bty.catalog``: this file holds the
async + state machinery, ``bty.catalog`` holds the byte-pumping +
SHA verification. Keeps the CLI path (``bty catalog fetch``) free
of any asyncio dependency.

Lifecycle plumbing lives in :class:`bty.web._jobs._BaseAsyncManager`.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bty import catalog as _catalog
from bty.web._jobs import ENQUEUE_DEDUP_STATES, _BaseAsyncManager

# Default cap on simultaneous downloads. Tuned so a typical homelab
# uplink isn't saturated by N parallel fetches; bumpable via env.
DEFAULT_MAX_PARALLEL = 2


def _reject_traversal_name(name: str) -> None:
    """Reject anything that's not a plain basename. Mirrors the
    same check on :class:`bty.web._hash.HashManager` so a
    non-API caller can't slip a path-traversal name past the
    catalog lookup."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(
            f"invalid name {name!r}: must be a basename without path separators or NUL bytes"
        )


@dataclass
class DownloadState:
    """Live state of a single catalog fetch.

    Mutable on purpose -- the worker updates ``status`` /
    ``bytes_downloaded`` / ``bytes_total`` / timestamps as the
    download proceeds, and the API serialises the current snapshot
    for ``GET /catalog/downloads``.
    """

    name: str
    sha256: str | None
    src: str
    status: str = "queued"  # queued | running | completed | cancelled | failed
    bytes_downloaded: int = 0
    bytes_total: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # Threading.Event because the actual IO happens in a worker
    # thread (via ``asyncio.to_thread``); ``asyncio.Event`` is not
    # thread-safe to query from inside the thread.
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # Build manually rather than via ``dataclasses.asdict`` --
        # that helper deep-copies every field, and threading.Event
        # contains a ``_thread.lock`` which cannot be pickled.
        return {
            "name": self.name,
            "sha256": self.sha256,
            "src": self.src,
            "status": self.status,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class DownloadManager(_BaseAsyncManager[DownloadState]):
    """Async worker-pool scheduler for catalog fetches.

    ``start(catalog, cache_dir)`` spawns workers, ``enqueue(name)``
    queues a job (idempotent on already-queued / completed /
    running), ``cancel(name)`` flips the per-job event, ``stop()``
    drains.
    """

    def __init__(self, max_parallel: int | None = None) -> None:
        super().__init__(max_parallel or _resolve_max_parallel())
        self._catalog: _catalog.Catalog | None = None
        self._cache_dir: Path | None = None
        # When set, the worker back-fills ``catalog_entries.disk_image_sha``
        # after a successful fetch on entries that had no pinned sha.
        # Tests that exercise the manager standalone leave this None.
        self._state_path: Path | None = None

    def start(
        self,
        catalog: _catalog.Catalog,
        cache_dir: Path,
        state_path: Path | None = None,
    ) -> None:
        """Bind the manager to a manifest + cache dir and spawn workers.

        ``state_path`` enables two behaviours:

        * Operator-initiated fetches of un-sha'd entries back-fill
          ``catalog_entries.disk_image_sha`` (and emit a
          ``catalog.cache.populated`` event) on completion.
        * The in-memory states pre-populate from recent
          ``catalog.cache.populated`` / ``catalog.fetch.sha_mismatch``
          events so the /ui/images Downloads table shows recent
          activity across bty-web restarts.
        """
        self._catalog = catalog
        self._cache_dir = cache_dir
        self._state_path = state_path
        if state_path is not None:
            self._backfill_from_events(state_path)
        self._spawn_workers()

    def _backfill_from_events(self, state_path: Path) -> None:
        """Repopulate ``_states`` with recent terminal outcomes from
        the events log so the /ui/images Downloads table shows
        history across restarts.

        Reads ``catalog.cache.populated`` (success) and
        ``catalog.fetch.sha_mismatch`` (failure) events; newest-per-
        name wins. Soft-fails on any DB exception so a corrupt
        state.db can't keep bty-web from starting.
        """
        from bty.web import _db, _events_log

        try:
            with _db.open_db(state_path) as conn:
                rows = _events_log.list_events(
                    conn,
                    subject_kind="catalog",
                    limit=400,
                )
        except Exception:
            return
        seen: set[str] = set()
        for ev in rows:
            if ev.kind not in (
                "catalog.cache.populated",
                "catalog.fetch.sha_mismatch",
            ):
                continue
            details = ev.details or {}
            # Operator-initiated fetches log ``name`` in details;
            # cache-through (pxe-client actor) logs ``src``. Prefer
            # ``name`` so the key matches the DownloadManager's
            # primary key.
            name = details.get("name") if isinstance(details.get("name"), str) else None
            if name is None:
                # Skip cache-through events: they're keyed by ref,
                # not catalog entry name, and don't represent
                # operator-driven fetches.
                continue
            if name in seen:
                continue
            seen.add(name)
            try:
                started = (
                    datetime.fromisoformat(ev.ts.replace("Z", "+00:00")).timestamp()
                    if ev.ts
                    else None
                )
            except (TypeError, ValueError):
                started = None
            sha_raw = details.get("disk_image_sha")
            sha = sha_raw if isinstance(sha_raw, str) else None
            src_raw = details.get("src")
            src = src_raw if isinstance(src_raw, str) else ""
            size = int(details.get("size_bytes") or 0)
            self._states[name] = DownloadState(
                name=name,
                sha256=sha,
                src=src,
                status="completed" if ev.kind == "catalog.cache.populated" else "failed",
                bytes_downloaded=size,
                bytes_total=size or None,
                started_at=started,
                finished_at=started,
                error=(
                    str(details.get("error")) if ev.kind == "catalog.fetch.sha_mismatch" else None
                ),
            )

    async def enqueue(self, name: str) -> DownloadState:
        """Look up the entry, create / re-use a state, and push
        onto the queue if a fresh download is needed.

        Returns the current ``DownloadState`` (which may already be
        ``running`` / ``completed`` / ``cancelled``). Callers expect
        idempotency on repeat enqueues for the same name.

        Raises :class:`ValueError` if ``name`` carries path-
        traversal characters. The catalog lookup at
        :meth:`bty.catalog.ParsedCatalog.by_name` would already
        return ``None`` for those (no catalog entry matches), but
        rejecting at the boundary makes the failure mode explicit
        and lines up with :class:`bty.web._hash.HashManager`.
        """
        _reject_traversal_name(name)
        if self._catalog is None or self._cache_dir is None:
            raise RuntimeError("DownloadManager not started")
        entry = self._catalog.by_name(name)
        if entry is None:
            raise KeyError(f"no catalog entry named {name!r}")

        async with self._lock:
            existing = self._states.get(name)
            if existing is not None and existing.status in ENQUEUE_DEDUP_STATES:
                return existing
            # ``cancelled`` / ``failed`` (or no existing state) -- create a fresh one.
            state = DownloadState(
                name=entry.name,
                sha256=entry.sha256,
                src=entry.src,
            )
            # If sha is pinned AND already cached on disk, mark
            # complete immediately. The un-sha'd case can't take
            # this shortcut because ``is_cached`` needs a sha to
            # build the cache path.
            if entry.sha256 is not None and _catalog.is_cached(entry, self._cache_dir):
                size = entry.cached_path(self._cache_dir).stat().st_size
                state.status = "completed"
                state.bytes_downloaded = size
                state.bytes_total = size
                state.started_at = state.finished_at = time.time()
                self._states[name] = state
                return state
            self._states[name] = state
            await self._queue.put(name)
            return state

    async def _run_one(self, state: DownloadState) -> None:
        """Run a single fetch in a worker thread, snapshot the
        result back into ``state``.

        Dispatches by entry shape:

        * ``entry.sha256`` pinned -> :func:`bty.catalog.fetch_to_cache`
          (download + verify against pinned sha).
        * ``entry.sha256`` is ``None`` (rolling oras tag, URL-only
          entry never hashed) -> :func:`bty.catalog.fetch_src_to_cache`
          (download + compute sha + back-fill ``catalog_entries``).
          The computed sha lands on ``state.sha256`` so the UI sees
          it; the back-fill UPDATEs the catalog row so the entry
          shows ``Cached`` + a content-sha on the next page load.
        """
        assert self._catalog is not None
        assert self._cache_dir is not None
        cancel_event = state._cancel

        # Re-resolve the entry inside the worker. ``enqueue`` looked
        # it up at submit time, but the manifest could in theory
        # have been reloaded between then and now; re-resolving
        # at run time keeps us honest and is cheap.
        entry = self._catalog.by_name(state.name)
        if entry is None:
            async with self._lock:
                state.status = "failed"
                state.error = "manifest entry vanished"
                state.finished_at = time.time()
            return

        def _progress(downloaded: int, total: int | None) -> None:
            state.bytes_downloaded = downloaded
            if total is not None:
                state.bytes_total = total

        def _cancel() -> bool:
            return cancel_event.is_set()

        computed_sha: str | None = None
        try:
            if entry.sha256 is not None:
                await asyncio.to_thread(
                    _catalog.fetch_to_cache,
                    entry,
                    self._cache_dir,
                    progress=_progress,
                    cancel=_cancel,
                )
                computed_sha = entry.sha256
            else:
                # Un-sha'd entry: download + compute sha + back-fill.
                # ``fetch_src_to_cache`` returns ``(path, sha)`` and
                # writes to ``cache_dir/<computed_sha>``.
                _cached, computed_sha = await asyncio.to_thread(
                    _catalog.fetch_src_to_cache,
                    entry.src,
                    self._cache_dir,
                    expected_sha=None,
                    progress=_progress,
                    cancel=_cancel,
                )
                state.sha256 = computed_sha
            final_status = "completed"
            error = None
        except _catalog.CatalogCancelled:
            final_status = "cancelled"
            error = None
        except (_catalog.CatalogError, Exception) as exc:
            # Cancel-vs-IO-error race: if the cancel flag fired
            # between chunks but urllib raised before the chunk
            # boundary's cancel check, treat as cancellation
            # rather than failure.
            if cancel_event.is_set():
                final_status = "cancelled"
                error = None
            else:
                final_status = "failed"
                error = (
                    str(exc)
                    if isinstance(exc, _catalog.CatalogError)
                    else f"{type(exc).__name__}: {exc}"
                )

        # Back-fill catalog_entries.disk_image_sha and emit a
        # ``catalog.cache.populated`` event on successful operator-
        # initiated fetches of previously-un-sha'd entries. Symmetric
        # with the live env's cache-through path so the audit log
        # records the back-fill regardless of who triggered it.
        if (
            final_status == "completed"
            and computed_sha is not None
            and entry.sha256 is None
            and self._state_path is not None
        ):
            from bty.web import _db, _events_log

            try:
                with _db.open_db(self._state_path) as conn:
                    conn.execute(
                        "UPDATE catalog_entries SET disk_image_sha = ? "
                        "WHERE name = ? AND disk_image_sha IS NULL",
                        (computed_sha, state.name),
                    )
                    _events_log.record(
                        conn,
                        kind="catalog.cache.populated",
                        summary=f"fetched + hashed {state.name!r}",
                        subject_kind="catalog",
                        subject_id=state.name,
                        actor="operator",
                        details={
                            "name": state.name,
                            "src": entry.src,
                            "disk_image_sha": computed_sha,
                            "size_bytes": state.bytes_total or state.bytes_downloaded,
                        },
                    )
                    conn.commit()
            except Exception:
                # DB write failed; the file is still cached on disk
                # (the bytes-write was atomic before this block). The
                # operator's next page-load triggers a fresh sha
                # detection via the merge_with_catalog path, so the
                # back-fill is a UX nicety, not a correctness
                # guarantee.
                pass

        async with self._lock:
            state.status = final_status
            state.finished_at = time.time()
            state.error = error


def _resolve_max_parallel() -> int:
    raw = os.environ.get("BTY_CATALOG_MAX_PARALLEL")
    if raw is None:
        return DEFAULT_MAX_PARALLEL
    try:
        n = int(raw)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        return DEFAULT_MAX_PARALLEL
