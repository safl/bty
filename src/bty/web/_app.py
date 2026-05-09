"""FastAPI application for bty-web.

``create_app(state_path, service_user, image_root)`` returns a fully
wired FastAPI instance. Tests construct one with a tmp_path SQLite +
a fixture service user (PAM gets monkeypatched in those tests).
``main()`` (in :mod:`bty.web.__init__`) builds one from environment
+ defaults and hands it to uvicorn.
"""

from __future__ import annotations

import asyncio
import json
import sys
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
from bty.web import _catalog as _web_catalog
from bty.web import _db, _hash, _models, _ui
from bty.web._auth import SESSION_COOKIE, require_auth
from bty.web._events import MachineEvent, MachineEventBus, sse_format
from bty.web._workflow import WorkflowRunner

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
    server is valid on another). On the cooked appliance,
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

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The SSE event bus accepts publishes from worker threads
        # (WorkflowRunner) - capture the loop now so cross-thread
        # publishes can hop in via call_soon_threadsafe.
        event_bus.attach(asyncio.get_running_loop())
        if parsed_catalog is not None:
            download_manager.start(parsed_catalog, catalog_cache_dir)
        # The hash manager always starts -- it operates on
        # ``image_root``, which exists for every bty-web shape
        # (appliance, container, dev). Default parallelism is 1
        # so a Pi-class box doesn't get hammered if multiple big
        # images need importing at once.
        hash_manager.start(resolved_image_root)
        # Auto-import: enqueue every dir-scan file without a
        # ``.sha256`` sidecar so the HashManager processes them
        # in the background. Once a sidecar lands, ``/images``
        # picks the entry up with a server URL on the next call.
        # Operator-initiated work is unaffected -- they queue
        # behind the auto-import jobs by FIFO order, but the
        # parallelism cap (default 1) keeps the box responsive.
        for img in images.list_images(resolved_image_root):
            if img.sha256 is None:
                try:
                    await hash_manager.enqueue(img.name)
                except FileNotFoundError:
                    # File vanished between the list_images scan
                    # and the enqueue; harmless.
                    pass
        try:
            yield
        finally:
            if parsed_catalog is not None:
                await download_manager.stop()
            await hash_manager.stop()

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
                "SELECT COUNT(*) FROM machines WHERE image_sha256 IS NULL"
            ).fetchone()[0]
        image_count = len(images.list_images(resolved_image_root))
        return jinja.get_template("ui/_dashboard_counts.html").render(
            machine_count=machine_count,
            discovered_count=discovered_count,
            image_count=image_count,
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

    workflow_runner = WorkflowRunner(
        state_path=state_path,
        publish_machines_changed=publish_machines_changed,
    )

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
        client_ip = request.client.host if request.client else None
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
                        (mac, provisioning_mode, boot_policy,
                         discovered_at, last_seen_at, last_seen_ip,
                         created_at, updated_at)
                    VALUES (?, 'none', 'tui', ?, ?, ?, ?, ?)
                    """,
                    (normalised, now, now, client_ip, now, now),
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
        if machine.get("image_sha256") and machine.get("boot_policy") == "flash":
            template = jinja.get_template("ipxe_flash.j2")
            return template.render(mac=normalised, machine=machine, host=host)
        if machine.get("image_sha256"):
            template = jinja.get_template("ipxe.j2")
            return template.render(mac=normalised, machine=machine)
        template = jinja.get_template("ipxe_unknown.j2")
        return template.render(mac=normalised, machine=machine)

    @app.post(
        "/pxe/{mac}/done",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def pxe_done(mac: str) -> Response:
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
        with _db.open_db(state_path) as conn:
            cur = conn.execute(
                "UPDATE machines SET last_flashed_at = ?, updated_at = ? WHERE mac = ?",
                (now, now, normalised),
            )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        publish_machines_changed()

        # Online cijoe (milestone 15): if the machine is set up for
        # post-boot provisioning, kick off a workflow run in a worker
        # thread now that the live env says the flash is done. cijoe's
        # transport-retry handles waiting for SSH to come up. The
        # request still returns 204 immediately - workflow status
        # surfaces via the SSE machines-update channel as it changes.
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT provisioning_mode, cijoe_workflow_ref, last_seen_ip "
                "FROM machines WHERE mac = ?",
                (normalised,),
            ).fetchone()
        if (
            row is not None
            and row["provisioning_mode"] == "cijoe-online"
            and row["cijoe_workflow_ref"]
            and row["last_seen_ip"]
        ):
            workflow_runner.kick_off(
                mac=normalised,
                workflow_ref=row["cijoe_workflow_ref"],
                target_ip=row["last_seen_ip"],
            )

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/boot/{name}", include_in_schema=False)
    def boot_artifact(name: str) -> FileResponse:
        # Live-env artifacts (kernel + initrd + squashfs) the iPXE chain
        # references. Open route: PXE clients have no token. Operator
        # populates ``boot_root`` via the UI's "fetch latest release"
        # action (D-3b) - until the dir has files, this returns 404
        # and the appliance is non-functional for boot_policy=flash.
        return _serve_safe_file(resolved_boot_root, name)

    def _serve_image_by_key(key: str) -> FileResponse:
        """Resolve ``key`` (filename OR SHA-256) to bytes.

        Resolution order:

          1. Literal filename under ``image_root`` -- legacy path.
          2. SHA-256 (64 lower-hex):
             a. Catalog cache: ``cache_dir/<sha>``.
             b. Dir-scan: file whose ``.sha256`` sidecar holds
                this digest.
        """
        try:
            return _serve_safe_file(resolved_image_root, key)
        except HTTPException:
            pass
        if len(key) == 64 and all(c in "0123456789abcdef" for c in key.lower()):
            sha = key.lower()
            cached = catalog_cache_dir / sha
            if cached.is_file():
                return FileResponse(cached, media_type="application/octet-stream")
            for img in images.list_images(resolved_image_root):
                if img.sha256 == sha:
                    return FileResponse(img.path, media_type="application/octet-stream")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such image: {key}",
        )

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
    def upsert_machine(mac: str, body: _models.MachineUpsert) -> _models.Machine:
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
                    (mac, image_sha256, provisioning_mode, hostname,
                     cijoe_workflow_ref, last_known_good,
                     boot_policy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    image_sha256       = excluded.image_sha256,
                    provisioning_mode  = excluded.provisioning_mode,
                    hostname           = excluded.hostname,
                    cijoe_workflow_ref = excluded.cijoe_workflow_ref,
                    boot_policy        = excluded.boot_policy,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    body.image_sha256,
                    body.provisioning_mode,
                    body.hostname,
                    body.cijoe_workflow_ref,
                    body.boot_policy,
                    created_at,
                    now,
                ),
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
    def delete_machine(mac: str) -> Response:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
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
        manifest_entries = parsed_catalog.entries if parsed_catalog else ()
        unified = images.merge_with_catalog(
            resolved_image_root, manifest_entries, catalog_cache_dir
        )
        host = request.headers.get("host", f"{request.url.hostname}:{request.url.port or 8080}")
        scheme = request.url.scheme or "http"
        out: list[_models.ImageEntry] = []
        for u in unified:
            # Skip entries we cannot point at: dir-scan files
            # without a sidecar AND no manifest entry. The
            # auto-import on startup will hash them and they will
            # appear in the next listing.
            if u.sha256 is None:
                continue
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
                url = f"{scheme}://{host}/images/{u.sha256}/{u.names[0]}"
            else:
                # Manifest entry not yet cached -- the client
                # streams directly from upstream. First manifest
                # source wins.
                upstream = next(
                    (s.location for s in u.sources if s.kind == "manifest"),
                    None,
                )
                if upstream is None:
                    # No manifest source AND not cached -- nothing
                    # we can hand the client. Skip.
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
        return out

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
        """
        result = await _stream_upload(request, resolved_image_root, name)
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
            try:
                await hash_manager.enqueue(name)
            except FileNotFoundError:
                # ``_stream_upload`` raised earlier if the write
                # failed, so this should never happen -- guarded
                # for safety.
                pass
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

    def _list_unified_images() -> list[images.UnifiedImage]:
        """SHA-keyed merge of dir-scan + catalog manifest entries.

        Recomputed per call so an operator who drops new files into
        BTY_IMAGE_ROOT (or whose catalog fetch just completed) sees
        the change on the next page load without restarting bty-web.
        """
        manifest_entries = parsed_catalog.entries if parsed_catalog else ()
        return images.merge_with_catalog(resolved_image_root, manifest_entries, catalog_cache_dir)

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
    raw_known_good = row["last_known_good"]  # type: ignore[index]
    last_known_good: dict[str, object] | None = None
    if raw_known_good:
        try:
            decoded = json.loads(raw_known_good)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            last_known_good = decoded
    return _models.Machine(
        mac=row["mac"],  # type: ignore[index]
        image_sha256=row["image_sha256"],  # type: ignore[index]
        provisioning_mode=row["provisioning_mode"],  # type: ignore[index]
        hostname=row["hostname"],  # type: ignore[index]
        cijoe_workflow_ref=row["cijoe_workflow_ref"],  # type: ignore[index]
        last_known_good=last_known_good,
        discovered_at=_iso_or_none(row["discovered_at"]),  # type: ignore[index]
        last_seen_at=_iso_or_none(row["last_seen_at"]),  # type: ignore[index]
        last_seen_ip=row["last_seen_ip"],  # type: ignore[index]
        boot_policy=row["boot_policy"],  # type: ignore[index]
        last_flashed_at=_iso_or_none(row["last_flashed_at"]),  # type: ignore[index]
        last_workflow_run_at=_iso_or_none(row["last_workflow_run_at"]),  # type: ignore[index]
        last_workflow_status=row["last_workflow_status"],  # type: ignore[index]
        last_workflow_output_path=row["last_workflow_output_path"],  # type: ignore[index]
        created_at=datetime.fromisoformat(row["created_at"]),  # type: ignore[index]
        updated_at=datetime.fromisoformat(row["updated_at"]),  # type: ignore[index]
    )


def _iso_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


async def _stream_upload(request: Request, root: Path, name: str) -> dict[str, object]:
    """Stream the request body to ``root / name`` and return basic metadata.

    Atomic via a sibling ``.partial`` file + rename so a torn upload
    can't leave a half-written image where a previous good copy used
    to be. The destination directory is created if it doesn't exist
    (server image's first-boot init creates it for the prod paths,
    but tests pass tmp_path and we want this to work without an
    init step).
    """
    candidate = _safe_path(root, name)
    root.mkdir(parents=True, exist_ok=True)
    partial = candidate.with_suffix(candidate.suffix + ".partial")
    size = 0
    with partial.open("wb") as fh:
        async for chunk in request.stream():
            if chunk:
                fh.write(chunk)
                size += len(chunk)
    partial.replace(candidate)
    return {"name": name, "size_bytes": size, "path": str(candidate)}
