"""Route registration for the operator-curated catalog.

Registers ``POST /catalog/entries`` (add-by-URL),
``GET /catalog/entries`` (list), ``DELETE /catalog/entries``
(delete by src), and ``POST /catalog/import`` (bulk-import from a
TOML manifest) via :func:`register_catalog_routes`.

Also registers the two catalog-manifest form endpoints
(``POST /ui/catalog/upload``, ``POST /ui/catalog/fetch-release``)
via :func:`register_catalog_upload_routes`, plus exposes the
shared :func:`import_parsed_catalog` and
:func:`auto_import_manifest_rows` helpers -- the app-lifespan
hook calls the latter directly to seed operator-curated rows on
startup.
"""

from __future__ import annotations

import asyncio
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile
from withcache import oras as _oras

from bty import catalog as _catalog
from bty import images
from bty.web import _db, _models, _settings_store
from bty.web._auth import require_auth
from bty.web._events_log import record as _log_event
from bty.web._helpers import CATALOG_UPLOAD_MAX_BYTES, head_content_length, now_iso
from bty.web._reqctx import client_ip as _client_ip


class _CatalogStateHolder(Protocol):
    """Structural interface for the ``catalog_state`` container the
    app factory hands us. Mutable ``.catalog`` attribute -- the
    reload path swaps it in-place so every closure-captured handler
    sees the new value on the next call."""

    catalog: _catalog.Catalog | None


def auto_import_manifest_rows(catalog: _catalog.Catalog, *, state_path: Path) -> None:
    """Insert a ``catalog_entries`` row for every manifest entry
    that doesn't already have one.

    Without this, an operator who uploads a ``catalog.toml`` via
    /ui/catalog/upload sees the entries on /ui/images (the merge
    renders them) but the /ui/machines/{mac} "Image" dropdown
    stays empty for those entries -- the dropdown queries
    ``catalog_entries`` only. Auto-importing on reload keeps the
    two views consistent: an upload makes the entries bindable
    without an extra ``POST /catalog/import`` round-trip.

    ``INSERT OR IGNORE`` -- operator-curated rows (added via
    the URL form or a prior ``/catalog/import``) for the same
    src are preserved with their original description / sha_url
    intact.
    """
    now = now_iso()
    with _db.open_db(state_path) as conn:
        for entry in catalog.entries:
            try:
                ref = _catalog.image_ref_for_src(entry.src)
            except ValueError:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO catalog_entries "
                "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                "format, size_bytes, description, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref,
                    entry.src,
                    entry.sha256,
                    entry.name,
                    None,
                    entry.format,
                    entry.size_bytes,
                    entry.description,
                    now,
                ),
            )
        conn.commit()


