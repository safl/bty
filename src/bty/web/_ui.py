"""Browser UI routes for bty-web (Jinja + Bootstrap + HTMX).

Routes live under ``/ui``. They render server-side HTML and gate on
the session cookie set by ``POST /ui/login`` (a Starlette
SessionMiddleware-managed signed cookie); the API surface at ``/`` is
unchanged. ``register_ui_routes(app, ...)`` attaches all the UI
handlers to an existing FastAPI app. The ``/ui/machines`` table
subscribes to ``/events/machines`` (HTMX SSE extension) for live
updates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import ValidationError

import bty
from bty import images as bty_images
from bty.web import (
    _auth,
    _backup,
    _config,
    _db,
    _events_log,
    _releases,
    _settings_store,
    _sysconfig,
)
from bty.web._auth import SESSION_AUTHED_KEY
from bty.web._events_log import KNOWN_ACTORS, KNOWN_EVENT_KINDS, KNOWN_SUBJECT_KINDS
from bty.web._events_log import normalize_ip as _normalize_ip
from bty.web._models import (
    BOOT_MODES,
    DEFAULT_BOOT_MODE,
    CatalogEntryAdd,
    MachineUpsert,
)


class NotAuthenticated(Exception):
    """Raised by UI dependencies when the request lacks an authed session.

    The exception handler redirects to ``/ui/login``; UI requests get
    a redirect, API requests would still hit the regular 401 dependency.
    """


def register_ui_routes(
    app: FastAPI,
    *,
    jinja: Environment,
    state_path: Path,
    service_user: str,
    boot_root: Path,
    backups_root: Path,
    publish_state_changed: Callable[[], None] = lambda: None,
    list_unified_images: Callable[[], list[bty_images.UnifiedImage]] | None = None,
) -> None:
    """Attach the ``/ui`` HTML routes (and exception handler) to ``app``.

    ``service_user`` is the Linux account bty-web runs as (shown in the
    UI for context). ``publish_state_changed`` is invoked after any
    UI form mutates a machine record, so SSE subscribers see the
    change immediately. The default no-op makes this module testable
    in isolation; the real app passes the bus-publishing callable.
    """

    def render(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("version", bty.__version__)
        # Auth is always on; ``logged_in`` is purely session-derived.
        # Templates can opt-in to a "using the default password"
        # banner via ``using_default_password``.
        ctx.setdefault("logged_in", bool(request.session.get(SESSION_AUTHED_KEY)))
        ctx.setdefault("using_default_password", _auth.using_default_password())
        ctx.setdefault("service_user", service_user)
        # Top-level path segment under /ui/ - the layout uses this to
        # mark the active nav button. ``request.url.path`` is the full
        # path; we want just the second segment so e.g. /ui/machines
        # AND /ui/machines/aa:bb:... both light up "machines".
        path = request.url.path
        nav_active = path.split("/")[2] if path.startswith("/ui/") and len(path) > 4 else ""
        ctx.setdefault("nav_active", nav_active)
        ctx.setdefault("flash", None)
        ctx.setdefault("flash_kind", None)
        template = jinja.get_template(name)
        return HTMLResponse(template.render(**ctx))

    @app.exception_handler(NotAuthenticated)
    async def _not_authed(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        del request, exc
        return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    def require_ui_auth(request: Request) -> None:
        # Auth is always on. 401-on-API-routes is handled by
        # ``_auth.require_auth``; UI routes bounce through here so the
        # exception handler can 303 to /ui/login instead of returning
        # a JSON 401 the browser can't act on.
        if not request.session.get(SESSION_AUTHED_KEY):
            raise NotAuthenticated

    # ----- entry / auth ----------------------------------------------------

    @app.get("/ui", include_in_schema=False)
    def ui_root() -> RedirectResponse:
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/login", include_in_schema=False)
    def ui_login_form(request: Request) -> Response:
        # Already authed -> skip the form and land on the dashboard.
        # Lets ``GET /`` (which 303s here) act as a smart entry point.
        if request.session.get(SESSION_AUTHED_KEY):
            return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        return render("ui/login.html", request)

    @app.post("/ui/login", include_in_schema=False)
    def ui_login_submit(
        request: Request,
        password: Annotated[str, Form()],
    ) -> Response:
        client_ip = _client_ip(request)
        if not _auth.check_password(password):
            # Failed login: record so an operator scanning /ui/events sees
            # brute-force attempts.
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="auth.login.failed",
                    summary="operator login failed (invalid password)",
                    subject_kind="auth",
                    subject_id="operator",
                    actor="operator",
                    source_ip=client_ip,
                )
                conn.commit()
            return render("ui/login.html", request, error="Invalid password.")
        # Success path. Record so the audit log shows session boundaries.
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="auth.login.succeeded",
                summary="operator login succeeded",
                subject_kind="auth",
                subject_id="operator",
                actor="operator",
                source_ip=client_ip,
            )
            conn.commit()
        # SessionMiddleware re-signs and re-attaches the cookie on the
        # response; we just flip the authed flag in the session dict.
        request.session[SESSION_AUTHED_KEY] = True
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/logout", include_in_schema=False)
    def ui_logout(request: Request) -> Response:
        # Record the logout *before* clearing the session so the
        # actor reflects who was logged in. Auth-event symmetry
        # with the login.succeeded / login.failed pair.
        was_authed = bool(request.session.get(SESSION_AUTHED_KEY))
        # ``clear()`` empties the session dict; SessionMiddleware then
        # emits an empty (deletion) cookie on the response.
        request.session.clear()
        if was_authed:
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="auth.logout",
                    summary="operator logout",
                    subject_kind="auth",
                    subject_id="operator",
                    actor="operator",
                    source_ip=_client_ip(request),
                )
                conn.commit()
        return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    # ----- pages (auth-required) ------------------------------------------

    @app.get(
        "/ui/dashboard",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_dashboard(request: Request) -> HTMLResponse:
        unified = list_unified_images() if list_unified_images is not None else []
        with _db.open_db(state_path) as conn:
            # Live panel context (machine summary + image breakdown),
            # shared with the SSE publisher so the two never drift.
            counts = dashboard_counts_context(conn, unified)
            catalog_entry_count = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
            # Error-event count for the Health Monitoring panel: only
            # *unacknowledged* failures count toward the tripwire, so
            # an operator can mark a known / resolved failure as seen
            # and get the panel back to green without deleting the row.
            # Same ``kind LIKE '%failed'`` predicate the /ui/events
            # ``?failed=1`` filter uses (list_events(failed_only=True)),
            # so the "Review errors" deep link lands on exactly this
            # family: image.upload.failed / image.hash.failed /
            # catalog.entry.add.failed / netboot.artifacts.fetch.failed /
            # settings.tftp.control_failed / auth.login.failed.
            error_event_count = _events_log.count_unacknowledged_failures(conn)
            # Recent activity slice for the dashboard's "what just
            # happened?" widget. Reuses ``_events_card.html`` so the
            # styling matches the per-machine and per-image cards;
            # the dashboard renders at request time only (no SSE
            # update) so a reload is the refresh gesture.
            recent_events = _events_log.list_events(conn, limit=10)
        # Health Monitoring: the conditions that must hold for PXE +
        # flash to work, plus an error-events tripwire. One glance at
        # "is this server ready to do its job", each row deep-
        # linking to the page that owns it (with a fix action on fail).
        missing_netboot = _releases.missing_netboot_artifacts(boot_root)
        tftp = _sysconfig.tftp_status()
        catalog_ok = catalog_entry_count > 0 or counts["img_total"] > 0
        # Dedicated-disk state check. Green when the state dir is a
        # mount that actually holds the live data (the DB plus the
        # image and netboot roots), so an OS reflash leaves it intact.
        # Otherwise an advisory INFO row (a blue "i", never a red
        # cross): a rootfs-only install is fully functional, the
        # dedicated disk is recommended, not required. ``os.path.ismount``
        # returns False on stat error, so this never raises.
        state_dir = state_path.parent
        state_migrated = os.path.ismount(state_dir)

        def _under(path: Path, parent: Path) -> bool:
            try:
                path.resolve().relative_to(parent.resolve())
            except (OSError, ValueError):
                return False
            return True

        state_valid = state_migrated and state_path.exists() and _under(boot_root, state_dir)
        sanity = [
            {
                "label": "Netboot artifacts present",
                "ok": not missing_netboot,
                "detail": (
                    f"All four files under {boot_root}."
                    if not missing_netboot
                    else f"Missing: {', '.join(missing_netboot)}"
                ),
                "href": "/ui/netboot",
                "fix_href": "/ui/netboot",
                "fix_label": "Fetch netboot artifacts",
            },
            {
                "label": "Catalog is non-empty",
                "ok": catalog_ok,
                "detail": (
                    f"{catalog_entry_count} catalog entries, {counts['img_total']} images total."
                    if catalog_ok
                    else "No catalog entries and no local images."
                ),
                "href": "/ui/images",
                "fix_href": "/ui/images",
                "fix_label": "Fetch catalog",
            },
            {
                "label": "TFTP daemon running",
                "ok": tftp.is_active,
                "detail": (
                    f"dnsmasq.service is {tftp.state}."
                    if tftp.state == "active"
                    else (
                        f"dnsmasq.service is {tftp.state} "
                        "(container deploys run TFTP from a sidecar "
                        "outside bty-web's visibility -- check the "
                        "sidecar's status if PXE is failing)."
                    )
                ),
                "href": "/ui/netboot",
                # The Netboot page is purely observational; "fix" is
                # the same as "view".
                "fix_href": "/ui/netboot",
                "fix_label": "TFTP daemon",
            },
            {
                "label": "No unacknowledged errors",
                "ok": error_event_count == 0,
                "detail": (
                    "No unacknowledged failed events."
                    if error_event_count == 0
                    else f"{error_event_count} unacknowledged failure(s); "
                    "review and acknowledge to clear."
                ),
                "href": "/ui/events?failed=1",
                "fix_href": "/ui/events?failed=1",
                "fix_label": "Review errors",
            },
            {
                # Green check when the state dir is a mount holding the
                # live DB plus the image and netboot roots (survives a
                # reflash); otherwise an advisory INFO row (a blue "i",
                # never a red cross): a rootfs-only install is fully
                # functional, the dedicated disk is recommended, not
                # required. No fix link: bty-state-migrate is a
                # host CLI command, not a web action.
                "label": "State on a dedicated disk",
                "ok": state_valid,
                "info": not state_valid,
                "detail": (
                    f"{state_dir} is a mount point; the database, images "
                    "and netboot artifacts all live on it, so they "
                    "survive an OS reflash."
                    if state_valid
                    else (
                        f"{state_dir} is mounted but images or netboot "
                        "resolve outside it, so they would not survive a "
                        "reflash. Re-run bty-state-migrate <disk>."
                        if state_migrated
                        else "Running on the root filesystem. Recommended "
                        "(not required): run bty-state-migrate <disk> so "
                        "the database, images and netboot artifacts "
                        "survive a reflash."
                    )
                ),
            },
        ]
        return render(
            "ui/dashboard.html",
            request,
            recent_events=recent_events,
            sanity=sanity,
            **counts,
        )

    @app.get(
        "/ui/machines",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machines(
        request: Request,
        filter: str | None = None,
    ) -> HTMLResponse:
        # ``?filter=discovered`` -- only unassigned machines (no
        # bty_image_ref yet). Powered by the dashboard's
        # "Unassigned (discovered)" counter card so clicking it
        # lands on a pre-filtered list. ``?filter=assigned`` --
        # symmetric "operator-bound" view. Anything else (no
        # filter, empty value, an unrecognised value) shows the
        # full list and surfaces no active-filter banner.
        if filter == "discovered":
            sql = "SELECT * FROM machines WHERE bty_image_ref IS NULL ORDER BY mac"
            active_filter: str | None = filter
        elif filter == "assigned":
            sql = "SELECT * FROM machines WHERE bty_image_ref IS NOT NULL ORDER BY mac"
            active_filter = filter
        else:
            sql = "SELECT * FROM machines ORDER BY mac"
            active_filter = None
        # Sub-nav: ``?section=list`` (default) is the live table,
        # ``?section=add`` is the form for staging a machine before
        # it PXE-boots. Unrecognised sections fall back to list.
        section = request.query_params.get("section") or "list"
        if section not in ("list",):
            section = "list"
        with _db.open_db(state_path) as conn:
            rows = conn.execute(sql).fetchall()
            # Recent machine activity (discoveries, flashes, inventory
            # posts) for the page's "Activity" table.
            machine_events = _events_log.list_events(conn, subject_kind="machine", limit=10)
        machines = [_row_to_dict(r) for r in rows]
        # v0.32.4: ``?deleted=<mac>`` / ``?missing=<mac>`` carried over
        # from ``POST /ui/machines/{mac}/delete`` so the operator gets a
        # success / no-op-but-acknowledged flash banner instead of a
        # silent redirect.
        flash_deleted = request.query_params.get("deleted")
        flash_missing = request.query_params.get("missing")
        return render(
            "ui/machines.html",
            request,
            machines=machines,
            active_filter=active_filter,
            section=section,
            machine_events=machine_events,
            flash_deleted=flash_deleted,
            flash_missing=flash_missing,
        )

    @app.get(
        "/ui/machines/{mac}",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machine_detail(mac: str, request: Request) -> HTMLResponse:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        # The picker lists catalog_entries rows by ``bty_image_ref``
        # (provenance ID). Operator binding stays valid across rolling
        # tags / re-fetches; the content sha (``disk_image_sha``)
        # surfaces alongside the ref when known.
        with _db.open_db(state_path) as conn:
            catalog_rows = conn.execute(
                "SELECT bty_image_ref, name, format, src, disk_image_sha, size_bytes "
                "FROM catalog_entries ORDER BY name"
            ).fetchall()
        catalog_options = [dict(r) for r in catalog_rows]
        # Reads ``?error=<msg>`` so the upsert form's bounce-on-
        # validation-failure renders a flash banner. Same shape as
        # /ui/images: Jinja autoescape covers ``flash`` so a
        # hostile error text cannot inject HTML.
        flash = request.query_params.get("error")
        # Per-machine event slice. ``subject_id=normalised`` filters
        # to events that touch this MAC (discovered, upserted,
        # flashed, task.*, etc.). Top 20 keeps the page short; the
        # full timeline lives at /ui/events.
        with _db.open_db(state_path) as conn:
            machine_events = _events_log.list_events(
                conn,
                subject_kind="machine",
                subject_id=normalised,
                limit=20,
            )
        return render(
            "ui/machine_detail.html",
            request,
            m=_row_to_dict(row),
            images=catalog_options,
            boot_policies=list(BOOT_MODES),
            machine_events=machine_events,
            hw=lshw_highlights(_db.row_value(row, "hw_lshw")),
            flash=flash,
            flash_kind="danger" if flash else None,
        )

    @app.post(
        "/ui/machines/{mac}",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machine_upsert(
        mac: str,
        request: Request,
        bty_image_ref: Annotated[str, Form()] = "",
        hostname: Annotated[str, Form()] = "",
        boot_mode: Annotated[str, Form()] = DEFAULT_BOOT_MODE,
        sanboot_drive: Annotated[str, Form()] = "",
        target_disk_serial: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        normalised = _normalise_mac(mac)
        # Run the form inputs through the same Pydantic model the
        # JSON ``PUT /machines/{mac}`` uses so the two paths stay in
        # sync: non-hex ``bty_image_ref`` and out-of-shape
        # ``hostname`` are rejected here too. Empty-form fields
        # normalise to ``None`` (Pydantic accepts ``None`` for
        # optional fields, sqlite stores NULL).
        try:
            validated = MachineUpsert(
                bty_image_ref=bty_image_ref or None,
                hostname=hostname or None,
                boot_mode=boot_mode,
                sanboot_drive=sanboot_drive or None,
                target_disk_serial=target_disk_serial or None,
            )
        except ValidationError as exc:
            # Pydantic's str() is a multi-line dump ending in a
            # pydantic.dev URL -- useless in a one-line flash banner.
            # Collapse to a concise ``field: message`` list instead.
            detail = (
                "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or 'field'}: {e['msg']}"
                    for e in exc.errors()
                )
                or "invalid input"
            )
            return RedirectResponse(
                f"/ui/machines/{normalised}?error="
                + urllib.parse.quote(f"validation failed: {detail}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except ValueError as exc:
            return RedirectResponse(
                f"/ui/machines/{normalised}?error="
                + urllib.parse.quote(f"validation failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        # ``boot_mode`` is pattern-checked by Pydantic above; the
        # explicit set membership check is therefore redundant but
        # kept so the operator gets an enum-style error message.
        if validated.boot_mode not in BOOT_MODES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid boot_mode: {validated.boot_mode!r}",
            )
        # Safety gate: boot_mode=bty-flash-always / bty-flash-once require a
        # picked target_disk_serial. Without it /pxe/<mac>/plan
        # would fall back to mode=interactive anyway (the auto-
        # flash branch needs a serial); catching it here gives
        # the operator a clear flash-banner error in /ui/machines
        # instead of the operator being surprised by a wizard
        # prompt at flash time.
        if (
            validated.boot_mode in ("bty-flash-always", "bty-flash-once")
            and not validated.target_disk_serial
        ):
            return RedirectResponse(
                f"/ui/machines/{normalised}?error="
                + urllib.parse.quote(
                    f"boot_mode={validated.boot_mode} requires a target disk; "
                    "pick one from the Target disk dropdown. If it's empty the "
                    "machine hasn't reported its disks yet -- power-cycle it and it "
                    "auto-reports them on its bty-inventory boot, then pick one here.",
                    safe="",
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            existing = conn.execute(
                "SELECT created_at FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            conn.execute(
                """
                INSERT INTO machines
                    (mac, bty_image_ref, hostname, boot_mode, sanboot_drive,
                     target_disk_serial, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    bty_image_ref      = excluded.bty_image_ref,
                    hostname           = excluded.hostname,
                    boot_mode          = excluded.boot_mode,
                    sanboot_drive      = excluded.sanboot_drive,
                    target_disk_serial = excluded.target_disk_serial,
                    -- Reset the one-shot alternation bit + completion
                    -- signals ONLY when a policy-affecting field
                    -- changes (boot_mode, bty_image_ref,
                    -- target_disk_serial). Hostname / sanboot_drive
                    -- don't invalidate the in-flight cycle. Mirrors
                    -- the JSON PUT /machines path; both must stay
                    -- in sync. See PUT for the rationale on why
                    -- the completion signals need clearing too
                    -- (stale last_flashed_at + a future crashed
                    -- flasher cycle = sanboot of a half-flashed disk).
                    saw_flasher_boot   = CASE
                        WHEN machines.boot_mode != excluded.boot_mode THEN 0
                        WHEN COALESCE(machines.bty_image_ref, '')
                             != COALESCE(excluded.bty_image_ref, '') THEN 0
                        WHEN COALESCE(machines.target_disk_serial, '')
                             != COALESCE(excluded.target_disk_serial, '') THEN 0
                        ELSE machines.saw_flasher_boot
                    END,
                    last_flashed_at    = CASE
                        WHEN machines.boot_mode != excluded.boot_mode THEN NULL
                        WHEN COALESCE(machines.bty_image_ref, '')
                             != COALESCE(excluded.bty_image_ref, '') THEN NULL
                        WHEN COALESCE(machines.target_disk_serial, '')
                             != COALESCE(excluded.target_disk_serial, '') THEN NULL
                        ELSE machines.last_flashed_at
                    END,
                    known_disks_at     = CASE
                        WHEN machines.boot_mode != excluded.boot_mode THEN NULL
                        WHEN COALESCE(machines.bty_image_ref, '')
                             != COALESCE(excluded.bty_image_ref, '') THEN NULL
                        WHEN COALESCE(machines.target_disk_serial, '')
                             != COALESCE(excluded.target_disk_serial, '') THEN NULL
                        ELSE machines.known_disks_at
                    END,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    validated.bty_image_ref,
                    validated.hostname,
                    validated.boot_mode,
                    validated.sanboot_drive,
                    validated.target_disk_serial,
                    created_at,
                    now,
                ),
            )
            # Audit the change (mirrors PUT /machines) so a UI policy
            # edit shows up on /ui/events like the JSON path does.
            _events_log.record(
                conn,
                kind="machine.created" if existing is None else "machine.upserted",
                summary=(f"{normalised} created" if existing is None else f"{normalised} updated"),
                subject_kind="machine",
                subject_id=normalised,
                actor="operator",
                source_ip=_client_ip(request),
                details={
                    "bty_image_ref": validated.bty_image_ref,
                    "boot_mode": validated.boot_mode,
                    "sanboot_drive": validated.sanboot_drive,
                    "hostname": validated.hostname,
                    "target_disk_serial": validated.target_disk_serial,
                },
            )
            conn.commit()
        publish_state_changed()
        return RedirectResponse("/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.post(
        "/ui/machines/{mac}/delete",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machine_delete(mac: str, request: Request) -> RedirectResponse:
        """Form-style delete. Records a ``machine.deleted`` event
        with the operator's IP so /ui/events is symmetric across
        the JSON API delete + this form delete. Pre-fix the form
        path silently removed the row with no audit trail.

        v0.32.4: the redirect URL carries ``?deleted=<mac>`` on a
        real removal OR ``?missing=<mac>`` when the row was already
        gone (a stale tab on /ui/machines after another operator
        deleted the same MAC, or a hand-typed URL against a never-
        bound MAC). /ui/machines renders the corresponding flash so
        the operator gets feedback either way -- previously a
        no-op delete redirected silently with no signal.
        """
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            deleted = cur.rowcount > 0
            if deleted:
                _events_log.record(
                    conn,
                    kind="machine.deleted",
                    summary=f"{normalised} deleted",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="operator",
                    source_ip=client_ip,
                )
            conn.commit()
        publish_state_changed()
        flash_key = "deleted" if deleted else "missing"
        return RedirectResponse(
            f"/ui/machines?{flash_key}={normalised}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def _render_images_page(request: Request) -> HTMLResponse:
        """Build the context for ``/ui/images`` and render the template.

        The catalog list (``catalog_entries`` rows) is the page's
        primary content. Three add-paths live in the header: upload
        a ``catalog.toml`` (``POST /ui/catalog/upload``), fetch the
        release catalog (``POST /ui/catalog/fetch-release``), or add
        a single URL (``POST /ui/catalog/entries``).

        ``?error=<msg>`` lands in the layout's flash slot (the form-
        style ``POST /ui/catalog/entries`` 303s back with that param on
        validation failure, sha-resolve failure, or duplicate-src 409).
        """
        unified = list_unified_images() if list_unified_images is not None else []
        flash = request.query_params.get("error")
        catalog_manifest_path = str(_config.cfg().catalog_file)
        with _db.open_db(state_path) as conn:
            release_repo = _settings_store.resolve_release_repo(conn)
            catalog_tag = _settings_store.resolve_catalog_tag(conn)
            image_events = _events_log.list_events(conn, subject_kind="catalog", limit=15)
        return render(
            "ui/images.html",
            request,
            unified=unified,
            image_events=image_events,
            manifest_path=catalog_manifest_path,
            release_repo=release_repo,
            catalog_tag=catalog_tag,
            flash=flash,
            flash_kind="danger" if flash else None,
        )

    @app.get(
        "/ui/images",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_images(request: Request) -> HTMLResponse:
        """The image catalog: one row per ``catalog_entries`` row.
        Add-paths (upload TOML / fetch release / add URL) live in the
        page's header; active netboot fetches live on ``/ui/netboot``;
        backups on ``/ui/backups``."""
        return _render_images_page(request)

    @app.get(
        "/ui/backups",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_backups(request: Request) -> HTMLResponse:
        """The Backups worker page: "Back up now" trigger + active
        backups + on-disk listing + schedule summary + recent backup
        activity. The retention number is surfaced alongside the
        schedule so the operator can see how many bundles will be
        kept after each successful backup (retention prunes on every
        completion, regardless of manual / scheduled trigger)."""
        with _db.open_db(state_path) as conn:
            backup_enabled = _settings_store.resolve_backup_enabled(conn)
            backup_cadence = _settings_store.resolve_backup_cadence(conn)
            backup_retention = _settings_store.resolve_backup_retention(conn)
            backup_last_run_at = _settings_store.get_backup_last_run_at(conn)
            backup_events = _events_log.list_events(conn, subject_kind="backup", limit=15)
        backups_on_disk = _backup.list_backups_on_disk(backups_root)
        return render(
            "ui/backups.html",
            request,
            backups_root=str(backups_root),
            backup_enabled=backup_enabled,
            backup_cadence=backup_cadence,
            backup_retention=backup_retention,
            backup_last_run_at=backup_last_run_at,
            backup_events=backup_events,
            backups_on_disk=backups_on_disk,
        )

    @app.get(
        "/ui/backups/{backup_id}/download",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_backups_download(backup_id: str) -> FileResponse:
        """Serve the bundle's ``inventory.json`` for the operator to
        download to their laptop.

        v3 bundles are one file, so this just streams ``inventory.json``
        as ``application/json``. The Content-Disposition filename is
        ``<backup_id>.json`` so the file lands with a self-describing
        name even when the browser saves several at once.

        Validates the ``backup_id`` against the ISO-8601-slug format
        before touching the filesystem (so a request like
        ``/ui/backups/../etc/download`` can't traverse out of
        ``backups_root``). Returns ``404`` if the slug is malformed,
        the bundle directory is missing, or the bundle has no
        ``inventory.json``.
        """
        if not _backup.is_valid_backup_id(backup_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"backup not found: {backup_id!r}",
            )
        bundle = backups_root / backup_id
        inventory = bundle / "inventory.json"
        if not inventory.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"backup not found: {backup_id!r}",
            )
        return FileResponse(
            inventory,
            media_type="application/json",
            filename=f"{backup_id}.json",
        )

    @app.delete(
        "/ui/backups/{backup_id}",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_backups_delete(backup_id: str) -> dict[str, Any]:
        """Operator-initiated delete of one on-disk backup bundle.

        Distinct from the cancel-in-flight route on ``/workers/backups``
        -- this targets a completed bundle sitting in ``backups_root``,
        not a queued / running job. Validates the slug before touching
        the filesystem (same guard as the download endpoint), then
        ``rmtree`` + records a ``backup.deleted`` audit event. The
        UI then re-renders the page to reflect the new list.
        """
        if not _backup.is_valid_backup_id(backup_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"backup not found: {backup_id!r}",
            )
        try:
            snapshot = _backup.delete_bundle(state_path, backups_root, backup_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"backup not found: {backup_id!r}",
            ) from exc
        return {
            "backup_id": snapshot.backup_id,
            "machines": snapshot.machines,
            "bytes_on_disk": snapshot.bytes_on_disk,
        }

    @app.post(
        "/ui/catalog/entries",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_catalog_entry_add(
        request: Request,
        image_url: Annotated[str, Form()],
        sha_url: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Form-style POST behind the "Add image by URL" form on
        /ui/images. Routes through the same logic as the JSON API
        ``POST /catalog/entries``: optional sha_url resolves to a
        sha256, optional ``Content-Length`` HEAD probes size, the
        row lands in ``catalog_entries``. Operator-friendly empty-
        string in ``sha_url`` is treated as None.

        Validation runs through the same :class:`CatalogEntryAdd`
        Pydantic model the JSON endpoint uses, so the form rejects
        ``ftp://`` / host-less URLs / non-http schemes identically.
        """
        from bty import catalog as _catalog
        from bty import oras as _oras
        from bty.web._app import _head_content_length  # local import: avoid cycle at module load

        cleaned_sha_url = sha_url.strip() or None
        # Apply the same Pydantic validation the JSON API uses
        # (URL scheme + host pattern, both fields). Pydantic
        # raises a ``ValidationError`` (subclass of ``ValueError``)
        # if a pattern doesn't match.
        try:
            validated = CatalogEntryAdd(image_url=image_url, sha_url=cleaned_sha_url)
        except ValueError as exc:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote(f"validation failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        image_url = validated.image_url
        cleaned_sha_url = validated.sha_url

        # Variables shared across the oras / http branches. Declared
        # up front so mypy sees a single binding (the oras branch
        # narrows ``sha256`` / ``fmt`` / ``size_bytes`` to concrete
        # types, which would clash with the http branch's re-
        # declarations).
        sha256: str | None = None
        fmt: str | None = None
        size_bytes: int | None = None
        # ``oras://`` short-circuit: resolve via bty.oras and write
        # the row with the manifest-derived digest / name / size /
        # format. Mirrors the JSON endpoint's oras branch verbatim.
        if image_url.startswith("oras://"):
            try:
                resolved = _oras.resolve_ref(image_url)
            except _oras.OrasError as exc:
                return RedirectResponse(
                    "/ui/images?error="
                    + urllib.parse.quote(f"oras resolve failed: {exc}", safe=""),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            sha256 = resolved.digest.removeprefix("sha256:")
            ref = _oras.parse_ref(image_url)
            name = resolved.title or ref.repository.rsplit("/", 1)[-1]
            fmt = bty_images.detect_format(Path(name)) or "img.gz"
            size_bytes = resolved.size
            now = _now_iso()
            try:
                bty_image_ref = _catalog.image_ref_for_src(image_url)
            except ValueError as exc:
                return RedirectResponse(
                    "/ui/images?error=" + urllib.parse.quote(f"invalid image_url: {exc}", safe=""),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            with _db.open_db(state_path) as conn:
                try:
                    conn.execute(
                        "INSERT INTO catalog_entries "
                        "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                        "format, size_bytes, description, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bty_image_ref,
                            image_url,
                            sha256,
                            name,
                            None,
                            fmt,
                            size_bytes,
                            None,
                            now,
                        ),
                    )
                    _events_log.record(
                        conn,
                        kind="catalog.entry.added",
                        summary=f"catalog entry added (oras): {name}",
                        subject_kind="catalog",
                        subject_id=image_url,
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
                except sqlite3.IntegrityError:
                    return RedirectResponse(
                        "/ui/images?error=already+exists",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

        # Filename-required: same rule as the JSON ``add_catalog_entry``
        # endpoint. ``https://example.com`` and
        # ``https://example.com/`` produce empty ``Path.name`` and
        # leave the catalog row with no useful display label.
        name = Path(urllib.parse.urlparse(image_url).path).name
        if not name:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    "image_url must end in a filename component "
                    "(e.g. https://example.com/path/foo.img.gz)",
                    safe="",
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        if cleaned_sha_url is not None:
            try:
                sha256 = _catalog.fetch_sha256_for_url(image_url, cleaned_sha_url)
            except _catalog.CatalogError as exc:
                # ``urllib.parse.quote`` so the redirect URL is
                # well-formed regardless of the exception text
                # (which can carry spaces, special chars, or even
                # newlines if upstream's reason phrase is weird).
                return RedirectResponse(
                    "/ui/images?error=" + urllib.parse.quote(f"sha resolve failed: {exc}", safe=""),
                    status_code=status.HTTP_303_SEE_OTHER,
                )

        fmt = bty_images.detect_format(Path(name))
        size_bytes = _head_content_length(image_url)
        now = _now_iso()
        try:
            bty_image_ref = _catalog.image_ref_for_src(image_url)
        except ValueError as exc:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote(f"invalid image_url: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        with _db.open_db(state_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO catalog_entries "
                    "(bty_image_ref, src, disk_image_sha, name, sha_url, "
                    "format, size_bytes, description, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bty_image_ref,
                        image_url,
                        sha256,
                        name,
                        cleaned_sha_url,
                        fmt,
                        size_bytes,
                        None,
                        now,
                    ),
                )
                _events_log.record(
                    conn,
                    kind="catalog.entry.added",
                    summary=f"catalog entry added: {name}",
                    subject_kind="catalog",
                    subject_id=image_url,
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
            except sqlite3.IntegrityError:
                return RedirectResponse(
                    "/ui/images?error=already+exists",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    # ----- boot artifacts ------------------------------------------------

    def _render_netboot_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        """The netboot artifacts inventory (present/missing, size,
        sha256, download), the Fetch-artifacts trigger + active-jobs
        table, plus the TFTP daemon control.

        v0.41.2+: the old ``/ui/downloads`` page collapsed into this
        one since bty-web's only download workload is the netboot
        artifact trio. ``artifacts_all_cached`` lets the template
        disable the Fetch artifacts button when the trio + sha256
        manifest are all present locally; it re-enables when one
        disappears (operator ``rm``, state-dir migrate, etc.).
        Router-side DHCP / PXE cheatsheet lives on the Settings page.
        """
        artifacts = _releases.inspect_boot_dir(boot_root)
        artifacts_all_cached = bool(artifacts) and all(a.present for a in artifacts)
        with _db.open_db(state_path) as conn:
            release_repo = _settings_store.resolve_release_repo(conn)
            netboot_tag = _settings_store.resolve_netboot_tag(conn)
            # Recent netboot activity for the page's "Activity" table.
            boot_events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
        return render(
            "ui/netboot.html",
            request,
            artifacts=artifacts,
            artifacts_all_cached=artifacts_all_cached,
            artifact_shas=_releases.boot_artifact_shas(boot_root),
            release_repo=release_repo,
            netboot_tag=netboot_tag,
            boot_events=boot_events,
            flash=flash,
            flash_kind=flash_kind,
            tftp=_sysconfig.tftp_status(),
            # Diagnostic probe: TFTP host reachable + ipxe.efi present?
            # Target resolves from config (explicit [netboot]
            # tftp_probe_host, else the withcache URL host) -- one source
            # of truth, so an upgrade that drops an env var can't silently
            # point this at loopback. The render is request-time so a
            # config / sidecar change reflects on the next page load.
            # ~1.5s in the worst case (probe timeout); fast path < 5 ms.
            tftp_probe=_sysconfig.tftp_probe(host=_config.cfg().effective_tftp_probe_host),
        )

    @app.get(
        "/ui/netboot",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_netboot(request: Request) -> HTMLResponse:
        return _render_netboot_page(request)

    # ----- event log -----------------------------------------------------

    @app.get(
        "/ui/events",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_events(
        request: Request,
        kind: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        source_ip: str | None = None,
        failed: str | None = None,
        before_id: int | None = None,
    ) -> HTMLResponse:
        """Event log page.

        Cursor pagination: each page shows ``_PAGE_SIZE`` rows;
        the "Older" link carries ``before_id`` = the smallest id
        on the current page so the next page picks up where this
        one ended. New events arriving while the operator pages
        through don't disturb the cursor (they get id values
        higher than the cursor and would only appear on page 1).

        Empty filter values come in as empty strings from the form;
        normalise them to ``None`` so the SQL builder skips the
        clause.
        """
        page_size = 50
        kind_norm = kind or None
        subject_kind_norm = subject_kind or None
        subject_id_norm = subject_id or None
        actor_norm = actor or None
        source_ip_norm = source_ip or None
        # ``?failed=1`` is the cross-kind "show me all failures"
        # toggle. Anything truthy enables it; absent / empty
        # leaves it off.
        failed_only = bool(failed)
        with _db.open_db(state_path) as conn:
            events = _events_log.list_events(
                conn,
                kind=kind_norm,
                subject_kind=subject_kind_norm,
                subject_id=subject_id_norm,
                actor=actor_norm,
                source_ip=source_ip_norm,
                failed_only=failed_only,
                before_id=before_id,
                limit=page_size,
            )
        # The "Older" link is meaningful only if we got a full
        # page of results; if we got fewer, there's nothing
        # older to fetch.
        older_url: str | None = None
        if len(events) == page_size:
            params = {
                "kind": kind_norm or "",
                "subject_kind": subject_kind_norm or "",
                "subject_id": subject_id_norm or "",
                "actor": actor_norm or "",
                "source_ip": source_ip_norm or "",
                "failed": "1" if failed_only else "",
                "before_id": str(events[-1].id),
            }
            non_empty = {k: v for k, v in params.items() if v}
            older_url = "/ui/events?" + urllib.parse.urlencode(non_empty)
        return render(
            "ui/events.html",
            request,
            events=events,
            kind=kind_norm,
            subject_kind=subject_kind_norm,
            subject_id=subject_id_norm,
            actor=actor_norm,
            source_ip=source_ip_norm,
            failed_only=failed_only,
            known_kinds=KNOWN_EVENT_KINDS,
            known_subject_kinds=KNOWN_SUBJECT_KINDS,
            known_actors=KNOWN_ACTORS,
            older_url=older_url,
        )

    @app.post(
        "/ui/events/acknowledge",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_events_acknowledge(
        ids: Annotated[list[int], Form()] = [],  # noqa: B006 (FastAPI form-list default)
        acknowledged: Annotated[str, Form()] = "1",
    ) -> dict[str, Any]:
        """Set the acknowledged flag on one or more events. Drives both
        the per-row toggle (one id) and the bulk Acknowledge / Clear
        buttons (the checked ids). ``acknowledged`` is "1" to
        acknowledge, "0" to clear (un-acknowledge).

        Acknowledging a failure clears it from the dashboard Health
        Monitoring tripwire (which counts only unacknowledged failures)
        without deleting the audit row; clearing puts it back. Returns
        the number of rows updated; the page reloads itself via JS.
        """
        value = acknowledged not in ("0", "false", "")
        updated = 0
        with _db.open_db(state_path) as conn:
            for event_id in ids:
                if _events_log.set_acknowledged(conn, event_id, value):
                    updated += 1
            conn.commit()
        return {"updated": updated, "acknowledged": value}

    # ----- settings -------------------------------------------------------

    def _config_row(
        label: str,
        value: object,
        env: str | None,
        default: str,
        *,
        section: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        """One config row for the Settings page.

        v0.42+: when ``section`` + ``key`` are passed, the row is an
        editable Config field; the template renders an inline form
        that POSTs to ``/ui/settings/config/edit`` and write-backs to
        ``cfg.primary_toml`` via ``_config.save_value``. The row is
        read-only iff (a) no ``section`` / ``key`` is set (display-
        only diagnostic like "bty version") OR (b) the value is
        currently sourced from an env var (the env wins; editing
        the TOML wouldn't take effect until env is unset).

        ``source`` is read from the LoadedConfig's per-key
        ``sources`` map so the badge reflects the actual provenance
        chain, not a heuristic.
        """
        loaded = _config.active_config()
        if section and key:
            dotted = f"{section}.{key}"
            raw_source = loaded.sources.get(dotted, "default")
        else:
            raw_source = "default"  # display-only row (e.g. bty version)
        # Squash the source string to a coarse bucket the template
        # branches on. ``raw_source`` may be ``"toml(/etc/bty/bty.toml)"``
        # or ``"env(BTY_ADMIN_PASSWORD)"`` -- keep the detail for the
        # tooltip but show a one-word badge.
        if raw_source.startswith("env("):
            source_bucket = "env"
        elif raw_source.startswith("toml("):
            source_bucket = "toml"
        else:
            source_bucket = "default"
        editable = section is not None and key is not None and source_bucket != "env"
        return {
            "label": label,
            "value": str(value),
            "env": env,
            "default": default,
            "source": source_bucket,
            "source_detail": raw_source,
            "section": section,
            "key": key,
            "editable": editable,
        }

    def _render_settings_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        # The Settings page is a read-only map of every magic value
        # that configures bty-web (where each comes from: env var /
        # derived path / default), plus the one editable card: the
        # upstream sources (release repo + catalog URL), persisted in
        # state.db so they survive a restart.
        cfg = _config.cfg()
        state_dir = state_path.parent
        catalog_file = str(cfg.catalog_file)
        with _db.open_db(state_path) as conn:
            release_repo = _settings_store.resolve_release_repo(conn)
            catalog_url = _settings_store.resolve_catalog_url(conn)
            catalog_tag = _settings_store.resolve_catalog_tag(conn)
            netboot_tag = _settings_store.resolve_netboot_tag(conn)
            repo_override = _settings_store.get(conn, _settings_store.KEY_RELEASE_REPO)
            catalog_tag_override = _settings_store.get(conn, _settings_store.KEY_CATALOG_TAG)
            netboot_tag_override = _settings_store.get(conn, _settings_store.KEY_NETBOOT_TAG)
        upstream = {
            "release_repo": release_repo,
            "release_repo_override": repo_override,
            "release_repo_default": _settings_store.default_release_repo(),
            "catalog_tag": catalog_tag,
            "catalog_tag_override": catalog_tag_override,
            "catalog_tag_default": _settings_store.DEFAULT_TAG,
            "catalog_url": catalog_url,  # derived view (repo + catalog_tag)
            "netboot_tag": netboot_tag,
            "netboot_tag_override": netboot_tag_override,
            "netboot_tag_default": _settings_store.DEFAULT_TAG,
        }
        config_groups = [
            {
                "title": "Identity",
                "icon": "info-circle",
                "rows": [
                    _config_row("bty version", bty.__version__, None, "(package version)"),
                    _config_row("Service user", service_user, None, "(launch argument)"),
                    _config_row(
                        "Project / release notes",
                        "https://github.com/safl/bty",
                        None,
                        "https://github.com/safl/bty",
                    ),
                ],
            },
            {
                "title": "Storage paths",
                "icon": "hdd",
                "rows": [
                    _config_row(
                        "State directory",
                        cfg.paths.state_dir,
                        "BTY_PATHS_STATE_DIR",
                        "/var/lib/bty",
                        section="paths",
                        key="state_dir",
                    ),
                    _config_row("Database", state_path, None, "<state dir>/state.db"),
                    _config_row(
                        "Netboot directory",
                        cfg.paths.boot_dir or str(cfg.boot_dir),
                        "BTY_PATHS_BOOT_DIR",
                        "<state dir>/boot",
                        section="paths",
                        key="boot_dir",
                    ),
                    _config_row(
                        "Catalog manifest",
                        cfg.paths.catalog_file or catalog_file,
                        "BTY_PATHS_CATALOG_FILE",
                        "<state dir>/catalog.toml",
                        section="paths",
                        key="catalog_file",
                    ),
                    _config_row(
                        "Session secret",
                        cfg.server.session_secret or str(state_dir / "session-secret"),
                        "BTY_SERVER_SESSION_SECRET",
                        "<state dir>/session-secret",
                        section="server",
                        key="session_secret",
                    ),
                ],
            },
            {
                "title": "Network",
                "icon": "hdd-network",
                "rows": [
                    _config_row(
                        "Bind host",
                        cfg.server.host,
                        "BTY_SERVER_HOST",
                        "0.0.0.0",
                        section="server",
                        key="host",
                    ),
                    _config_row(
                        "Bind port",
                        str(cfg.server.port),
                        "BTY_SERVER_PORT",
                        "8080",
                        section="server",
                        key="port",
                    ),
                    _config_row(
                        "Trust X-Forwarded-For",
                        cfg.server.trusted_proxy or "(off)",
                        "BTY_SERVER_TRUSTED_PROXY",
                        "(off)",
                        section="server",
                        key="trusted_proxy",
                    ),
                    _config_row(
                        "TFTP probe target",
                        cfg.netboot.tftp_probe_host,
                        "BTY_NETBOOT_TFTP_PROBE_HOST",
                        # When unset, the probe derives the target from
                        # the withcache URL host -- show that resolved
                        # value as the default so the operator sees where
                        # it actually aims, not a misleading 127.0.0.1.
                        cfg.effective_tftp_probe_host,
                        section="netboot",
                        key="tftp_probe_host",
                    ),
                    _config_row(
                        "withcache base URL",
                        cfg.withcache.url or "(unset)",
                        "BTY_WITHCACHE_URL",
                        "(unset; bty-web streams from origin)",
                        section="withcache",
                        key="url",
                    ),
                ],
            },
            {
                "title": "Background workers",
                "icon": "cpu",
                "rows": [
                    _config_row(
                        "Release-fetch user agent",
                        _releases.DEFAULT_USER_AGENT,
                        None,
                        "bty-web release-fetcher",
                    ),
                ],
            },
        ]
        # Network context for the DHCP / PXE cheatsheet (moved here from
        # the Netboot page): the server's interfaces + the address the
        # operator points their router's Next-Server at. Prefer the
        # configured advertised host (the withcache URL's host -- the
        # address clients actually reach) over interface sniffing, which
        # inside a bridge-network container sees container-internal NICs,
        # not the host's LAN address.
        interfaces = _sysconfig.list_interfaces()
        primary = next((i for i in interfaces if i.ipv4), interfaces[0] if interfaces else None)
        suggested_host = _config.cfg().advertised_host or (
            primary.ipv4 if primary and primary.ipv4 else None
        )
        # Backup-schedule context for the Backup schedule card. Re-opens
        # the DB; cheap and keeps the read close to where it's rendered.
        with _db.open_db(state_path) as conn:
            backup_enabled = _settings_store.resolve_backup_enabled(conn)
            backup_cadence = _settings_store.resolve_backup_cadence(conn)
            backup_retention_count = _settings_store.resolve_backup_retention(conn)
            backup_last_run_at = _settings_store.get_backup_last_run_at(conn)
        return render(
            "ui/settings.html",
            request,
            flash=flash,
            flash_kind=flash_kind,
            upstream=upstream,
            config_groups=config_groups,
            interfaces=interfaces,
            primary=primary,
            suggested_host=suggested_host,
            boot_root=str(boot_root),
            backups_root=str(backups_root),
            backup_enabled=backup_enabled,
            backup_cadence=backup_cadence,
            backup_cadences=_settings_store.BACKUP_CADENCES,
            backup_retention_count=backup_retention_count,
            backup_last_run_at=backup_last_run_at,
            missing_netboot_artifacts=_releases.missing_netboot_artifacts(boot_root),
            # Provenance / write-target info for the editable config
            # rows: the path the Settings POST handler writes to (or
            # None when the operator hasn't installed a TOML yet).
            config_primary_toml=(
                str(_config.active_config().primary_toml)
                if _config.active_config().primary_toml is not None
                else None
            ),
            config_loaded_files=[str(p) for p in _config.active_config().loaded_files],
        )

    @app.get(
        "/ui/settings",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings(request: Request, saved: str | None = None) -> HTMLResponse:
        # ``saved`` carries which form just submitted (so the success
        # banner can be specific). Upstream POSTs back ``?saved=upstream``;
        # backup POSTs back ``?saved=backup``. Unknown values render the
        # page without a banner -- a hand-crafted ``?saved=foo`` won't
        # echo arbitrary strings into the UI.
        flash_map = {
            "upstream": "Upstream sources saved.",
            "backup": "Backup schedule saved.",
        }
        flash = flash_map.get(saved or "")
        return _render_settings_page(request, flash=flash, flash_kind="success" if flash else None)

    @app.get(
        "/ui/account",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_account(request: Request) -> HTMLResponse:
        # Operator account page (reached via the user-bar gear icon):
        # authentication is an operator concern, kept separate from the
        # bty-config Settings page. version + service_user come from the
        # global render context.
        return render("ui/account.html", request)

    @app.post(
        "/ui/settings/config/edit",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_config_edit(
        request: Request,
        section: Annotated[str, Form()] = "",
        key: Annotated[str, Form()] = "",
        value: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Persist a single config edit from the Settings page into
        the operator's bty.toml.

        Per-row inline edit form on /ui/settings posts ``section`` +
        ``key`` + ``value``. The handler validates the key is part
        of the Config schema (no arbitrary write paths) + the
        section/key combo IS editable (not env-overridden +
        ``primary_toml`` is writable), then calls
        ``_config.save_value`` to round-trip through tomlkit.
        Coerces ``value`` to int when the schema field is typed as
        int (``server.port`` / ``tuning.*``); empty string is treated
        as "clear" and removes the override (reverts to default).

        Records ``settings.config.updated`` (or ``.failed``) so the
        audit log carries before/after for the bty-toml edit
        trail. The next-reload picks up the change; bty-web reloads
        on restart, OR the operator can call this endpoint and the
        SAME process reloads the active config inline so the change
        shows up on the next page render.
        """
        from dataclasses import fields
        from typing import get_type_hints

        from bty.web._config import Config as _ConfigCls

        client_ip = _client_ip(request)
        section = section.strip()
        key = key.strip()
        value = value.strip()

        # Validate section/key against the schema -- the form can
        # only target keys that exist in Config. Stops a hand-
        # crafted POST from writing arbitrary TOML keys.
        section_types = get_type_hints(_ConfigCls)
        if section not in section_types:
            return RedirectResponse(
                "/ui/settings?error=unknown+section",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        valid_keys = {f.name: f.type for f in fields(section_types[section])}
        if key not in valid_keys:
            return RedirectResponse(
                "/ui/settings?error=unknown+key",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        loaded = _config.active_config()
        primary = loaded.primary_toml
        if primary is None:
            return RedirectResponse(
                "/ui/settings?error=no+writable+bty.toml",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # Refuse to write a key that's currently env-overridden --
        # the TOML write would silently no-op as the env wins. The
        # template hides edit forms for those rows, but a hand-
        # crafted POST could still try.
        dotted = f"{section}.{key}"
        cur_source = loaded.sources.get(dotted, "default")
        if cur_source.startswith("env("):
            return RedirectResponse(
                f"/ui/settings?error=env+override+for+{dotted}",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # Coerce value to the schema's declared type. Int fields
        # accept blank (treated as "no override" -- delete the key
        # from the TOML so the default takes effect).
        declared = valid_keys[key]
        coerced: str | int
        try:
            if value == "":
                coerced = ""
            elif declared in (int, "int"):
                coerced = int(value)
            else:
                coerced = value
        except ValueError:
            return RedirectResponse(
                f"/ui/settings?error=invalid+value+for+{dotted}",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # Capture old value for the audit row before the write.
        old_value = _read_dotted(loaded.cfg, section, key)
        try:
            if coerced == "":
                # Remove the key so the default takes over. Use
                # ``tomlkit``'s in-place delete to preserve comments.
                _delete_toml_key(primary, section, key)
            else:
                _config.save_value(primary, section, key, coerced)
        except OSError as exc:
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="settings.config.failed",
                    summary=f"bty.toml write failed: {exc}",
                    subject_kind="settings",
                    subject_id=dotted,
                    actor="operator",
                    source_ip=client_ip,
                    details={"section": section, "key": key, "error": str(exc)},
                )
                conn.commit()
            return RedirectResponse(
                f"/ui/settings?error=write+failed+{dotted}",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # Reload the active config so the next render sees the new
        # value without a process restart.
        _config.set_active_config(_config.load_config(None))

        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="settings.config.updated",
                summary=f"bty.toml: {dotted} = {coerced!r}",
                subject_kind="settings",
                subject_id=dotted,
                actor="operator",
                source_ip=client_ip,
                details={
                    "section": section,
                    "key": key,
                    "old": str(old_value),
                    "new": "" if coerced == "" else str(coerced),
                    "path": str(primary),
                },
            )
            conn.commit()

        return RedirectResponse(
            "/ui/settings?saved=config",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/ui/settings/upstream",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_upstream(
        request: Request,
        release_repo: Annotated[str, Form()] = "",
        catalog_tag: Annotated[str, Form()] = "",
        netboot_tag: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Save (or clear) the three editable upstream overrides:
        release repo, catalog tag, netboot tag. An empty field clears
        that override, reverting to the default. All three take effect
        on the next fetch without a restart, since the fetch sites
        resolve from this store at request time."""
        rr = release_repo.strip()
        ct = catalog_tag.strip()
        nt = netboot_tag.strip()
        with _db.open_db(state_path) as conn:
            # Snapshot the previous explicit overrides (None = was on
            # default) BEFORE the writes so the audit event can carry
            # both before + after.
            old_rr = _settings_store.get(conn, _settings_store.KEY_RELEASE_REPO)
            old_ct = _settings_store.get(conn, _settings_store.KEY_CATALOG_TAG)
            old_nt = _settings_store.get(conn, _settings_store.KEY_NETBOOT_TAG)
            if rr:
                _settings_store.set_value(conn, _settings_store.KEY_RELEASE_REPO, rr)
            else:
                _settings_store.clear(conn, _settings_store.KEY_RELEASE_REPO)
            if ct:
                _settings_store.set_value(conn, _settings_store.KEY_CATALOG_TAG, ct)
            else:
                _settings_store.clear(conn, _settings_store.KEY_CATALOG_TAG)
            if nt:
                _settings_store.set_value(conn, _settings_store.KEY_NETBOOT_TAG, nt)
            else:
                _settings_store.clear(conn, _settings_store.KEY_NETBOOT_TAG)
            _events_log.record(
                conn,
                kind="settings.upstream.updated",
                summary=(
                    f"upstream sources set: repo={rr or '(default)'}, "
                    f"catalog_tag={ct or '(default)'}, "
                    f"netboot_tag={nt or '(default)'}"
                ),
                subject_kind="settings",
                subject_id="upstream",
                actor="operator",
                source_ip=_client_ip(request),
                details={
                    "release_repo": {"old": old_rr, "new": rr or None},
                    "catalog_tag": {"old": old_ct, "new": ct or None},
                    "netboot_tag": {"old": old_nt, "new": nt or None},
                },
            )
            conn.commit()
        return RedirectResponse(
            "/ui/settings?saved=upstream", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post(
        "/ui/settings/backup",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_backup(
        request: Request,
        backup_enabled: Annotated[str, Form()] = "",
        backup_cadence: Annotated[str, Form()] = "manual",
        backup_retention_count: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Save the scheduled-backup knobs.

        Form fields:
          backup_enabled         -- checkbox; present (any truthy) =
                                    enabled, absent (HTML omits unchecked
                                    boxes entirely) = disabled.
          backup_cadence         -- one of BACKUP_CADENCES; an unknown
                                    value returns 422 -- no soft fallback.
          backup_retention_count -- positive int (>= 1); non-numeric or
                                    sub-1 returns 422.

        Effects propagate within the scheduler's next tick (60s) -- no
        restart needed.
        """
        cadence = backup_cadence.strip()
        if cadence not in _settings_store.BACKUP_CADENCES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"backup_cadence={cadence!r} is not a known cadence "
                    f"(expected one of {', '.join(_settings_store.BACKUP_CADENCES)})"
                ),
            )
        try:
            retention = int(backup_retention_count)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"backup_retention_count={backup_retention_count!r} is not an integer",
            ) from exc
        if retention < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"backup_retention_count={retention} is out of range (must be >= 1)",
            )
        enabled = bool(backup_enabled)
        with _db.open_db(state_path) as conn:
            _settings_store.set_value(
                conn, _settings_store.KEY_BACKUP_ENABLED, "1" if enabled else "0"
            )
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_CADENCE, cadence)
            _settings_store.set_value(conn, _settings_store.KEY_BACKUP_RETENTION, str(retention))
            _events_log.record(
                conn,
                kind="settings.backup.updated",
                summary=(
                    f"backup schedule: enabled={enabled}, cadence={cadence}, retention={retention}"
                ),
                subject_kind="settings",
                subject_id="backup",
                actor="operator",
                source_ip=_client_ip(request),
            )
            conn.commit()
        return RedirectResponse(
            "/ui/settings?saved=backup#backup-schedule",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/ui/netboot/fetch-release",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_netboot_fetch(
        request: Request,
        tag: Annotated[str, Form()] = "latest",
    ) -> HTMLResponse:
        # Best-effort fetch; on success render the page with a green
        # flash, on failure with a red one. We do NOT propagate the
        # underlying urllib / network exception further.
        resolved_tag = tag or "latest"
        client_ip = _client_ip(request)
        try:
            result = _releases.fetch_release(boot_root, tag=resolved_tag)
        except _releases.FetchError as exc:
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="netboot.artifacts.fetch.failed",
                    summary=f"boot release {resolved_tag!r} fetch failed: {exc}",
                    subject_kind="netboot",
                    subject_id=resolved_tag,
                    actor="operator",
                    source_ip=client_ip,
                    details={"tag": resolved_tag, "error": str(exc)},
                )
                conn.commit()
            return _render_netboot_page(
                request,
                flash=f"Fetch failed: {exc}",
                flash_kind="danger",
            )
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="netboot.artifacts.fetched",
                summary=f"boot release {resolved_tag!r} fetched from {result.base_url}",
                subject_kind="netboot",
                subject_id=resolved_tag,
                actor="operator",
                source_ip=client_ip,
                details={
                    "tag": resolved_tag,
                    "base_url": result.base_url,
                    "total_bytes": result.total_bytes,
                    "artifacts": list(result.artifacts),
                },
            )
            conn.commit()
        return _render_netboot_page(
            request,
            flash=(
                f"Fetched {len(result.artifacts)} artifacts ({result.total_bytes:,} bytes) "
                f"from {result.base_url}"
            ),
            flash_kind="success",
        )


# ---------- helpers ---------------------------------------------------------


def dashboard_counts_context(conn: Any, unified: list[bty_images.UnifiedImage]) -> dict[str, Any]:
    """Build the LIVE dashboard-panel context: machine summary +
    image breakdown. Shared by ``ui_dashboard`` (initial render) and
    the SSE ``render_dashboard_panels`` publisher (in _app) so the two
    panels can never drift.

    ``unified`` is the merged catalog listing. The image breakdown
    counts entries that carry each kind of source: ``local`` is a
    file on disk, ``http`` an http(s):// source, ``oras`` an OCI
    registry ref; ``cached`` is the content-addressed-cache hit flag.
    An entry can count toward several (e.g. a local file that's also
    a catalog http entry).
    """
    machine_count = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
    unassigned_count = conn.execute(
        "SELECT COUNT(*) FROM machines WHERE bty_image_ref IS NULL"
    ).fetchone()[0]
    last_seen = conn.execute("SELECT MAX(last_seen_at) FROM machines").fetchone()[0]
    last_flashed = conn.execute("SELECT MAX(last_flashed_at) FROM machines").fetchone()[0]
    return {
        "machine_count": machine_count,
        "unassigned_count": unassigned_count,
        "last_seen": last_seen,
        "last_flashed": last_flashed,
        "img_total": len(unified),
        "img_cached": sum(1 for u in unified if u.cached),
        "img_local": sum(1 for u in unified if any(s.kind == "local" for s in u.sources)),
        "img_http": sum(
            1
            for u in unified
            if any(s.location.startswith(("http://", "https://")) for s in u.sources)
        ),
        "img_oras": sum(
            1 for u in unified if any(s.location.startswith("oras://") for s in u.sources)
        ),
    }


def lshw_highlights(blob: str | None) -> dict[str, Any] | None:
    """Pull a few display highlights out of a stored ``lshw -json``
    blob: CPU model, total RAM, and the NIC list (name / MAC /
    product). Returns ``None`` when there's no blob or it won't parse,
    so the Machine view can hide the Hardware card. Shallow on purpose
    -- the raw download (GET /machines/{mac}/lshw.json) is the real
    artifact; this is just a glance."""
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return None
    roots = data if isinstance(data, list) else [data]
    cpu: str | None = None
    cpu_cores: int | None = None
    container_bytes: int | None = None  # size on the "System Memory" node
    bank_bytes = 0  # sum of populated DIMM/bank sizes
    mem_modules = 0  # count of populated banks
    nics: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        nonlocal cpu, cpu_cores, container_bytes, bank_bytes, mem_modules
        if not isinstance(node, dict):
            return
        cls = node.get("class")
        ident = node.get("id", "")
        ident = ident if isinstance(ident, str) else ""
        if cls == "processor" and cpu is None:
            prod = node.get("product") or node.get("description")
            if isinstance(prod, str):
                cpu = prod
            conf = node.get("configuration")
            if isinstance(conf, dict):
                # lshw reports cores as a string ("4"); be liberal.
                raw_cores = conf.get("cores") or conf.get("enabledcores")
                if isinstance(raw_cores, int):
                    cpu_cores = raw_cores
                elif isinstance(raw_cores, str) and raw_cores.isdigit():
                    cpu_cores = int(raw_cores)
        elif cls == "memory":
            size = node.get("size")
            # A populated DIMM/bank (``bank:0`` etc.) -> count it + sum it.
            # The "System Memory" container (``memory``) carries the total
            # on systems that don't break out banks; keep it as fallback.
            if ident.startswith("bank") and isinstance(size, int) and size > 0:
                bank_bytes += size
                mem_modules += 1
            elif "memory" in ident and isinstance(size, int):
                container_bytes = (container_bytes or 0) + size
        elif cls == "network":
            name = node.get("logicalname")
            if isinstance(name, list):
                name = ", ".join(str(n) for n in name)
            nics.append(
                {
                    "name": name if isinstance(name, str) else None,
                    "mac": node.get("serial") if isinstance(node.get("serial"), str) else None,
                    "product": node.get("product") or node.get("description"),
                }
            )
        for child in node.get("children") or []:
            _walk(child)

    for root in roots:
        _walk(root)

    # Prefer the summed banks (works even when the container node has no
    # size -- the case the old "container only" parser missed); fall back
    # to the container total.
    mem_bytes = bank_bytes or container_bytes
    mem_h = f"{mem_bytes / 1024**3:.1f} GiB" if mem_bytes else None
    return {
        "cpu": cpu,
        "cpu_cores": cpu_cores,
        "memory": mem_h,
        "mem_modules": mem_modules or None,
        "nics": nics,
    }


def _read_dotted(cfg_obj: Any, section: str, key: str) -> Any:
    """Read ``cfg.<section>.<key>`` -- the path-walk version of
    Python attribute access. Used by the Settings edit handler to
    capture the BEFORE value for the audit row without hand-rolling
    a switch per section."""
    section_obj = getattr(cfg_obj, section)
    return getattr(section_obj, key)


def _delete_toml_key(path: Path, section: str, key: str) -> None:
    """Remove ``[section] key`` from the TOML at ``path``, preserving
    operator formatting via tomlkit. No-op if the file / section /
    key doesn't exist (the caller already validated; this is the
    on-disk path).

    Atomic via tempfile + rename, same shape as ``save_value``."""
    import os

    import tomlkit

    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())
    if section not in doc or key not in doc[section]:
        return
    del doc[section][key]
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Decode a sqlite3.Row of ``machines`` into a plain dict.

    ``known_disks`` is stored as JSON text in the column;
    decode it here so the Jinja template can iterate it
    directly. Bad JSON degrades to ``None`` so a stale row
    can't 500 the detail page.

    The columns are part of the current schema -- ``_db.init_db``
    auto-rotates any stale-schema ``state.db`` to ``.bak`` on
    startup and creates a fresh one stamped with the running
    version, so by the time this function runs the row always
    matches the schema defined in ``_db.SCHEMA``. We can index
    directly.
    """
    raw_disks = row["known_disks"]
    parsed_disks: list[dict[str, Any]] | None = None
    if raw_disks:
        import json as _json

        try:
            decoded = _json.loads(raw_disks)
            if isinstance(decoded, list):
                parsed_disks = decoded
        except (TypeError, ValueError):
            parsed_disks = None
    return {
        "mac": row["mac"],
        "bty_image_ref": row["bty_image_ref"],
        "hostname": row["hostname"],
        "discovered_at": row["discovered_at"],
        "last_seen_at": row["last_seen_at"],
        "last_seen_ip": row["last_seen_ip"],
        "boot_mode": row["boot_mode"],
        # Drives the derived "Boot state" badge (the boot_state Jinja
        # filter) in the list + detail views.
        "saw_flasher_boot": _db.row_value(row, "saw_flasher_boot", 0),
        "sanboot_drive": _db.row_value(row, "sanboot_drive"),
        "last_flashed_at": row["last_flashed_at"],
        "known_disks": parsed_disks,
        "known_disks_at": row["known_disks_at"],
        # Additive columns: guard with key check so an older row mid-
        # migration can't KeyError the detail page.
        "hw_lshw": _db.row_value(row, "hw_lshw"),
        "hw_lshw_at": _db.row_value(row, "hw_lshw_at"),
        "target_disk_serial": row["target_disk_serial"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _client_ip(request: Request) -> str | None:
    """Mirror of ``bty.web._app._client_ip``: read the request's
    client host and feed it through :func:`_events_log.normalize_ip`
    so v4-mapped-v6 addresses collapse to bare v4 before hitting
    the audit log. ``[server] trusted_proxy`` (env override
    ``BTY_SERVER_TRUSTED_PROXY``) opts into reading
    ``X-Forwarded-For`` for deployments behind a reverse proxy.
    Duplicated here rather than imported because ``_app`` already
    imports this module (circular)."""
    if _config.cfg().server.trusted_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return _normalize_ip(first)
    return _normalize_ip(request.client.host if request.client else None)


def _normalise_mac(raw: str) -> str:
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
