"""In-process view of withcache's catalog.

Since withcache 0.9.1 the catalog lives entirely on the withcache
side (JSON ``GET /catalog`` + Bearer-gated ``POST /catalog/entries``
+ ``DELETE /catalog/entries?name=<name>``). bty caches a snapshot
in memory here and every image-lookup + machine-binding read site
hits this cache instead of a local ``catalog_entries`` table
(dropped in the same PR that added this module).

Mutations from bty's UI flow through :meth:`add` / :meth:`delete`,
which POST/DELETE to withcache and then :meth:`refresh` to pick up
the change. Startup wiring calls :meth:`refresh` once so the
first render sees populated entries.

Each entry carries the same shape withcache's JSON returns:

    {
        "name": "...",             # display + delete key
        "src": "...",              # canonical origin URL
        "resolved_src": "...",     # canonical fetch URL (for oras://)
        "format": "img.gz" | ...,  # optional
        "arch": "x86_64" | ...,    # optional
        "sha256": "...",           # optional
        "size_bytes": 123,         # optional int
        "description": "...",      # optional
    }

plus a bty-computed ``bty_image_ref`` field that the machine
binding table's FK points at. ``bty_image_ref = sha256(canonicalise_src(src))``
so it's derived, not stored on the withcache side.
"""

from __future__ import annotations

import threading
from typing import Any

from withcache import client as _wc_client

from bty import catalog as _bty_catalog


class WithcacheCatalog:
    """Thread-safe cache of withcache's catalog + a thin write path.

    The cache is populated by :meth:`refresh` which fetches
    ``GET /catalog`` and enriches each entry with a computed
    ``bty_image_ref``. Consumers read via :attr:`entries` (list),
    :meth:`get_by_ref` (fast lookup by the machine-binding key),
    or :meth:`get_by_name` (delete-key lookup).

    Not configured (empty ``withcache_url``): every operation is a
    no-op; :attr:`entries` returns ``[]`` and :attr:`last_error`
    carries a stub explanation so the UI can flag "no catalog
    source" rather than "empty catalog".
    """

    def __init__(self, withcache_url: str | None) -> None:
        self._url: str | None = (withcache_url or "").strip() or None
        self._entries: list[dict[str, Any]] = []
        self._entries_by_ref: dict[str, dict[str, Any]] = {}
        self._entries_by_name: dict[str, dict[str, Any]] = {}
        self._last_error: str | None = None if self._url else "withcache URL not configured"
        self._fetched_at: str | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._url is not None

    @property
    def withcache_url(self) -> str | None:
        return self._url

    def set_withcache_url(self, url: str | None) -> None:
        """Rebind the catalog to a different withcache. Called from the
        Settings save handler when the operator flips
        ``[ramboot] withcache_url`` (yes, the same knob nbdmux
        consults; bty follows suit so the operator has one place
        to point). Clears the in-memory cache; next :meth:`refresh`
        pulls from the new source."""
        with self._lock:
            self._url = (url or "").strip() or None
            self._entries = []
            self._entries_by_ref = {}
            self._entries_by_name = {}
            self._last_error = None if self._url else "withcache URL not configured"
            self._fetched_at = None

    def refresh(self) -> None:
        """Pull ``GET /catalog`` and rebuild the cache.

        Silent no-op when unconfigured. On transport / HTTP / JSON
        failure the previous cache is kept and :attr:`last_error` is
        updated so the UI can surface the freshness signal.
        """
        if self._url is None:
            return
        try:
            snapshot = _wc_client.list_catalog(self._url)
        except _wc_client.WithcacheError as exc:
            with self._lock:
                self._last_error = str(exc)
            return
        entries: list[dict[str, Any]] = snapshot.get("entries") or []
        enriched: list[dict[str, Any]] = []
        by_ref: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for entry in entries:
            src = entry.get("src")
            if not isinstance(src, str) or not src:
                continue
            try:
                ref = _bty_catalog.image_ref_for_src(src)
            except (ValueError, TypeError):
                # A src that doesn't parse to a canonical form is
                # unbindable; skip it rather than crash the whole
                # refresh. The withcache-side add would normally
                # have rejected it, so a hit here means an operator
                # hand-edited catalog.toml with a bogus row.
                continue
            enriched_entry = {**entry, "bty_image_ref": ref}
            enriched.append(enriched_entry)
            by_ref[ref] = enriched_entry
            name = entry.get("name")
            if isinstance(name, str) and name:
                by_name[name] = enriched_entry
        with self._lock:
            self._entries = enriched
            self._entries_by_ref = by_ref
            self._entries_by_name = by_name
            self._last_error = snapshot.get("last_error")
            self._fetched_at = snapshot.get("fetched_at")

    @property
    def entries(self) -> list[dict[str, Any]]:
        """Snapshot of the current catalog. Returns a shallow copy so
        callers can iterate without holding the internal lock."""
        with self._lock:
            return list(self._entries)

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def fetched_at(self) -> str | None:
        with self._lock:
            return self._fetched_at

    def get_by_ref(self, ref: str) -> dict[str, Any] | None:
        """Fast lookup by ``bty_image_ref`` -- the machine-binding key.
        The iPXE plan renderer + the machine-detail page both use
        this."""
        with self._lock:
            return self._entries_by_ref.get(ref)

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            return self._entries_by_name.get(name)

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        """POST an entry to withcache and refresh the cache.

        Raises :class:`RuntimeError` when withcache isn't configured
        -- the catalog is single-sourced from withcache since v0.66.0
        and a standalone bty deploy has nowhere to persist an add.
        Any withcache-side rejection (409 duplicate name, 400
        missing field, etc.) propagates as
        :class:`withcache.client.WithcacheError`.
        """
        if self._url is None:
            raise RuntimeError(
                "withcache URL not configured; catalog writes are only "
                "possible against a running withcache server"
            )
        result: dict[str, Any] = _wc_client.add_catalog_entry(self._url, entry)
        self.refresh()
        return result

    def delete(self, name: str) -> None:
        """DELETE an entry by name from withcache and refresh the
        cache. 404 (no such entry) is treated as success so the
        operator's "make sure this is gone" intent is idempotent.

        Raises :class:`RuntimeError` when withcache isn't
        configured, same reasoning as :meth:`add`.
        """
        if self._url is None:
            raise RuntimeError(
                "withcache URL not configured; catalog deletes are only "
                "possible against a running withcache server"
            )
        _wc_client.delete_catalog_entry(self._url, name)
        self.refresh()

    def _seed_for_tests(self, entries: list[dict[str, Any]]) -> None:
        """Populate the in-memory cache directly, bypassing the HTTP
        round-trip. TEST-ONLY: gives fixtures a way to inject rows
        without spinning up a withcache TestClient. Each entry is
        enriched with ``bty_image_ref`` the same way :meth:`refresh`
        would."""
        enriched: list[dict[str, Any]] = []
        by_ref: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for entry in entries:
            src = entry.get("src")
            if not isinstance(src, str) or not src:
                continue
            try:
                ref = _bty_catalog.image_ref_for_src(src)
            except (ValueError, TypeError):
                continue
            e = {**entry, "bty_image_ref": ref}
            enriched.append(e)
            by_ref[ref] = e
            name = entry.get("name")
            if isinstance(name, str) and name:
                by_name[name] = e
        with self._lock:
            self._entries = enriched
            self._entries_by_ref = by_ref
            self._entries_by_name = by_name
            self._last_error = None