def import_parsed_catalog(
    parsed: _catalog.Catalog,
    *,
    source: str,
    source_ip: str | None,
    state_path: Path,
) -> tuple[int, int, list[dict[str, str]]]:
    """Insert every entry from ``parsed`` into ``catalog_entries``.

    Idempotent: rows whose ``src`` already exists are counted in
    ``skipped`` (sqlite IntegrityError on the UNIQUE constraint)
    rather than overwriting. Returns ``(imported, skipped, errors)``.
    ``source`` is the human-readable origin (a URL, a file path,
    or ``"<upload>"``) and rides into the events-log row so the
    operator can trace where a batch came from.
    """
    imported = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    now = now_iso()
    with _db.open_db(state_path) as conn:
        for entry in parsed.entries:
            sha = entry.sha256
            fmt = entry.format
            size_bytes = entry.size_bytes
            # Default: a plain HTTPS catalog entry is fetchable as-is;
            # oras entries need a manifest walk to produce the canonical
            # registry blob URL, and a ``file://`` entry has no URL
            # withcache or the PXE plan would ever talk to (the local
            # path is the path).
            resolved_src: str | None = (
                entry.src if entry.src.startswith(("http://", "https://")) else None
            )
            if entry.src.startswith("oras://"):
                # Best-effort oras resolution: try to pin sha + size
                # AND populate ``resolved_src`` with the canonical
                # registry blob URL so withcache (which is oras-blind)
                # can warm against it. On failure (offline / registry
                # unreachable / private registry needing auth) we still
                # insert the entry, just without ``resolved_src`` /
                # sha / size pre-filled. The row is bindable via
                # ``bty_image_ref`` even without sha, and a later
                # ``Check`` / re-import will fill in what's missing.
                # Strict-fail mode would refuse offline imports which
                # is operator-hostile for sealed environments.
                try:
                    resolved = _oras.resolve_ref(entry.src)
                except _oras.OrasError as exc:
                    errors.append({"name": entry.name, "error": f"oras (kept without sha): {exc}"})
                else:
                    resolved_src = resolved.blob_url
                    if sha is None:
                        sha = resolved.digest.removeprefix("sha256:")
                    if size_bytes is None:
                        size_bytes = resolved.size
            try:
                bty_image_ref = _catalog.image_ref_for_src(entry.src)
            except ValueError as exc:
                errors.append({"name": entry.name, "error": str(exc)})
                continue
            try:
                _db.insert_catalog_row(
                    conn,
                    bty_image_ref=bty_image_ref,
                    src=entry.src,
                    resolved_src=resolved_src,
                    disk_image_sha=sha,
                    name=entry.name,
                    sha_url=None,
                    format=fmt,
                    size_bytes=size_bytes,
                    description=entry.description,
                    added_at=now,
                )
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1
        _log_event(
            conn,
            kind="catalog.entries.imported",
            summary=(f"imported {imported} entr{'y' if imported == 1 else 'ies'} from {source!r}"),
            subject_kind="catalog",
            subject_id=source,
            actor="operator",
            source_ip=source_ip,
            details={
                "source": source,
                "imported": imported,
                "skipped": skipped,
                "errors": errors,
            },
        )
        conn.commit()
    return imported, skipped, errors


