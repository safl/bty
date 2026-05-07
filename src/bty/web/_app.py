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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

import bty
from bty import images
from bty.web import _db, _models, _ui
from bty.web._auth import make_token_dep
from bty.web._events import MachineEvent, MachineEventBus, sse_format
from bty.web._workflow import WorkflowRunner

TEMPLATES_DIR = Path(__file__).parent / "_templates"
STATIC_DIR = Path(__file__).parent / "_static"


def create_app(
    *,
    state_path: Path,
    service_user: str,
    image_root: Path | None = None,
    boot_root: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app. All config flows through this function.

    ``service_user`` is the Linux account whose OS password gates
    ``POST /ui/login`` - typically the user bty-web is running as
    (resolved from ``geteuid`` in :func:`bty.web.main`). Tests pass a
    fixture name and monkeypatch ``pamela.authenticate``.

    ``boot_root`` is where the live-env artifacts (kernel + initrd +
    squashfs) live for the ``GET /boot/{name}`` endpoint; defaults to
    ``state_path.parent / "boot"`` (i.e. ``/var/lib/bty/boot`` on a
    stock appliance).
    """
    require_token = make_token_dep(state_path)
    resolved_image_root: Path = image_root or images.default_image_root()
    resolved_boot_root: Path = boot_root or (state_path.parent / "boot")
    event_bus = MachineEventBus()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The SSE event bus accepts publishes from worker threads
        # (WorkflowRunner) - capture the loop now so cross-thread
        # publishes can hop in via call_soon_threadsafe.
        event_bus.attach(asyncio.get_running_loop())
        yield

    jinja = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        # Autoescape only HTML (UI) templates; the iPXE ``.j2`` files
        # are plain text and would be mangled by escaping.
        autoescape=select_autoescape(enabled_extensions=("html",)),
        keep_trailing_newline=True,
    )

    _db.init_db(state_path)

    app = FastAPI(title="bty-web", version=bty.__version__, lifespan=_lifespan)

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
                "SELECT COUNT(*) FROM machines WHERE image IS NULL"
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
        if machine.get("image") and machine.get("boot_policy") == "flash":
            template = jinja.get_template("ipxe_flash.j2")
            return template.render(mac=normalised, machine=machine, host=host)
        if machine.get("image"):
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

    @app.get("/images/{name}", include_in_schema=False)
    def serve_image(name: str) -> FileResponse:
        # Same trust model as /boot. The live env curls this to get
        # the image bytes that ``bty.image_url`` points at.
        return _serve_safe_file(resolved_image_root, name)

    @app.get(
        "/events/machines",
        dependencies=[Depends(require_token)],
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
        dependencies=[Depends(require_token)],
    )
    def list_machines() -> list[_models.Machine]:
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        return [_row_to_machine(r) for r in rows]

    @app.get(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_token)],
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
        dependencies=[Depends(require_token)],
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
                    (mac, image, provisioning_mode, hostname,
                     cijoe_workflow_ref, last_known_good,
                     boot_policy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    image              = excluded.image,
                    provisioning_mode  = excluded.provisioning_mode,
                    hostname           = excluded.hostname,
                    cijoe_workflow_ref = excluded.cijoe_workflow_ref,
                    boot_policy        = excluded.boot_policy,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    body.image,
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
        dependencies=[Depends(require_token)],
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
    def list_images() -> list[_models.ImageEntry]:
        # Open route: the bty-tui-on-PXE flow needs to enumerate the
        # catalog without first bootstrapping auth. The byte-serving
        # route ``GET /images/{name}`` is already open (PXE clients
        # download images during the live env's flash phase), so
        # leaving the listing protected only added discovery friction
        # without changing the trust model. Same homelab-network
        # assumption as the other /pxe / /boot / /images/{name}
        # endpoints.
        out: list[_models.ImageEntry] = []
        for img in images.list_images(resolved_image_root):
            out.append(
                _models.ImageEntry(
                    name=img.name,
                    path=str(img.path),
                    format=img.format or "",
                    size_bytes=img.size_bytes,
                )
            )
        return out

    @app.put(
        "/images/{name}",
        dependencies=[Depends(require_token)],
        include_in_schema=False,
    )
    async def upload_image(name: str, request: Request) -> dict[str, object]:
        """Stream-upload an image into the image root.

        Body is the raw image bytes (``Content-Type:
        application/octet-stream``). Atomic via a ``.partial`` sibling
        + rename. Returns the resolved path + bytes-written on
        success; replaces an existing file with the same name.
        """
        result = await _stream_upload(request, resolved_image_root, name)
        # Image catalog count changes; refresh the dashboard fragment.
        publish_state_changed()
        return result

    @app.put(
        "/boot/{name}",
        dependencies=[Depends(require_token)],
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
    _ui.register_ui_routes(
        app,
        jinja=jinja,
        state_path=state_path,
        service_user=service_user,
        image_root=resolved_image_root,
        boot_root=resolved_boot_root,
        publish_machines_changed=publish_machines_changed,
    )

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
        image=row["image"],  # type: ignore[index]
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
