"""FastAPI application for bty-web.

``create_app(state_path, service_user, image_root)`` returns a fully
wired FastAPI instance. Tests construct one with a tmp_path SQLite +
a fixture service user (PAM gets monkeypatched in those tests).
``main()`` (in :mod:`bty.web.__init__`) builds one from environment
+ defaults and hands it to uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

import bty
from bty import catalog as _catalog
from bty import images
from bty import oras as _oras
from bty.web import _catalog as _web_catalog
from bty.web import _db, _hash, _models, _release_mgr, _sysconfig, _ui
from bty.web._auth import SESSION_COOKIE, require_auth
from bty.web._events import MachineEvent, MachineEventBus, sse_format
from bty.web._events_log import list_events as _list_events
from bty.web._events_log import normalize_ip as _normalize_ip
from bty.web._events_log import record as _log_event

# Session cookie max-age. Sliding TTL on the browser side; Starlette's
# SessionMiddleware refreshes the cookie on each authed response, so
# active sessions stay alive while idle ones eventually expire.
_SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

TEMPLATES_DIR = Path(__file__).parent / "_templates"
STATIC_DIR = Path(__file__).parent / "_static"


def create_app(
    *,
    state_path: Path,
    service_user: str,
    secret_key: str,
    image_root: Path | None = None,
    boot_root: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app. All config flows through this function.

    ``service_user`` is the Linux account whose OS password gates
    ``POST /ui/login`` - typically the user bty-web is running as
    (resolved from ``geteuid`` in :func:`bty.web.main`). Tests pass a
    fixture name and monkeypatch ``pamela.authenticate``.

    ``secret_key`` is the per-appliance random key used by Starlette's
    :class:`SessionMiddleware` to sign session cookies. It must persist
    across bty-web restarts (otherwise every restart logs everyone out)
    and must be unique per appliance (otherwise a cookie minted by one
    server is valid on another). On the appliance,
    ``bty-web-init`` writes a 32-byte random key to
    ``/var/lib/bty/session-secret`` on first boot.

    ``boot_root`` is where the live-env artifacts (kernel + initrd +
    squashfs) live for the ``GET /boot/{name}`` endpoint; defaults to
    ``state_path.parent / "boot"`` (i.e. ``/var/lib/bty/boot`` on a
    stock appliance).
    """
    resolved_image_root: Path = image_root or images.default_image_root()
    resolved_boot_root: Path = boot_root or (state_path.parent / "boot")
    event_bus = MachineEventBus()

    # Catalog manifest + cache + download manager. Optional: if no
    # manifest is configured (operator hasn't authored one), the
    # ``DownloadManager`` simply isn't started and the
    # ``/catalog/...`` endpoints return 404. ``BTY_CATALOG_FILE``
    # and ``BTY_CATALOG_CACHE_DIR`` override the defaults derived
    # from ``BTY_STATE_DIR``.
    manifest_path = _catalog.default_manifest_path()
    catalog_cache_dir = _catalog.default_cache_dir()
    parsed_catalog: _catalog.Catalog | None = None
    if manifest_path is not None:
        try:
            parsed_catalog = _catalog.load(manifest_path)
        except _catalog.CatalogError as exc:
            # Don't crash bty-web startup over a malformed manifest;
            # log it and proceed without the catalog feature. The
            # operator sees the empty catalog page + can fix the
            # manifest then restart.
            print(f"bty-web: catalog manifest at {manifest_path}: {exc}", file=sys.stderr)
            parsed_catalog = None
    download_manager = _web_catalog.DownloadManager()
    hash_manager = _hash.HashManager()
    release_fetch_manager = _release_mgr.ReleaseFetchManager()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The SSE event bus accepts publishes from worker threads -
        # capture the loop now so cross-thread publishes can hop in
        # via call_soon_threadsafe.
        event_bus.attach(asyncio.get_running_loop())
        if parsed_catalog is not None:
            download_manager.start(parsed_catalog, catalog_cache_dir)
        # The hash manager always starts -- it operates on
        # ``image_root``, which exists for every bty-web shape
        # (appliance, container, dev). Default parallelism is 1
        # so a Pi-class box doesn't get hammered if multiple big
        # images need importing at once.
        hash_manager.start(resolved_image_root, state_path=state_path)
        # Release-fetch manager: powers the trackable
        # /boot/releases endpoints (and the /ui/boot page's
        # progress + cancel buttons). Default parallelism is 1
        # because fetching two GitHub releases in parallel is
        # operator-confusing and bandwidth-saturating.
        release_fetch_manager.start(resolved_boot_root, state_path=state_path)
        # Auto-import: ensure every dir-scan file under
        # ``resolved_image_root`` has a catalog_entries row keyed by
        # ``bty_image_ref = sha256("file://<rel-path>")``, then enqueue
        # the HashManager for any without a known ``disk_image_sha``.
        # The row exists immediately (so the operator can bind to it
        # without waiting for hashing); the HashManager populates
        # ``disk_image_sha`` in the background and the PXE flash path
        # becomes viable once the hash lands.
        #
        # Idempotent: ``INSERT OR IGNORE`` skips rows whose src is
        # already in the table (the operator may have curated the
        # file via the UI; preserve their description, etc.).
        # Operator-initiated work queues behind these jobs by FIFO
        # order; parallelism cap (default 1) keeps a Pi responsive.
        _auto_import_dir_scan_rows()
        for img in images.list_images(resolved_image_root):
            if img.sha256 is None:
                # ``FileNotFoundError`` -- file vanished between the
                # ``list_images`` scan and the enqueue (harmless).
                # ``ValueError`` -- the traversal guard in
                # ``HashManager.enqueue`` rejects suspect basenames;
                # ``list_images`` shouldn't surface any but a freshly-
                # created file named ``..`` (impossible) or ``.``
                # (likewise) would crash startup without this
                # suppression.
                with contextlib.suppress(FileNotFoundError, ValueError):
                    await hash_manager.enqueue(img.name)
        try:
            yield
        finally:
            if parsed_catalog is not None:
                await download_manager.stop()
            await hash_manager.stop()
            await release_fetch_manager.stop()

    def _auto_import_dir_scan_rows() -> None:
        """Insert a ``catalog_entries`` row for every dir-scan file
        under ``resolved_image_root`` that doesn't already have one.

        Src shape: ``file://<rel-path>`` (path relative to image
        root; root-relocation invariant, so moving the image-store
        disk between appliances does not change refs). Ref:
        ``sha256(canonicalise_src(src))`` from
        ``bty.catalog.image_ref_for_src``. ``disk_image_sha`` is
        populated when the file has a ``.sha256`` sidecar already;
        otherwise it stays NULL until the HashManager finishes the
        background hash.

        Idempotent via ``INSERT OR IGNORE``: operator-curated rows
        from ``POST /catalog/entries`` (or the UI form) that target
        the same src keep their descriptions / sha_url intact.
        ``UPDATE`` on the disk_image_sha column for rows that newly
        gained a sidecar between bty-web restarts -- without this,
        a sidecar that landed while bty-web was down wouldn't make
        the entry bindable.
        """
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            for img in images.list_images(resolved_image_root):
                try:
                    rel = img.path.relative_to(resolved_image_root)
                except ValueError:
                    # Symlink that escaped the root, or an image_root
                    # that got remounted mid-scan -- skip rather than
                    # auto-import.
                    continue
                src = "file://" + rel.as_posix()
                try:
                    ref = _catalog.image_ref_for_src(src)
                except ValueError:
                    continue  # path can't be canonicalised; skip silently
                conn.execute(
                    "INSERT OR IGNORE INTO catalog_entries "
                    "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                    "format, size_bytes, description, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ref,
                        src,
                        img.sha256,
                        img.name,
                        None,
                        img.format,
                        img.size_bytes,
                        None,
                        now,
                    ),
                )
                # If the file has a sidecar that landed since the row
                # was last seen, propagate it. ``COALESCE`` keeps any
                # existing value to avoid overwriting an operator-
                # pinned ``disk_image_sha`` with a stale read.
                if img.sha256 is not None:
                    conn.execute(
                        "UPDATE catalog_entries "
                        "SET disk_image_sha = COALESCE(disk_image_sha, ?) "
                        "WHERE bty_image_ref = ?",
                        (img.sha256, ref),
                    )
            conn.commit()

    jinja = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        # Autoescape only HTML (UI) templates; the iPXE ``.j2`` files
        # are plain text and would be mangled by escaping.
        autoescape=select_autoescape(enabled_extensions=("html",)),
        keep_trailing_newline=True,
    )

    _db.init_db(state_path)

    app = FastAPI(title="bty-web", version=bty.__version__, lifespan=_lifespan)

    # Server-signed session cookie via Starlette's SessionMiddleware.
    # Cookie name kept as ``bty-token`` so existing operator scripts
    # (and the PXE chain test) that captured Set-Cookie don't break.
    # ``same_site="strict"`` blocks cross-site cookie attachment;
    # browsers won't auto-send the cookie on third-party requests.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=_SESSION_MAX_AGE,
        same_site="strict",
        https_only=False,  # appliance is plain HTTP on a homelab segment
    )

    # Vendored client-side assets (Bootstrap CSS, HTMX, htmx-ext-sse)
    # ship inside the wheel so the appliance has no runtime CDN
    # dependency. See ``_static/README.md`` for provenance.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render_machines_tbody() -> str:
        """Render the rows fragment used by /ui/machines and the SSE stream."""
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        machines = [_ui._row_to_dict(r) for r in rows]
        return jinja.get_template("ui/_machines_tbody.html").render(machines=machines)

    def render_dashboard_counts() -> str:
        """Render the dashboard counter cards as a swappable fragment."""
        with _db.open_db(state_path) as conn:
            machine_count = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
            discovered_count = conn.execute(
                "SELECT COUNT(*) FROM machines WHERE bty_image_ref IS NULL"
            ).fetchone()[0]
        image_count = len(images.list_images(resolved_image_root))
        # PXE proxy-DHCP state for the 4th tile. Three flavours: inactive
        # (no config), active+healthy (config present, NIC present),
        # active+nic-gone (config bound to a NIC that's no longer in
        # /sys/class/net -- USB ethernet adapters or systemd predictable-
        # name churn). The tile links to /ui/settings; state changes from
        # there are not pushed via SSE (the bus fires on machine + image
        # mutations only), so the tile refreshes on page reload.
        pxe_state = _sysconfig.pxe_state()
        return jinja.get_template("ui/_dashboard_counts.html").render(
            machine_count=machine_count,
            discovered_count=discovered_count,
            image_count=image_count,
            pxe=pxe_state.config,
            pxe_iface_present=pxe_state.iface_present,
        )

    def publish_state_changed() -> None:
        """Publish fresh snapshots of every SSE-driven UI fragment.

        Mutating routes call this on commit. Subscribers receive all
        events on the same stream and route to elements with matching
        ``sse-swap`` attributes - the machines table swaps the
        ``machines-update`` event, the dashboard counters swap the
        ``dashboard-counts`` event.
        """
        event_bus.publish(MachineEvent(name="machines-update", html=render_machines_tbody()))
        event_bus.publish(MachineEvent(name="dashboard-counts", html=render_dashboard_counts()))

    # Back-compat alias - older internal call sites use this name.
    publish_machines_changed = publish_state_changed

    # ----- Open routes (no auth) ------------------------------------------

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        """Bare-host hit (``http://bty-server:8080/``) lands the
        operator at the login screen. Already-authed visitors get
        bounced from there to the dashboard via ``ui_login_form``."""
        return Response(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/ui/login"},
        )

    @app.get("/healthz", response_model=_models.HealthResponse)
    def healthz() -> _models.HealthResponse:
        return _models.HealthResponse()

    @app.get("/version", response_model=_models.VersionResponse)
    def version() -> _models.VersionResponse:
        return _models.VersionResponse(version=bty.__version__)

    @app.get("/pxe-bootstrap.ipxe", response_class=PlainTextResponse)
    def pxe_bootstrap(request: Request) -> str:
        # Fixed iPXE script that PXE clients hit after their iPXE binary
        # loads (second-stage DHCP from dnsmasq points here). It chains
        # to the per-MAC plan endpoint using the Host header, so the
        # client always loops back to whichever name/IP/port the operator
        # used to reach this server. Open route: PXE clients have no
        # tokens.
        host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 8080}")
        template = jinja.get_template("pxe_bootstrap.j2")
        return template.render(host=host)

    @app.get("/pxe/{mac}", response_class=PlainTextResponse)
    def pxe(mac: str, request: Request) -> str:
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            if row is None:
                # Auto-discovery: record an unassigned machine so the
                # operator can see this MAC in /machines and decide
                # what to do with it. ``boot_policy='tui'`` makes
                # the unknown MAC chain into the live env in
                # interactive mode (bty-tui) - "bty-on-a-USB but
                # over the network" - so first contact is useful
                # without prior server-side configuration.
                conn.execute(
                    """
                    INSERT INTO machines
                        (mac, boot_policy,
                         discovered_at, last_seen_at, last_seen_ip,
                         created_at, updated_at)
                    VALUES (?, 'tui', ?, ?, ?, ?, ?)
                    """,
                    (normalised, now, now, client_ip, now, now),
                )
                # First /pxe contact = the moment a machine becomes
                # visible to the operator. Worth a row in the audit
                # log so they can see "this MAC first checked in at
                # X" without paging through stale records. Only
                # logged on the discovery path (the else branch
                # below is "we've seen this MAC before" -- too
                # noisy to log every chain into the live env).
                _log_event(
                    conn,
                    kind="machine.discovered",
                    summary=f"{normalised} first contacted /pxe from {client_ip or 'unknown IP'}",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                )
                conn.commit()
                row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            else:
                conn.execute(
                    """
                    UPDATE machines
                    SET last_seen_at = ?,
                        last_seen_ip = ?,
                        discovered_at = COALESCE(discovered_at, ?),
                        updated_at = ?
                    WHERE mac = ?
                    """,
                    (now, client_ip, now, now, normalised),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()

        assert row is not None
        machine = dict(row)
        publish_machines_changed()
        # Boot-policy decision tree (highest priority first):
        #   - boot_policy == 'tui'                   -> live env in interactive mode
        #   - boot_policy == 'flash' AND image       -> live env auto-flash
        #   - boot_policy == 'local' AND image       -> sanboot (already provisioned)
        #   - else (no image, boot_policy == 'local')-> sanboot fallback
        # Completion signal (POST /pxe/{mac}/done) updates last_flashed_at
        # but never flips boot_policy - operator does that explicitly.
        host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 8080}")
        if machine.get("boot_policy") == "tui":
            template = jinja.get_template("ipxe_tui.j2")
            return template.render(mac=normalised, machine=machine, host=host)
        ref = machine.get("bty_image_ref")
        if ref and machine.get("boot_policy") == "flash":
            # Emit the iPXE flash chain whose URL ends in
            # ``/images/<ref>/<name>``. The serve_image route does
            # eager cache-through (Option A): if the bound entry's
            # disk_image_sha is unknown the route fetches upstream
            # synchronously before sendfile-ing to the live env's
            # curl. Operators who care about first-flash latency
            # pre-warm the cache via the /ui/images Fetch button.
            image_name = _flash_target_for_ref(str(ref))
            if image_name is not None:
                template = jinja.get_template("ipxe_flash.j2")
                return template.render(
                    mac=normalised,
                    machine=machine,
                    host=host,
                    image_name=image_name,
                    flash_sha=str(ref),
                )
        if ref:
            template = jinja.get_template("ipxe.j2")
            return template.render(mac=normalised, machine=machine)
        template = jinja.get_template("ipxe_unknown.j2")
        return template.render(mac=normalised, machine=machine)

    @app.post(
        "/pxe/{mac}/done",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def pxe_done(mac: str, request: Request) -> Response:
        # Open route: the live env hits this from the PXE-booted target,
        # which has no token. Trust model: bty-web is for trusted
        # networks (homelab / CI), not the open internet - same as the
        # other ``/pxe/*`` endpoints.
        #
        # Only updates ``last_flashed_at`` and ``updated_at``. Does NOT
        # touch ``boot_policy``: if the operator wants the box to stop
        # reflashing on every boot they flip the policy themselves.
        # This decoupling is deliberate - per-job CI cadence wants
        # boot_policy=flash to stay flash across reflashes.
        normalised = _normalise_mac(mac)
        now = _now_iso()
        client_ip = _client_ip(request)
        with _db.open_db(state_path) as conn:
            cur = conn.execute(
                "UPDATE machines SET last_flashed_at = ?, updated_at = ? WHERE mac = ?",
                (now, now, normalised),
            )
            if cur.rowcount > 0:
                _log_event(
                    conn,
                    kind="machine.flashed",
                    summary=f"{normalised} signalled flash completion",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        publish_machines_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---------- release-fetch manager ---------------------------
    # Registered BEFORE the ``GET /boot/{name}`` catch-all so
    # ``/boot/releases`` doesn't get eaten as a missing artefact name.
    # Powers the trackable "Fetch from GitHub releases" action on
    # /ui/boot: ``POST /boot/releases`` enqueues, ``GET /boot/releases``
    # polls, ``DELETE /boot/releases/{tag}`` cancels.

    @app.get("/boot/releases", dependencies=[Depends(require_auth)])
    async def list_release_fetches() -> dict[str, Any]:
        states = await release_fetch_manager.list()
        return {
            "boot_root": str(resolved_boot_root),
            "max_parallel": release_fetch_manager.max_parallel,
            "fetches": [s.to_dict() for s in states],
        }

    @app.post(
        "/boot/releases",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def enqueue_release_fetch(body: _models.ReleaseFetchRequest) -> dict[str, Any]:
        state = await release_fetch_manager.enqueue(body.tag)
        return state.to_dict()

    @app.delete("/boot/releases/{tag}", dependencies=[Depends(require_auth)])
    async def cancel_release_fetch(tag: str) -> dict[str, Any]:
        state = await release_fetch_manager.cancel(tag)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active release fetch for tag {tag!r}",
            )
        return state.to_dict()

    # ---------- event log ---------------------------------------
    # Slim audit log of operator + machine activity. Backs the
    # /ui/events page + per-subject embedded lists on
    # /ui/machines/{mac} and /ui/images.

    @app.get("/events", dependencies=[Depends(require_auth)])
    def list_events_endpoint(
        kind: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        source_ip: str | None = None,
        failed: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        with _db.open_db(state_path) as conn:
            events = _list_events(
                conn,
                kind=kind,
                subject_kind=subject_kind,
                subject_id=subject_id,
                actor=actor,
                source_ip=source_ip,
                failed_only=bool(failed),
                before_id=before_id,
                limit=limit,
            )
        return {"events": [e.to_dict() for e in events]}

    @app.get("/boot/{name}", include_in_schema=False)
    def boot_artifact(name: str) -> FileResponse:
        # Live-env artifacts (kernel + initrd + squashfs) the iPXE chain
        # references. Open route: PXE clients have no token. Operator
        # populates ``boot_root`` via the UI's "fetch latest release"
        # action (D-3b) - until the dir has files, this returns 404
        # and the appliance is non-functional for boot_policy=flash.
        return _serve_safe_file(resolved_boot_root, name)

    def _serve_image_by_key(key: str) -> FileResponse:
        """Resolve ``key`` (filename OR 64-hex ID) to bytes.

        Resolution order:

          1. Literal filename under ``image_root`` -- legacy path.
          2. 64-hex ID through :func:`_resolve_image_for_key`, which
             handles ``bty_image_ref`` lookups (with eager cache-through
             on miss) and falls back to bare ``disk_image_sha`` lookups
             against the cache + dir-scan.
        """
        try:
            return _serve_safe_file(resolved_image_root, key)
        except HTTPException:
            pass
        if images.is_sha256_hex(key.lower()):
            resolved_path = _resolve_image_for_key(key)
            if resolved_path is not None:
                return FileResponse(resolved_path, media_type="application/octet-stream")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such image: {key}",
        )

    def _image_name_for_sha(sha: str) -> str | None:
        """Find a display name for the image whose bytes hash to
        ``sha``. Used by the iPXE flash-template renderer to build
        ``/images/<sha>/<name>`` URLs that preserve the filename's
        format-by-extension semantics on the live-env client side.

        Resolution: dir-scan first (operator-supplied images take
        precedence), then catalog manifest (cached or upstream).
        Returns ``None`` if no entry matches the SHA -- caller's
        responsibility to fall back to a safe default.
        """
        sha = sha.lower()
        for img in images.list_images(resolved_image_root):
            if img.sha256 == sha:
                return img.name
        if parsed_catalog is not None:
            for entry in parsed_catalog.entries:
                if entry.sha256 == sha:
                    return entry.name
        return None

    def _flash_target_for_ref(ref: str) -> str | None:
        """Resolve a ``bty_image_ref`` to a display name so the iPXE
        flash template can build the ``/images/<ref>/<name>`` URL.

        Returns the entry's ``name`` (preserves format-by-extension
        on the live-env side) or ``None`` for an orphaned binding (no
        catalog row matches this ref). The URL uses the ref itself,
        not the content sha -- :func:`_resolve_image_for_key` does
        the cache-through on the fly when the bound entry's
        ``disk_image_sha`` is still NULL (eager fetch-then-serve,
        Option A in the v0.11.0 design notes).
        """
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT name FROM catalog_entries WHERE bty_image_ref = ?",
                (ref,),
            ).fetchone()
        if row is None:
            return None
        return str(row["name"])

    def _resolve_image_for_key(key: str) -> Path | None:
        """Resolve a 64-hex key (bty_image_ref or disk_image_sha) to a
        local file path. Triggers eager cache-through if the bound
        catalog row's bytes aren't on disk yet.

        Resolution order:

        1. ``key`` as ``bty_image_ref``: look up catalog_entries.
           - disk_image_sha known + cache file present -> cache path
           - disk_image_sha known + dir-scan file present -> local
           - src is file:// + file exists -> local (HashManager will
             populate disk_image_sha asynchronously)
           - else (remote src, no cache): fetch src -> cache,
             UPDATE row.disk_image_sha, return new cache path.
             Pre-pinned-sha mismatch on fetch returns ``None`` (the
             caller surfaces a 502 and logs a sha_mismatch event).
        2. ``key`` as raw disk_image_sha: legacy path -- cache lookup
           then dir-scan fallback for entries whose ref-keyed row was
           deleted but the cache file lingers.
        """
        key_lower = key.lower()
        # (1) ref lookup.
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT bty_image_ref, src, disk_image_sha "
                "FROM catalog_entries WHERE bty_image_ref = ?",
                (key_lower,),
            ).fetchone()
        if row is not None:
            sha = row["disk_image_sha"]
            src = str(row["src"])
            ref = str(row["bty_image_ref"])
            if sha:
                cached = catalog_cache_dir / sha
                if cached.is_file():
                    return cached
            if src.startswith("file://"):
                rel = src[len("file://") :]
                local = resolved_image_root / rel
                if local.is_file():
                    return local
                return None
            # Remote src, no cache yet -- cache-through (Option A).
            try:
                cached, computed_sha = _catalog.fetch_src_to_cache(
                    src,
                    catalog_cache_dir,
                    expected_sha=sha,
                )
            except _catalog.CatalogError as exc:
                with _db.open_db(state_path) as conn:
                    _log_event(
                        conn,
                        kind="catalog.fetch.sha_mismatch",
                        summary=f"cache-through {src!r}: {exc}",
                        subject_kind="catalog",
                        subject_id=ref,
                        actor="pxe-client",
                        details={"src": src, "error": str(exc)},
                    )
                    conn.commit()
                return None
            with _db.open_db(state_path) as conn:
                conn.execute(
                    "UPDATE catalog_entries SET disk_image_sha = ? WHERE bty_image_ref = ?",
                    (computed_sha, ref),
                )
                _log_event(
                    conn,
                    kind="catalog.cache.populated",
                    summary=f"cache-through populated for {src!r}",
                    subject_kind="catalog",
                    subject_id=ref,
                    actor="pxe-client",
                    details={"src": src, "disk_image_sha": computed_sha},
                )
                conn.commit()
            return cached
        # (2) sha lookup -- cache + dir-scan fallback.
        if images.is_sha256_hex(key_lower):
            cached = catalog_cache_dir / key_lower
            if cached.is_file():
                return cached
            for img in images.list_images(resolved_image_root):
                if img.sha256 == key_lower:
                    return img.path
        return None

    @app.get("/images/{key}", include_in_schema=False)
    def serve_image(key: str) -> FileResponse:
        """Serve image bytes by filename OR by SHA-256.

        Same trust model as /boot. The live env curls this to
        get the bytes that ``bty.image_url`` points at.
        """
        return _serve_image_by_key(key)

    @app.get("/images/{key}/{name:path}", include_in_schema=False)
    def serve_image_with_name(key: str, name: str) -> FileResponse:
        """``/images/<sha>/<filename>`` form. The ``key`` (SHA-256)
        binds the bytes; ``name`` is purely decorative -- it lets
        clients that derive image format from URL filename
        extension (the live env's ``bty-flash-on-boot``,
        ``bty.flash.probe_image_url``) keep working when bty-web
        emits SHA-keyed URLs. The server ignores ``name`` for the
        actual lookup; it's there so ``Path(url.path).name``
        returns ``foo.img.zst`` instead of a bare 64-hex SHA.
        """
        del name  # informational only; lookup is by ``key``
        return _serve_image_by_key(key)

    @app.get(
        "/events/machines",
        dependencies=[Depends(require_auth)],
        include_in_schema=False,
    )
    async def events_machines() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            # Send current snapshots on subscribe so each page is
            # immediately consistent without a separate fetch. Each
            # event is routed by the htmx-ext-sse client to whichever
            # element on the page declares ``sse-swap=<name>``.
            yield sse_format("machines-update", render_machines_tbody())
            yield sse_format("dashboard-counts", render_dashboard_counts())
            async for event in event_bus.subscribe():
                yield sse_format(event.name, event.html)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ----- Protected routes (session cookie required) -----------------------------

    @app.get(
        "/machines",
        response_model=list[_models.Machine],
        dependencies=[Depends(require_auth)],
    )
    def list_machines() -> list[_models.Machine]:
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        return [_row_to_machine(r) for r in rows]

    @app.get(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_auth)],
    )
    def get_machine(mac: str) -> _models.Machine:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        return _row_to_machine(row)

    @app.put(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_auth)],
    )
    def upsert_machine(mac: str, body: _models.MachineUpsert, request: Request) -> _models.Machine:
        normalised = _normalise_mac(mac)
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            existing = conn.execute(
                "SELECT created_at FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            conn.execute(
                """
                INSERT INTO machines
                    (mac, bty_image_ref, hostname, boot_policy,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    bty_image_ref = excluded.bty_image_ref,
                    hostname     = excluded.hostname,
                    boot_policy  = excluded.boot_policy,
                    updated_at   = excluded.updated_at
                """,
                (
                    normalised,
                    body.bty_image_ref,
                    body.hostname,
                    body.boot_policy,
                    created_at,
                    now,
                ),
            )
            _log_event(
                conn,
                kind="machine.created" if existing is None else "machine.upserted",
                summary=(f"{normalised} created" if existing is None else f"{normalised} updated"),
                subject_kind="machine",
                subject_id=normalised,
                actor="operator",
                source_ip=_client_ip(request),
                details={
                    "bty_image_ref": body.bty_image_ref,
                    "boot_policy": body.boot_policy,
                    "hostname": body.hostname,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        assert row is not None
        publish_machines_changed()
        return _row_to_machine(row)

    @app.delete(
        "/machines/{mac}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_auth)],
    )
    def delete_machine(mac: str, request: Request) -> Response:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            if cur.rowcount > 0:
                _log_event(
                    conn,
                    kind="machine.deleted",
                    summary=f"{normalised} deleted",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="operator",
                    source_ip=_client_ip(request),
                )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        publish_machines_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/images", response_model=list[_models.ImageEntry])
    def list_images_endpoint(request: Request) -> list[_models.ImageEntry]:
        """Unified catalog listing.

        Each entry carries a single ``url``: server URL for
        cached / imported / dir-scan-with-sidecar images,
        upstream URL for manifest entries that have not been
        cached yet. The client just flashes from ``url`` --
        no need to know about manifests, sidecars, or cache.

        Open route: the bty-tui-on-PXE flow needs to enumerate
        the catalog without first bootstrapping auth. The
        byte-serving route ``GET /images/{name}`` is already
        open. Same homelab-network trust model as /pxe / /boot.
        """
        unified = _list_unified_images()
        host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 8080}")
        scheme = request.url.scheme or "http"
        out: list[_models.ImageEntry] = []
        for u in unified:
            if u.cached:
                # Local file or cached manifest blob -- bty-web
                # serves the bytes. URL shape is
                # ``/images/<sha>/<name>``: the SHA binds the
                # content; the trailing name is decorative so a
                # client that derives format from URL filename
                # extension (the live env's bty-flash-on-boot,
                # ``bty.flash.probe_image_url``) gets ``foo.img.zst``
                # instead of a bare 64-hex digest. The server
                # route ignores ``<name>`` for the lookup.
                if u.sha256 is None:
                    continue  # cached + no sha is impossible; defensive
                url = f"{scheme}://{host}/images/{u.sha256}/{u.names[0]}"
            else:
                # Not cached: the client streams directly from
                # upstream. Try manifest source first, then ``url``
                # source (operator-curated catalog_entries row).
                # Skip dir-scan-only entries with no sha + no
                # upstream URL -- the auto-import on startup will
                # hash them and they'll re-surface as cached in the
                # next listing.
                upstream = next(
                    (s.location for s in u.sources if s.kind in ("manifest", "url")),
                    None,
                )
                if upstream is None:
                    continue
                url = upstream
            out.append(
                _models.ImageEntry(
                    name=u.names[0],
                    format=u.format or "",
                    size_bytes=u.size_bytes or 0,
                    url=url,
                    ref=u.sha256[:12] if u.sha256 else None,
                    cached=u.cached,
                )
            )
        # ``.bri`` (bty Remote Image) descriptors are deliberately
        # NOT surfaced here. ``.bri`` is the bty-usb / bty-tui ad-hoc
        # local-catalog format -- a tiny pointer file an operator
        # drops next to their .img.gz files for quick "flash this
        # URL" workflows. bty-web is the SHA-keyed managed-catalog
        # model: machine bindings store ``bty_image_ref``, not URL
        # pointers, so a ``.bri`` can't bind to a machine without
        # being fetched + hashed first. Mixing the two surfaces here
        # would invite the operator to bind a ``.bri`` they then
        # can't actually flash.
        return out

    @app.get("/catalog.toml", response_class=PlainTextResponse)
    def list_catalog_toml(request: Request) -> Response:
        """Serve the unified image catalog as a TOML manifest matching
        the ``bty.catalog.Catalog`` schema (``version=1``, ``[[images]]``
        entries with ``name``/``src``/``sha256``/``format``/``size_bytes``).

        Same set of rows as ``GET /images`` (manifest + dir-scan +
        operator-curated DB entries; ``.bri`` deliberately excluded),
        but serialised so ``bty-tui --catalog`` clients can consume it
        with the same code path they use for static files hosted on
        e.g. GitHub. Open route, same trust model as ``/images``.

        Implemented as ``application/toml`` so a curl-then-eyeball
        round-trip shows a human-readable manifest, not a binary blob.
        Entries without a sha256 are skipped (the catalog schema
        requires one); a future cache-hashing pass over dir-scan
        files will surface them.
        """
        unified = _list_unified_images()
        host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 8080}")
        scheme = request.url.scheme or "http"
        lines: list[str] = ["version = 1", ""]
        for u in unified:
            if u.sha256 is None:
                # Catalog manifest schema requires a sha; skip dir-scan
                # entries that haven't been hashed yet.
                continue
            if u.cached:
                src = f"{scheme}://{host}/images/{u.sha256}/{u.names[0]}"
            else:
                upstream = next(
                    (s.location for s in u.sources if s.kind in ("manifest", "url")),
                    None,
                )
                if upstream is None:
                    continue
                src = upstream
            # tomllib basic-string escaping: backslash + double-quote.
            # All other fields are sourced from validated state and
            # are either plain ASCII identifiers or already-quoted URLs.
            name_quoted = u.names[0].replace("\\", "\\\\").replace('"', '\\"')
            src_quoted = src.replace("\\", "\\\\").replace('"', '\\"')
            lines.append("[[images]]")
            lines.append(f'name = "{name_quoted}"')
            lines.append(f'src = "{src_quoted}"')
            lines.append(f'sha256 = "{u.sha256}"')
            if u.format:
                lines.append(f'format = "{u.format}"')
            if u.size_bytes is not None:
                lines.append(f"size_bytes = {u.size_bytes}")
            lines.append("")
        body = "\n".join(lines).rstrip() + "\n"
        return PlainTextResponse(content=body, media_type="application/toml")

    @app.put(
        "/images/{name}",
        dependencies=[Depends(require_auth)],
        include_in_schema=False,
    )
    async def upload_image(name: str, request: Request) -> dict[str, object]:
        """Stream-upload an image into the image root.

        Body is the raw image bytes (``Content-Type:
        application/octet-stream``). Atomic via a ``.partial`` sibling
        + rename. Returns the resolved path + bytes-written on
        success; replaces an existing file with the same name.

        Auto-enqueues a hash job after the write so the new image
        appears in the unified ``/images`` listing on the next
        request without waiting for a server restart. The
        HashManager runs a single worker by default; the upload
        returns immediately and the operator can watch progress
        via ``/catalog/hashes``.

        Failure path logs ``image.upload_failed`` so the audit log
        is symmetric with the success path's ``image.uploaded``.
        Operators scanning /ui/events see "this upload was tried
        and rejected" with the underlying error.
        """
        try:
            result = await _stream_upload(request, resolved_image_root, name)
        except HTTPException as exc:
            with _db.open_db(state_path) as conn:
                _log_event(
                    conn,
                    kind="image.upload_failed",
                    summary=f"image {name!r} upload failed: {exc.detail}",
                    subject_kind="image",
                    subject_id=name,
                    actor="operator",
                    source_ip=_client_ip(request),
                    details={"status_code": exc.status_code, "error": str(exc.detail)},
                )
                conn.commit()
            raise
        except OSError as exc:
            # Disk full / read-only filesystem / etc. ``_stream_upload``
            # already cleaned up the .partial; we just record the
            # failure and let it propagate to a 500 response.
            with _db.open_db(state_path) as conn:
                _log_event(
                    conn,
                    kind="image.upload_failed",
                    summary=f"image {name!r} upload failed: {type(exc).__name__}: {exc}",
                    subject_kind="image",
                    subject_id=name,
                    actor="operator",
                    source_ip=_client_ip(request),
                    details={"status_code": 500, "error": f"{type(exc).__name__}: {exc}"},
                )
                conn.commit()
            raise
        # Image catalog count changes; refresh the dashboard fragment.
        publish_state_changed()
        # Trigger an import for the just-uploaded file UNLESS it's
        # itself a sidecar (operators occasionally upload the
        # ``<file>.sha256`` after the image): hashing a sidecar
        # would be nonsense + would write a ``.sha256.sha256``
        # cousin. ``list_images`` already filters sidecars; the
        # auto-import lifespan would have skipped this entry, so
        # we mirror that guard here.
        if not name.endswith(".sha256"):
            # ``_stream_upload`` raised earlier if the write
            # failed, so FileNotFoundError shouldn't happen here --
            # guarded for safety.
            with contextlib.suppress(FileNotFoundError):
                await hash_manager.enqueue(name)
            with _db.open_db(state_path) as conn:
                _log_event(
                    conn,
                    kind="image.uploaded",
                    summary=f"image {name!r} uploaded ({result['size_bytes']} bytes)",
                    subject_kind="image",
                    subject_id=name,
                    actor="operator",
                    source_ip=_client_ip(request),
                    details={"size_bytes": result["size_bytes"]},
                )
                conn.commit()
        return result

    @app.put(
        "/boot/{name}",
        dependencies=[Depends(require_auth)],
        include_in_schema=False,
    )
    async def upload_boot_artifact(name: str, request: Request) -> dict[str, object]:
        """Stream-upload a live-env artefact into the boot dir.

        Same shape as ``PUT /images/{name}`` - the live trio
        (vmlinuz / initrd / squashfs) goes here so the iPXE chain
        finds it via the open ``GET /boot/{name}`` route.
        """
        return await _stream_upload(request, resolved_boot_root, name)

    # Browser UI under /ui/ (Jinja + Bootstrap, cookie-auth).

    def _load_db_catalog_split() -> tuple[
        tuple[_catalog.CatalogEntry, ...],
        tuple[images.UnifiedImage, ...],
    ]:
        """Load operator-curated catalog rows from state.db, split
        by whether they carry a sha256:

        - Sha-keyed rows -> :class:`bty.catalog.CatalogEntry` for
          the SHA-keyed merge pipeline (so they dedupe with
          dir-scan files / manifest entries that share the hash).
        - URL-only rows (operator added without a sha_url) ->
          :class:`bty.images.UnifiedImage` with ``sha256=None``,
          surfaced verbatim. Flashable via the URL streaming
          pipeline; not bindable to a machine.
        """
        with _db.open_db(state_path) as conn:
            # ``ORDER BY added_at`` matches the ``list_catalog_entries``
            # API endpoint so the UI's catalog table renders in the
            # same insertion order regardless of which code path
            # populated the page (display merge vs. raw API listing).
            # Without it, SQLite returns rows in unspecified order
            # and a page-refresh can shuffle the table.
            rows = conn.execute(
                "SELECT disk_image_sha, name, src, format, size_bytes, description "
                "FROM catalog_entries ORDER BY added_at"
            ).fetchall()
        sha_keyed: list[_catalog.CatalogEntry] = []
        url_only: list[images.UnifiedImage] = []
        for row in rows:
            if row["disk_image_sha"]:
                sha_keyed.append(
                    _catalog.CatalogEntry(
                        name=row["name"],
                        src=row["src"],
                        sha256=row["disk_image_sha"],
                        format=row["format"],
                        size_bytes=row["size_bytes"],
                        description=row["description"],
                    )
                )
            else:
                url_only.append(
                    images.UnifiedImage(
                        sha256=None,
                        names=(row["name"],),
                        format=row["format"],
                        size_bytes=row["size_bytes"],
                        sources=(images.ImageSource(kind="url", location=row["src"]),),
                        cached=False,
                    )
                )
        return tuple(sha_keyed), tuple(url_only)

    def _list_unified_images() -> list[images.UnifiedImage]:
        """SHA-keyed merge of dir-scan + catalog manifest entries +
        operator-curated catalog_entries rows.

        Recomputed per call so an operator who drops new files into
        BTY_IMAGE_ROOT (or whose catalog fetch just completed, or who
        added a URL via the UI) sees the change on the next page load
        without restarting bty-web.
        """
        manifest_entries = parsed_catalog.entries if parsed_catalog else ()
        sha_keyed, url_only = _load_db_catalog_split()
        merged = images.merge_with_catalog(
            resolved_image_root,
            (*manifest_entries, *sha_keyed),
            catalog_cache_dir,
        )
        return [*merged, *url_only]

    _ui.register_ui_routes(
        app,
        jinja=jinja,
        state_path=state_path,
        service_user=service_user,
        image_root=resolved_image_root,
        boot_root=resolved_boot_root,
        publish_machines_changed=publish_machines_changed,
        list_unified_images=_list_unified_images,
    )

    # ---------- operator-curated catalog entries -----------------------
    # ``catalog_entries`` table in state.db backs a UI form where the
    # operator pastes ``image-url`` + optional ``sha-url`` and hits
    # Add. The shape mirrors a catalog.toml manifest entry, so once
    # written the row flows through ``merge_with_catalog`` and shows
    # in the operator's catalog page like any other entry. No
    # filesystem dance; no TOML editing.

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
          digest if the manifest carries one entry). The entry is
          SHA-keyed and can bind to a machine.
        - If ``sha_url`` is null: the entry is URL-only. Flashable
          via the URL streaming pipeline; not bindable to a machine
          (machine binding is content-addressed; needs a known SHA).

        - HEADs ``image_url`` for ``Content-Length`` (best-effort).
        - Inserts a row keyed by image_url.

        ``oras://`` short-circuit: when ``image_url`` starts with
        ``oras://``, the server runs ``bty.oras.resolve_ref`` at add
        time. The picked layer's digest becomes the entry's sha256
        (= machine-bindable), the layer's title annotation becomes
        ``name``, the layer's declared size becomes ``size_bytes``,
        and ``format`` is detected from the title. ``sha_url`` is
        ignored for oras refs (the manifest is authoritative).

        409 if a row with the same image_url already exists.
        """
        # Variables shared across the oras / http branches. Declared
        # up front so mypy sees a single binding (the oras branch
        # narrows ``sha256`` to ``str``, which would clash with the
        # ``str | None`` re-declaration the http branch had previously).
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
                        kind="catalog.entry.add_failed",
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
            now = _now_iso()
            try:
                bty_image_ref = _catalog.image_ref_for_src(body.image_url)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid image_url: {exc}",
                ) from exc
            with _db.open_db(state_path) as conn:
                try:
                    conn.execute(
                        "INSERT INTO catalog_entries "
                        "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                        "format, size_bytes, description, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bty_image_ref,
                            body.image_url,
                            sha256,
                            name,
                            None,
                            fmt,
                            size_bytes,
                            None,
                            now,
                        ),
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
                        kind="catalog.entry.add_failed",
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
        size_bytes = _head_content_length(body.image_url)
        now = _now_iso()
        try:
            bty_image_ref = _catalog.image_ref_for_src(body.image_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid image_url: {exc}",
            ) from exc
        with _db.open_db(state_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO catalog_entries "
                    "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                    "format, size_bytes, description, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bty_image_ref,
                        body.image_url,
                        sha256,
                        name,
                        body.sha_url,
                        fmt,
                        size_bytes,
                        None,
                        now,
                    ),
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
                "SELECT bty_image_ref, src, disk_image_sha, name, sha_url, format, size_bytes, "
                "description, added_at "
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
        which is operator-hostile; query param is cleaner."""
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
        side fetcher the TUI uses applies here.

        **Metadata-only**. Bytes are NOT fetched at import time; each
        imported entry surfaces in ``/images`` as ``cached=False``.
        The operator's "Fetch" button (or ``POST /catalog/downloads``)
        materialises bytes on demand.

        Per-entry behaviour:

        - If the TOML entry carries a ``sha256``, it's inserted as-is.
        - Else if the entry's ``src`` is ``oras://``, the registry
          manifest is resolved at import time to get the layer digest
          (= sha256). Errors propagate into the per-entry ``errors``
          list, not a request-level 4xx.
        - Else (http(s):// URL with no sha): the entry is URL-only
          (``sha256=NULL``). Flashable; not machine-bindable until
          hashed.

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

        imported = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            for entry in parsed.entries:
                sha = entry.sha256
                fmt = entry.format
                size_bytes = entry.size_bytes
                if sha is None and entry.src.startswith("oras://"):
                    try:
                        resolved = _oras.resolve_ref(entry.src)
                    except _oras.OrasError as exc:
                        errors.append({"name": entry.name, "error": f"oras: {exc}"})
                        continue
                    sha = resolved.digest.removeprefix("sha256:")
                    if size_bytes is None:
                        size_bytes = resolved.size
                try:
                    bty_image_ref = _catalog.image_ref_for_src(entry.src)
                except ValueError as exc:
                    errors.append({"name": entry.name, "error": str(exc)})
                    continue
                try:
                    conn.execute(
                        "INSERT INTO catalog_entries "
                        "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                        "format, size_bytes, description, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bty_image_ref,
                            entry.src,
                            sha,
                            entry.name,
                            None,
                            fmt,
                            size_bytes,
                            entry.description,
                            now,
                        ),
                    )
                    imported += 1
                except sqlite3.IntegrityError:
                    skipped += 1
            _log_event(
                conn,
                kind="catalog.entries.imported",
                summary=(
                    f"imported {imported} entr{'y' if imported == 1 else 'ies'} from {source!r}"
                ),
                subject_kind="catalog",
                subject_id=source,
                actor="operator",
                source_ip=_client_ip(request),
                details={
                    "source": source,
                    "imported": imported,
                    "skipped": skipped,
                    "errors": errors,
                },
            )
            conn.commit()
        return {
            "source": source,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }

    # ---------- catalog download manager ----------------------------------
    # Authenticated endpoints; only operators logged into the bty-web
    # UI can enqueue / cancel fetches. Skipped silently when no
    # manifest is configured.

    @app.get("/catalog/downloads")
    async def list_downloads(_: str = Depends(require_auth)) -> dict[str, Any]:
        if parsed_catalog is None:
            return {"manifest": None, "downloads": []}
        states = await download_manager.list()
        return {
            "manifest": str(manifest_path),
            "cache_dir": str(catalog_cache_dir),
            "max_parallel": download_manager.max_parallel,
            "downloads": [s.to_dict() for s in states],
        }

    @app.post("/catalog/downloads", status_code=status.HTTP_202_ACCEPTED)
    async def enqueue_download(
        body: _models.CatalogEnqueueRequest,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if parsed_catalog is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no catalog manifest configured",
            )
        try:
            state = await download_manager.enqueue(body.name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        return state.to_dict()

    @app.delete("/catalog/downloads/{name}")
    async def cancel_download(
        name: str,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if parsed_catalog is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no catalog manifest configured",
            )
        state = await download_manager.cancel(name)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active download named {name!r}",
            )
        return state.to_dict()

    @app.delete(
        "/catalog/cache/{name}",
        dependencies=[Depends(require_auth)],
    )
    def delete_catalog_cache(name: str, request: Request) -> dict[str, Any]:
        """Delete the cached bytes for a named catalog entry; keep
        the entry's metadata.

        Looks up the entry's sha256 (DB ``catalog_entries`` first;
        then the static manifest if loaded), unlinks
        ``$cache_dir/<sha256>`` if it exists. Idempotent: a missing
        file or unknown name both return ``{"deleted": false}``
        with a ``reason`` string. The catalog entry's metadata is
        preserved, so the row keeps surfacing in ``/images`` as
        "available" (not cached) and ``POST /catalog/downloads``
        re-fetches on demand.

        Used by the ``/ui/images`` bulk "Delete local copy" toolbar
        action; per-name dispatch keeps the response surface symmetric
        with the per-name ``POST /catalog/downloads`` enqueue path.
        Path-separator characters or NUL in ``name`` are rejected at
        the boundary, same rule as :class:`CatalogEnqueueRequest`.
        """
        if not name or any(c in name for c in ("/", "\\", "\x00")) or name in (".", ".."):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid name: {name!r}",
            )
        sha: str | None = None
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT disk_image_sha FROM catalog_entries WHERE name = ?",
                (name,),
            ).fetchone()
            if row and row["disk_image_sha"]:
                sha = str(row["disk_image_sha"])
        if sha is None and parsed_catalog is not None:
            entry = parsed_catalog.by_name(name)
            if entry is not None and entry.sha256 is not None:
                sha = entry.sha256
        if sha is None:
            return {"name": name, "deleted": False, "reason": "no sha256 for name"}
        cached_file = catalog_cache_dir / sha
        if not cached_file.exists():
            return {
                "name": name,
                "deleted": False,
                "reason": "not cached",
                "disk_image_sha": sha,
            }
        cached_file.unlink()
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="catalog.cache.deleted",
                summary=f"deleted cached file for {name!r}",
                subject_kind="catalog",
                subject_id=name,
                actor="operator",
                source_ip=_client_ip(request),
                details={"name": name, "disk_image_sha": sha},
            )
            conn.commit()
        return {"name": name, "deleted": True, "disk_image_sha": sha}

    # ---------- catalog hash manager --------------------------------------
    # Hashing is independent of the manifest -- always available so
    # an operator can compute SHA-256 sidecars for dir-scan files
    # whether or not they author a catalog.toml.

    @app.get("/catalog/hashes")
    async def list_hashes(_: str = Depends(require_auth)) -> dict[str, Any]:
        states = await hash_manager.list()
        return {
            "image_root": str(resolved_image_root),
            "max_parallel": hash_manager.max_parallel,
            "hashes": [s.to_dict() for s in states],
        }

    @app.post("/catalog/hashes", status_code=status.HTTP_202_ACCEPTED)
    async def enqueue_hash(
        body: _models.CatalogEnqueueRequest,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            state = await hash_manager.enqueue(body.name)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        return state.to_dict()

    @app.delete("/catalog/hashes/{name}")
    async def cancel_hash(
        name: str,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        state = await hash_manager.cancel(name)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active hash named {name!r}",
            )
        return state.to_dict()

    return app


# ---------- helpers -----------------------------------------------------------


def _normalise_mac(raw: str) -> str:
    """Return a canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC, or 400."""
    cleaned = raw.lower().replace("-", ":")
    parts = cleaned.split(":")
    if len(parts) != 6 or any(
        len(p) != 2 or any(c not in "0123456789abcdef" for c in p) for p in parts
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid MAC: {raw!r}",
        )
    return cleaned


def _row_to_machine(row: object) -> _models.Machine:
    """Decode a sqlite3.Row into a ``_models.Machine``."""
    return _models.Machine(
        mac=row["mac"],  # type: ignore[index]
        bty_image_ref=row["bty_image_ref"],  # type: ignore[index]
        hostname=row["hostname"],  # type: ignore[index]
        discovered_at=_iso_or_none(row["discovered_at"]),  # type: ignore[index]
        last_seen_at=_iso_or_none(row["last_seen_at"]),  # type: ignore[index]
        last_seen_ip=row["last_seen_ip"],  # type: ignore[index]
        boot_policy=row["boot_policy"],  # type: ignore[index]
        last_flashed_at=_iso_or_none(row["last_flashed_at"]),  # type: ignore[index]
        created_at=datetime.fromisoformat(row["created_at"]),  # type: ignore[index]
        updated_at=datetime.fromisoformat(row["updated_at"]),  # type: ignore[index]
    )


def _iso_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _head_content_length(url: str, *, timeout: float = 10.0) -> int | None:
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _client_ip(request: Request) -> str | None:
    """Return the request's client IP, normalised for storage.

    Wraps ``request.client.host`` in ``_events_log.normalize_ip``
    so a v4-mapped-v6 address (``::ffff:192.168.1.5`` -- the form
    Starlette returns when bty-web binds on ``::`` and a v4 client
    connects) collapses to the bare v4 form. Without this, the
    same client shows up as two distinct rows in the audit log.

    When ``BTY_TRUSTED_PROXY`` is set in the environment (any
    truthy value), the leftmost ``X-Forwarded-For`` entry takes
    precedence so audit rows reflect the real client IP rather
    than the reverse-proxy's loopback. Off by default because
    the header is client-spoofable: only enable it when bty-web
    is configured behind a proxy that strips inbound X-F-F.
    """
    if os.environ.get("BTY_TRUSTED_PROXY"):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # X-F-F is a comma-separated chain (proxy-near-client
            # first); the leftmost entry is the originating client.
            first = xff.split(",", 1)[0].strip()
            if first:
                return _normalize_ip(first)
    return _normalize_ip(request.client.host if request.client else None)


def _safe_path(root: Path, name: str) -> Path:
    """Resolve ``root / name`` with path-traversal checks, return the path.

    Rejects names with slashes, ``..``, NULs, etc. Caller decides
    what to do with the resolved path (404 vs. open-for-write); the
    existing FileResponse helper used to inline this check.
    """
    if not name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad name")
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad name") from exc
    return candidate


def _serve_safe_file(root: Path, name: str) -> FileResponse:
    """Return a FileResponse for ``root / name`` after path-traversal checks."""
    candidate = _safe_path(root, name)
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no such file: {name}")
    return FileResponse(candidate, filename=name)


# Default max upload-body size (200 GiB). Generous for plausible
# real OS images (decompressed Windows is the largest target at
# ~50 GiB; everything Linux-y fits in single-digit GB) but caps
# the worst case at "the disk fills up before bty-web does
# anything useful". Operators can raise via ``BTY_MAX_UPLOAD_BYTES``
# if they have a legitimate use case for bigger images.
_DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024 * 1024


def _max_upload_bytes() -> int:
    """Resolve the upload size cap from ``BTY_MAX_UPLOAD_BYTES`` or default."""
    raw = os.environ.get("BTY_MAX_UPLOAD_BYTES")
    if raw is None:
        return _DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else _DEFAULT_MAX_UPLOAD_BYTES


async def _stream_upload(request: Request, root: Path, name: str) -> dict[str, object]:
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

    Caps the body at :data:`_DEFAULT_MAX_UPLOAD_BYTES` (200 GiB by
    default; ``BTY_MAX_UPLOAD_BYTES`` overrides). A runaway script
    or hostile request that streams forever otherwise fills the
    image-root partition; the cap kills the upload + unlinks the
    partial well before that. The pre-cleanup partial unlink
    covers the cancellation case.
    """
    candidate = _safe_path(root, name)
    root.mkdir(parents=True, exist_ok=True)
    partial = candidate.with_suffix(candidate.suffix + ".partial")
    max_bytes = _max_upload_bytes()
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
                                f"(BTY_MAX_UPLOAD_BYTES). Aborted at {size} bytes."
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