def register_catalog_routes(app: FastAPI, *, state_path: Path) -> None:
    """Attach the operator-curated catalog routes to ``app``.

    ``catalog_entries`` table in state.db backs a UI form where the
    operator pastes ``image-url`` + optional ``sha-url`` and hits
    Add. The shape mirrors a catalog.toml manifest entry, so once
    written the row appears on the operator's catalog page like any
    other entry. No filesystem dance; no TOML editing.
    """

    @app.post(
        "/catalog/entries",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_auth)],
    )
    def add_catalog_entry(body: _models.CatalogEntryAdd, request: Request) -> dict[str, Any]:
        """Add an operator-curated catalog entry by URL.

        Body: ``{"image_url": "...", "sha_url": "..." | null}``.

        - If ``sha_url`` is given: fetches it, parses, picks the
          digest matching the image-URL filename (or the only
          digest if the manifest carries one entry). The entry's
          ``disk_image_sha`` is populated so the cache-through
          step on first flash verifies against it.
        - If ``sha_url`` is null: the entry is URL-only
          (``disk_image_sha`` stays NULL). Still bindable to a
          machine via the row's ``bty_image_ref``; the first
          flash trusts the upstream bytes and back-fills
          ``disk_image_sha`` with what it observed.

        - HEADs ``image_url`` for ``Content-Length`` (best-effort).
        - Inserts a row keyed by image_url.

        ``oras://`` short-circuit: when ``image_url`` starts with
        ``oras://``, the server runs ``withcache.oras.resolve_ref`` at add
        time. The picked layer's digest becomes the entry's
        ``disk_image_sha``, the layer's title annotation becomes
        ``name``, the layer's declared size becomes ``size_bytes``,
        and ``format`` is detected from the title. ``sha_url`` is
        ignored for oras refs (the manifest is authoritative).

        409 if a row with the same image_url already exists. 422
        if the body carries a ``ref`` that doesn't match
        ``image_ref_for_src(image_url)``.
        """
        # Trust-but-verify: if the client supplied a ``ref``,
        # recompute it from the URL and reject mismatches at 422.
        try:
            body.verify_ref()
        except ValueError as exc:
            raise HTTPException(
                # Match the sibling 422 below (the non-deprecated
                # spelling Starlette renamed ``..._ENTITY`` to).
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        # Variables shared across the oras / http branches. Declared
        # up front so mypy sees a single binding (the oras branch
        # narrows ``sha256`` to ``str``, which would clash with a
        # branch-local ``str | None`` re-declaration).
        sha256: str | None = None
        fmt: str | None = None
        size_bytes: int | None = None
        # ``oras://`` short-circuit: resolve the manifest first and
        # populate everything from it. This bypasses both the
        # sha_url branch (no separate sidecar needed) and the
        # HEAD-for-Content-Length call (the layer carries size).
        if body.image_url.startswith("oras://"):
            try:
                resolved = _oras.resolve_ref(body.image_url)
            except _oras.OrasError as exc:
                with _db.open_db(state_path) as conn:
                    _log_event(
                        conn,
                        kind="catalog.entry.add.failed",
                        summary=f"catalog entry add failed for {body.image_url!r}: {exc}",
                        subject_kind="catalog",
                        subject_id=body.image_url,
                        actor="operator",
                        source_ip=_client_ip(request),
                        details={"image_url": body.image_url, "error": str(exc)},
                    )
                    conn.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"could not resolve oras ref: {exc}",
                ) from exc
            # Layer digest is ``sha256:<hex>``; strip the algorithm
            # prefix since the schema column stores bare 64-hex.
            sha256 = resolved.digest.removeprefix("sha256:")
            # Display name: prefer the layer's title annotation
            # (typically the upstream filename, e.g.
            # ``nosi-debian-sysdev-x86_64.img.gz``). Fall back to
            # the repository basename when the manifest doesn't
            # annotate the layer.
            ref = _oras.parse_ref(body.image_url)
            name = resolved.title or ref.repository.rsplit("/", 1)[-1]
            fmt = images.detect_format(Path(name)) or "img.gz"
            size_bytes = resolved.size
            now = now_iso()
            try:
                bty_image_ref = _catalog.image_ref_for_src(body.image_url)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid image_url: {exc}",
                ) from exc
            with _db.open_db(state_path) as conn:
                try:
                    _db.insert_catalog_row(
                        conn,
                        bty_image_ref=bty_image_ref,
                        src=body.image_url,
                        resolved_src=resolved.blob_url,
                        disk_image_sha=sha256,
                        name=name,
                        sha_url=None,
                        format=fmt,
                        size_bytes=size_bytes,
                        description=None,
                        added_at=now,
                    )
                    _log_event(
                        conn,
                        kind="catalog.entry.added",
                        summary=f"catalog entry added (oras): {name}",
                        subject_kind="catalog",
                        subject_id=body.image_url,
                        actor="operator",
                        source_ip=_client_ip(request),
                        details={
                            "name": name,
                            "bty_image_ref": bty_image_ref,
                            "disk_image_sha": sha256,
                            "format": fmt,
                            "size_bytes": size_bytes,
                            "oras": True,
                        },
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"catalog entry with src={body.image_url} already exists",
                    ) from exc
            return {
                "src": body.image_url,
                "bty_image_ref": bty_image_ref,
                "disk_image_sha": sha256,
                "name": name,
                "sha_url": None,
                "format": fmt,
                "size_bytes": size_bytes,
                "added_at": now,
            }

        if body.sha_url is not None:
            try:
                sha256 = _catalog.fetch_sha256_for_url(body.image_url, body.sha_url)
            except _catalog.CatalogError as exc:
                with _db.open_db(state_path) as conn:
                    _log_event(
                        conn,
                        kind="catalog.entry.add.failed",
                        summary=f"catalog entry add failed for {body.image_url!r}: {exc}",
                        subject_kind="catalog",
                        subject_id=body.image_url,
                        actor="operator",
                        source_ip=_client_ip(request),
                        details={
                            "image_url": body.image_url,
                            "sha_url": body.sha_url,
                            "error": str(exc),
                        },
                    )
                    conn.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"could not resolve sha256: {exc}",
                ) from exc

        parsed = urllib.parse.urlparse(body.image_url)
        name = Path(parsed.path).name
        if not name:
            # ``https://example.com`` (no path) and ``https://example.com/foo/``
            # (trailing slash) both surface as empty ``Path.name``. Without a
            # filename component there's nothing meaningful to display in the
            # catalog table and the URL streaming pipeline can't pick a cache
            # key. Refuse at the API boundary rather than silently falling back
            # to "the whole URL is the name", which makes the UI render
            # ``<code>https://...</code>`` as the entry's display label.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "image_url must end in a filename component "
                    "(e.g. https://example.com/path/foo.img.gz); "
                    f"got {body.image_url!r} which has no basename"
                ),
            )
        fmt = images.detect_format(Path(name))
        size_bytes = head_content_length(body.image_url)
        now = now_iso()
        try:
            bty_image_ref = _catalog.image_ref_for_src(body.image_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid image_url: {exc}",
            ) from exc
        with _db.open_db(state_path) as conn:
            try:
                _db.insert_catalog_row(
                    conn,
                    bty_image_ref=bty_image_ref,
                    src=body.image_url,
                    resolved_src=body.image_url,
                    disk_image_sha=sha256,
                    name=name,
                    sha_url=body.sha_url,
                    format=fmt,
                    size_bytes=size_bytes,
                    description=None,
                    added_at=now,
                )
                _log_event(
                    conn,
                    kind="catalog.entry.added",
                    summary=f"catalog entry added: {name}",
                    subject_kind="catalog",
                    subject_id=body.image_url,
                    actor="operator",
                    source_ip=_client_ip(request),
                    details={
                        "name": name,
                        "bty_image_ref": bty_image_ref,
                        "disk_image_sha": sha256,
                        "format": fmt,
                        "size_bytes": size_bytes,
                    },
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"catalog entry with src={body.image_url} already exists",
                ) from exc
        return {
            "src": body.image_url,
            "bty_image_ref": bty_image_ref,
            "disk_image_sha": sha256,
            "name": name,
            "sha_url": body.sha_url,
            "format": fmt,
            "size_bytes": size_bytes,
            "added_at": now,
        }

    @app.get(
        "/catalog/entries",
        dependencies=[Depends(require_auth)],
    )
    def list_catalog_entries() -> list[dict[str, Any]]:
        with _db.open_db(state_path) as conn:
            rows = conn.execute(
                "SELECT bty_image_ref, src, resolved_src, disk_image_sha, name, sha_url, "
                "format, size_bytes, description, added_at "
                "FROM catalog_entries ORDER BY added_at"
            ).fetchall()
        return [dict(row) for row in rows]

    @app.delete(
        "/catalog/entries",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_auth)],
    )
    def delete_catalog_entry(src: str, request: Request) -> Response:
        """Delete via ``?src=<url>`` query param. URL-as-path-param
        would require percent-encoding the schema and slashes,
        which is operator-hostile; query param is cleaner.

        The DB is the authoritative catalog: ``catalog.toml`` is an
        import seed (``_auto_import_manifest_rows``), not a live
        overlay that re-injects deletions. So a delete that succeeds
        at the DB level is genuinely the end of the entry's lifetime
        -- no re-injection on next render.
        """
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM catalog_entries WHERE src = ?", (src,))
            if cur.rowcount > 0:
                _log_event(
                    conn,
                    kind="catalog.entry.deleted",
                    summary=f"catalog entry deleted: {src}",
                    subject_kind="catalog",
                    subject_id=src,
                    actor="operator",
                    source_ip=_client_ip(request),
                )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no catalog entry with src={src}",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/catalog/import",
        dependencies=[Depends(require_auth)],
    )
    def import_catalog(source: str, request: Request) -> dict[str, Any]:
        """Bulk-import catalog entries from a TOML manifest source.

        ``source`` is a query parameter: a local path on the
        bty-server host (``/etc/bty/my-catalog.toml``), an
        ``http(s)://`` URL pointing at a TOML manifest, or an
        ``oras://`` reference whose layer is the manifest. Parsed
        through :func:`bty.catalog.load_source` so the same client-
        side fetcher ``bty`` uses applies here.

        **Metadata-only**. Bytes are NOT fetched at import time. From
        v0.40 the catalog-Download manager + the per-entry Fetch
        button are gone; bytes materialise on demand at flash time
        via the withcache warm-fetch path (oras + https), or the raw
        upstream origin when no withcache is configured.

        Per-entry behaviour:

        - If the TOML entry carries a ``sha256``, it's inserted as-is.
        - Else if the entry's ``src`` is ``oras://``, the registry
          manifest is resolved at import time to get the layer digest
          (= sha256). Errors propagate into the per-entry ``errors``
          list, not a request-level 4xx.
        - Else (http(s):// URL with no sha): the entry is URL-only
          (``disk_image_sha=NULL``). Still bindable to a machine
          via ``bty_image_ref``; the first flash's cache-through
          populates ``disk_image_sha``.

        Idempotent: re-importing the same source skips entries whose
        ``src`` already exists (counted in ``skipped``).

        Returns:

        .. code-block:: json

           {
             "source": "...",
             "imported": 3,
             "skipped": 1,
             "errors": [{"name": "...", "error": "..."}]
           }

        """
        try:
            parsed = _catalog.load_source(source)
        except (ValueError, _catalog.CatalogError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"failed to load catalog from {source!r}: {exc}",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"failed to fetch catalog from {source!r}: {exc}",
            ) from exc
        imported, skipped, errors = import_parsed_catalog(
            parsed, source=source, source_ip=_client_ip(request), state_path=state_path
        )
        return {
            "source": source,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }


def register_catalog_upload_routes(
    app: FastAPI,
    *,
    state_path: Path,
    manifest_path: Path,
    catalog_state: _CatalogStateHolder,
    publish_state_changed: Callable[[], None],
) -> None:
    """Attach ``POST /ui/catalog/upload`` + ``POST /ui/catalog/fetch-release``
    to ``app``. Authenticated endpoints; only operators logged into
    the bty-web UI can enqueue catalog reloads."""

    async def _reload_catalog_from_disk() -> None:
        """Re-read ``manifest_path`` and refresh the in-process catalog.

        Called after a manifest write (UI upload or release fetch).
        Raises :class:`_catalog.CatalogError` on parse failure -- the
        caller wraps it into an HTTP 400 + flash error so the
        operator sees what's wrong rather than getting a silent
        no-op.

        Auto-imports the parsed entries into ``catalog_entries``
        as a side-effect so the /ui/machines/{mac} dropdown
        becomes populated without a separate ``POST /catalog/import``
        step. Idempotent (``INSERT OR IGNORE``).
        """
        new_catalog = _catalog.load(manifest_path)
        catalog_state.catalog = new_catalog
        auto_import_manifest_rows(new_catalog, state_path=state_path)
        publish_state_changed()

    @app.post(
        "/ui/catalog/upload",
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    async def upload_catalog_manifest(request: Request) -> RedirectResponse:
        """Receive a multipart ``catalog.toml`` upload, save it as
        ``${BTY_STATE_DIR}/catalog.toml`` (or whatever
        ``$BTY_CATALOG_FILE`` overrides to), parse, and reload the
        download manager in-process. 303s back to /ui/images with
        either a success or ``?error=`` query param so the page's
        flash slot surfaces the outcome.

        Validation layers, in order:

        * ``file`` field present + an UploadFile.
        * Size cap: ``CATALOG_UPLOAD_MAX_BYTES`` (1 MiB). A real
          ``catalog.toml`` is a handful of KB; anything multi-MB
          is almost certainly an operator dropping the wrong file
          (an .iso, an image) into the catalog form by mistake,
          and rejecting at the boundary beats OOM-ing the
          process trying to parse it as TOML.
        * Non-empty body.
        * Filename extension hint (``.toml`` / ``.tml``): served
          purely as a clearer-error path. The actual gate is the
          TOML parse below; a .yaml file accidentally renamed
          to .toml will still bounce on parse failure, and a
          stripped-extension upload that is valid TOML still
          works.
        * Parses as a valid catalog manifest.
        """
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote("no file in upload", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        filename = upload.filename or ""
        if filename and not filename.lower().endswith((".toml", ".tml")):
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    f"unexpected file extension for catalog upload: {filename!r} (expected .toml)",
                    safe="",
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        # Read up to the cap+1 so we can distinguish "exactly the
        # cap" from "more than the cap".
        content = await upload.read(CATALOG_UPLOAD_MAX_BYTES + 1)
        if len(content) > CATALOG_UPLOAD_MAX_BYTES:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    f"catalog upload exceeded {CATALOG_UPLOAD_MAX_BYTES} bytes; "
                    "is this actually a catalog.toml?",
                    safe="",
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if not content:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote("upload was empty", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        # Parse the uploaded TOML and import each entry into the
        # ``catalog_entries`` DB so the table on /ui/images picks
        # the rows up. Also persist the bytes to ``manifest_path``
        # so the import is durable across restarts (the lifespan
        # auto-import seeds the DB from this file on the next boot).
        try:
            parsed = _catalog.load_bytes(content, source="<upload>")
        except _catalog.CatalogError as exc:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote(f"catalog parse failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        import_parsed_catalog(
            parsed, source="<upload>", source_ip=_client_ip(request), state_path=state_path
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(content)
        await _reload_catalog_from_disk()
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    @app.post(
        "/ui/catalog/fetch-release",
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    async def fetch_release_catalog() -> RedirectResponse:
        """Fetch ``catalog.toml`` from the bty project's GitHub
        release page (``releases/latest/download/catalog.toml``),
        save it at the manifest path, and reload. Symmetric with the
        boot-artifacts page's "Fetch latest release" button.

        Error paths surface via ``?error=`` so the operator sees
        what went wrong on the /ui/images flash slot:

        * Network failure / timeout -> URLError / TimeoutError.
        * HTTP non-2xx (e.g. release tag has no catalog.toml asset
          and GitHub returns a 404 HTML page) -> HTTPError, caught
          by the same URLError branch since HTTPError is a
          URLError subclass.
        * Oversized body (release page returned something
          unexpected and huge) -> rejected against
          ``CATALOG_UPLOAD_MAX_BYTES`` before parse.
        * Non-TOML body (e.g. HTML 404) -> caught by load_bytes'
          TOMLDecodeError -> CatalogError.
        """
        with _db.open_db(state_path) as conn:
            catalog_url = _settings_store.resolve_catalog_url(conn)

        def _fetch_sync() -> bytes:
            # urllib.request.urlopen is blocking; run it on a worker
            # thread via asyncio.to_thread so a slow/unreachable
            # release page doesn't stall the event loop for the full
            # 30-second timeout. Other requests (including SSE
            # heartbeats) would otherwise queue behind it.
            with urllib.request.urlopen(catalog_url, timeout=30) as resp:
                # Bound the read at the catalog upload cap + 1 byte
                # so a release page that responds with a huge
                # unexpected body (HTML, a binary asset that
                # somehow got the catalog.toml URL pointed at it)
                # can't OOM the worker.
                body: bytes = resp.read(CATALOG_UPLOAD_MAX_BYTES + 1)
                return body

        try:
            content = await asyncio.to_thread(_fetch_sync)
        except (urllib.error.URLError, TimeoutError) as exc:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote(f"release fetch failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if len(content) > CATALOG_UPLOAD_MAX_BYTES:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    f"fetched catalog exceeded {CATALOG_UPLOAD_MAX_BYTES} bytes; "
                    "release URL did not serve a catalog.toml",
                    safe="",
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if not content:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote("fetched catalog was empty", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        try:
            parsed = _catalog.load_bytes(content, source=catalog_url)
        except _catalog.CatalogError as exc:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(f"fetched catalog parse failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        # Import rows into the ``catalog_entries`` DB AND persist
        # the bytes to ``manifest_path`` so the import is durable
        # across restarts (the lifespan auto-import seeds the DB
        # from this file on the next boot).
        import_parsed_catalog(parsed, source=catalog_url, source_ip=None, state_path=state_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(content)
        await _reload_catalog_from_disk()
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)
