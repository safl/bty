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
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import ValidationError

import bty
from bty import images as bty_images
from bty.web import _db, _events_log, _releases, _settings_store, _sysconfig
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
    image_root: Path,
    boot_root: Path,
    backups_root: Path,
    publish_state_changed: Callable[[], None] = lambda: None,
    list_unified_images: Callable[[], list[bty_images.UnifiedImage]] | None = None,
) -> None:
    """Attach the ``/ui`` HTML routes (and exception handler) to ``app``.

    ``service_user`` is the Linux account whose OS password gates
    ``/ui/login``. ``publish_state_changed`` is invoked after any
    UI form mutates a machine record, so SSE subscribers see the
    change immediately. The default no-op makes this module testable
    in isolation; the real app passes the bus-publishing callable.
    """

    def render(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("version", bty.__version__)
        ctx.setdefault("logged_in", bool(request.session.get(SESSION_AUTHED_KEY)))
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
        if not request.session.get(SESSION_AUTHED_KEY):
            raise NotAuthenticated

    # ----- entry / auth ----------------------------------------------------

    @app.get("/ui", include_in_schema=False)
    def ui_root() -> RedirectResponse:
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/login", include_in_schema=False)
    def ui_login_form(request: Request) -> Response:
        # Already authed -> skip the form entirely. Lets ``GET /``
        # (which 303s here) act as a smart entry point: unauthed
        # visitors see the login form, authed visitors land at the
        # dashboard.
        if request.session.get(SESSION_AUTHED_KEY):
            return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        return render("ui/login.html", request)

    @app.post("/ui/login", include_in_schema=False)
    def ui_login_submit(
        request: Request,
        password: Annotated[str, Form()],
    ) -> Response:
        # Lazily import pamela so missing libpam doesn't break module
        # import. pamela is in the ``[web]`` extras alongside fastapi.
        import pamela

        client_ip = _client_ip(request)
        try:
            pamela.authenticate(service_user, password, service="login")
        except pamela.PAMError:
            # Failed login: record so an operator scanning
            # /ui/events sees brute-force attempts. Subject is the
            # OS username we tried to authenticate; source_ip is
            # the request client. Actor is the username (best
            # available) -- we don't know who they really are.
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="auth.login.failed",
                    summary=f"login failed for user {service_user!r}",
                    subject_kind="auth",
                    subject_id=service_user,
                    actor=service_user,
                    source_ip=client_ip,
                )
                conn.commit()
            return render(
                "ui/login.html",
                request,
                error=f"Invalid password for {service_user!r}.",
            )
        # Success path. Record so the audit log shows session
        # boundaries (operator may correlate "this IP did X
        # between Y and Z" with login + logout pairs).
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="auth.login.succeeded",
                summary=f"login succeeded for user {service_user!r}",
                subject_kind="auth",
                subject_id=service_user,
                actor=service_user,
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
                    summary=f"logout for user {service_user!r}",
                    subject_kind="auth",
                    subject_id=service_user,
                    actor=service_user,
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
            # family: image.upload_failed / image.hash_failed /
            # catalog.entry.add_failed / netboot.artifacts.fetch_failed /
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
        # "is this appliance ready to do its job", each row deep-
        # linking to the page that owns it (with a fix action on fail).
        missing_netboot = _releases.missing_netboot_artifacts(boot_root)
        tftp = _sysconfig.tftp_status()
        tftp_controllable = _sysconfig.tftp_controllable()
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

        state_valid = (
            state_migrated
            and state_path.exists()
            and _under(image_root, state_dir)
            and _under(boot_root, state_dir)
        )
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
                "fix_href": "/ui/workers#downloads",
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
                    if tftp_controllable or tftp.state == "active"
                    else (
                        f"dnsmasq.service is {tftp.state} "
                        "(no daemon helper here, the container "
                        "or operator owns the lifecycle)."
                    )
                ),
                "href": "/ui/netboot",
                # The TFTP daemon control now lives on the Netboot
                # list view (below the artifacts table); "fix" is the
                # same as "view".
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
                # required. No fix link: bty-state-migrate is an
                # appliance CLI, not a web action.
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
        return render(
            "ui/machines.html",
            request,
            machines=machines,
            active_filter=active_filter,
            section=section,
            machine_events=machine_events,
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
                    boot_mode        = excluded.boot_mode,
                    sanboot_drive      = excluded.sanboot_drive,
                    target_disk_serial = excluded.target_disk_serial,
                    -- Reset the one-shot alternation bit (mirrors the
                    -- JSON PUT /machines path): an operator changing
                    -- policy here starts a fresh cycle, so a stale
                    -- arming left over from a prior bty-flash-always /
                    -- bty-inventory boot can't make the next /pxe
                    -- wrongly sanboot instead of flashing / inventorying.
                    saw_flasher_boot   = 0,
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
        path silently removed the row with no audit trail."""
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            if cur.rowcount > 0:
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
        return RedirectResponse("/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    def _render_images_page(request: Request, section: str) -> HTMLResponse:
        """Build the shared context for the image-catalog family of
        pages and render ``ui/images.html`` for ``section``.

        ``section`` selects which body the template renders:

        * ``list``      -- the SHA-keyed catalog merge (the ``/ui/images``
          landing); its header carries the inline Fetch-latest
          (release catalog.toml) + Upload-catalog controls.
        * ``downloads`` -- the image-add forms (upload a local file, or
          add by http(s):// / oras:// URL) above the live download-jobs
          table. Top-level page at ``/ui/downloads``.
        * ``hashes``    -- the background sha worker pane. Top-level
          page at ``/ui/hashes``.

        ``?error=<msg>`` lands in the layout's flash slot (the form-
        style ``POST /ui/catalog/entries`` 303s back with that param on
        validation failure, sha-resolve failure, or duplicate-src 409).
        """
        unified = list_unified_images() if list_unified_images is not None else []
        flash = request.query_params.get("error")
        # Catalog manifest path + release repo for the list view's
        # "Catalog manifest" card. ``state_path.parent`` is the resolved
        # state dir create_app was given, so this stays consistent with
        # the Settings page even when ``state_path`` was passed
        # explicitly rather than derived from ``BTY_STATE_DIR``.
        catalog_manifest_path = os.environ.get("BTY_CATALOG_FILE") or str(
            state_path.parent / "catalog.toml"
        )
        # Image-relevant slice of the event log: uploads, hash
        # completions, catalog entry add/delete. Top 15 keeps the
        # page short; full timeline at /ui/events.
        with _db.open_db(state_path) as conn:
            # Effective release repo: operator override -> env ->
            # default. Drives the fetch-catalog button's title.
            release_repo = _settings_store.resolve_release_repo(conn)
            image_events = []
            for kind in ("image", "catalog"):
                image_events.extend(_events_log.list_events(conn, subject_kind=kind, limit=10))
        # Sort by id desc and clip to top 15.
        image_events.sort(key=lambda e: e.id, reverse=True)
        image_events = image_events[:15]
        return render(
            "ui/images.html",
            request,
            unified=unified,
            image_root=str(image_root),
            image_events=image_events,
            manifest_path=catalog_manifest_path,
            release_repo=release_repo,
            section=section,
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
        """The image catalog: the SHA-keyed merge of dir-scan files +
        catalog entries, with the inline Fetch-latest / Upload-catalog
        controls in its header. Downloads and Hashes used to be
        ``?section=`` sub-tabs here; they are now top-level pages
        (``/ui/downloads``, ``/ui/hashes``) reached from the navbar's
        worker indicators right of Settings."""
        return _render_images_page(request, "list")

    @app.get(
        "/ui/downloads",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_downloads(request: Request) -> HTMLResponse:
        """Top-level Downloads page: the image-add forms (upload a local
        file, or add by URL) above the live download-jobs table. Reached
        from the navbar's "Active downloads" indicator (right of
        Settings)."""
        return _render_images_page(request, "downloads")

    @app.get(
        "/ui/hashes",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_hashes(request: Request) -> HTMLResponse:
        """Top-level Hashes page: the background SHA-256 worker pane.
        Reached from the navbar's "Active hashes" indicator (right of
        Settings)."""
        return _render_images_page(request, "hashes")

    @app.get(
        "/ui/workers",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_workers(request: Request) -> HTMLResponse:
        """The merged background-workers page: Downloads (catalog +
        release artifacts), Hashing, Backup. Active jobs only -- the
        events log is the history. Triggers stay on their home pages
        (catalog downloads on /ui/images, release artifacts on
        /ui/netboot); only Backup has a trigger here.

        The legacy ``/ui/downloads`` / ``/ui/hashes`` / ``/ui/fetches``
        pages continue to render for now -- the navbar's three worker
        icons all point at this merged page going forward, but the
        legacy URLs still respond so direct links and operator muscle
        memory keep working.
        """
        with _db.open_db(state_path) as conn:
            backup_enabled = _settings_store.resolve_backup_enabled(conn)
            backup_cadence = _settings_store.resolve_backup_cadence(conn)
            backup_last_run_at = _settings_store.get_backup_last_run_at(conn)
        return render(
            "ui/workers.html",
            request,
            backups_root=str(backups_root),
            backup_enabled=backup_enabled,
            backup_cadence=backup_cadence,
            backup_last_run_at=backup_last_run_at,
        )

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
        sha256, download) plus the TFTP daemon control.

        The artifacts header still carries a "Fetch latest artifacts"
        button, but it enqueues the fetch and hands off to the Release
        fetches page (``/ui/fetches``, under the navbar worker icon) to
        watch progress. The router-side DHCP / PXE cheatsheet moved to
        Settings. Operator-side artifact uploads stay scripted via the
        auth-gated ``PUT /boot/{name}`` route, not the browser.
        """
        with _db.open_db(state_path) as conn:
            release_repo = _settings_store.resolve_release_repo(conn)
            release_tag = _settings_store.resolve_release_tag(conn)
            # Recent netboot activity for the page's "Activity" table.
            boot_events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
        return render(
            "ui/netboot.html",
            request,
            boot_root=str(boot_root),
            artifacts=_releases.inspect_boot_dir(boot_root),
            artifact_shas=_releases.boot_artifact_shas(boot_root),
            release_repo=release_repo,
            release_tag=release_tag,
            boot_events=boot_events,
            flash=flash,
            flash_kind=flash_kind,
            tftp=_sysconfig.tftp_status(),
            tftp_controllable=_sysconfig.tftp_controllable(),
        )

    @app.get(
        "/ui/netboot",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_netboot(request: Request) -> HTMLResponse:
        return _render_netboot_page(request)

    def _render_fetches_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        """The Release fetches page (under the navbar's release-fetch
        worker indicator, right of Settings): the "Fetch latest
        artifacts" trigger and the live release-fetch jobs table (active
        + recent, event-backfilled on restart), plus recent netboot
        activity. The artifact inventory + TFTP daemon are on the Netboot
        page."""
        with _db.open_db(state_path) as conn:
            boot_events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
            release_repo = _settings_store.resolve_release_repo(conn)
            release_tag = _settings_store.resolve_release_tag(conn)
        return render(
            "ui/fetches.html",
            request,
            release_repo=release_repo,
            release_tag=release_tag,
            boot_events=boot_events,
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.get(
        "/ui/fetches",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_fetches(request: Request) -> HTMLResponse:
        return _render_fetches_page(request)

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

    def _config_row(label: str, value: object, env: str | None, default: str) -> dict[str, Any]:
        """One read-only config row. ``source`` is "env" when an env var
        is set, else "default" (the built-in / derived value), so the
        operator can see why a magic value is what it is."""
        source = "env" if (env and os.environ.get(env)) else "default"
        return {
            "label": label,
            "value": str(value),
            "env": env,
            "default": default,
            "source": source,
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
        state_dir = state_path.parent
        catalog_file = os.environ.get("BTY_CATALOG_FILE") or str(state_dir / "catalog.toml")
        cache_dir = os.environ.get("BTY_CATALOG_CACHE_DIR") or str(state_dir / "cache")
        session_secret = os.environ.get("BTY_SESSION_SECRET")
        with _db.open_db(state_path) as conn:
            release_repo = _settings_store.resolve_release_repo(conn)
            catalog_url = _settings_store.resolve_catalog_url(conn)
            release_tag = _settings_store.resolve_release_tag(conn)
            repo_override = _settings_store.get(conn, _settings_store.KEY_RELEASE_REPO)
            catalog_override = _settings_store.get(conn, _settings_store.KEY_CATALOG_URL)
            tag_override = _settings_store.get(conn, _settings_store.KEY_RELEASE_TAG)
        upstream = {
            "release_repo": release_repo,
            "release_repo_override": repo_override,
            "release_repo_default": _settings_store.default_release_repo(),
            "catalog_url": catalog_url,
            "catalog_url_override": catalog_override,
            "catalog_url_default": _settings_store.default_catalog_url(release_repo),
            "release_tag": release_tag,
            "release_tag_override": tag_override,
            "release_tag_default": _settings_store.DEFAULT_RELEASE_TAG,
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
                    _config_row("State directory", state_dir, "BTY_STATE_DIR", "/var/lib/bty"),
                    _config_row("Database", state_path, None, "<state dir>/state.db"),
                    _config_row("Image root", image_root, "BTY_IMAGE_ROOT", "/var/lib/bty/images"),
                    _config_row("Netboot directory", boot_root, "BTY_BOOT_DIR", "<state dir>/boot"),
                    _config_row(
                        "Catalog manifest",
                        catalog_file,
                        "BTY_CATALOG_FILE",
                        "<state dir>/catalog.toml",
                    ),
                    _config_row(
                        "Image cache", cache_dir, "BTY_CATALOG_CACHE_DIR", "<state dir>/cache"
                    ),
                    _config_row(
                        "Session secret",
                        session_secret or str(state_dir / "session-secret"),
                        "BTY_SESSION_SECRET",
                        "<state dir>/session-secret",
                    ),
                ],
            },
            {
                "title": "Network",
                "icon": "hdd-network",
                "rows": [
                    _config_row(
                        "Bind host",
                        os.environ.get("BTY_WEB_HOST", "0.0.0.0"),
                        "BTY_WEB_HOST",
                        "0.0.0.0",
                    ),
                    _config_row(
                        "Bind port", os.environ.get("BTY_WEB_PORT", "8080"), "BTY_WEB_PORT", "8080"
                    ),
                    _config_row(
                        "Trust X-Forwarded-For",
                        "yes" if os.environ.get("BTY_TRUSTED_PROXY") else "no",
                        "BTY_TRUSTED_PROXY",
                        "no",
                    ),
                    _config_row("TFTP systemd unit", _sysconfig.TFTP_UNIT, None, "dnsmasq.service"),
                ],
            },
            {
                "title": "Background workers",
                "icon": "cpu",
                "rows": [
                    _config_row(
                        "Catalog downloads (parallel)",
                        os.environ.get("BTY_CATALOG_MAX_PARALLEL", "2"),
                        "BTY_CATALOG_MAX_PARALLEL",
                        "2",
                    ),
                    _config_row(
                        "Image hashing (parallel)",
                        os.environ.get("BTY_HASH_MAX_PARALLEL", "1"),
                        "BTY_HASH_MAX_PARALLEL",
                        "1",
                    ),
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
        # the Netboot page): the appliance's interfaces + the primary
        # v4 address the operator points their router's Next-Server at.
        interfaces = _sysconfig.list_interfaces()
        primary = next((i for i in interfaces if i.ipv4), interfaces[0] if interfaces else None)
        return render(
            "ui/settings.html",
            request,
            flash=flash,
            flash_kind=flash_kind,
            upstream=upstream,
            config_groups=config_groups,
            interfaces=interfaces,
            primary=primary,
            boot_root=str(boot_root),
            missing_netboot_artifacts=_releases.missing_netboot_artifacts(boot_root),
        )

    @app.get(
        "/ui/settings",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings(request: Request, saved: str | None = None) -> HTMLResponse:
        flash = "Upstream sources saved." if saved else None
        return _render_settings_page(request, flash=flash, flash_kind="success" if saved else None)

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
        "/ui/settings/upstream",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_upstream(
        request: Request,
        release_repo: Annotated[str, Form()] = "",
        catalog_url: Annotated[str, Form()] = "",
        release_tag: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Save (or clear) the three editable upstream overrides
        (release repo, catalog URL, release tag). An empty field clears
        that override, reverting to env / default. All three take effect
        on the next fetch without a restart, since the fetch sites
        resolve from this store at request time."""
        rr = release_repo.strip()
        cu = catalog_url.strip()
        rt = release_tag.strip()
        with _db.open_db(state_path) as conn:
            if rr:
                _settings_store.set_value(conn, _settings_store.KEY_RELEASE_REPO, rr)
            else:
                _settings_store.clear(conn, _settings_store.KEY_RELEASE_REPO)
            if cu:
                _settings_store.set_value(conn, _settings_store.KEY_CATALOG_URL, cu)
            else:
                _settings_store.clear(conn, _settings_store.KEY_CATALOG_URL)
            if rt:
                _settings_store.set_value(conn, _settings_store.KEY_RELEASE_TAG, rt)
            else:
                _settings_store.clear(conn, _settings_store.KEY_RELEASE_TAG)
            _events_log.record(
                conn,
                kind="settings.upstream.updated",
                summary=(
                    f"upstream sources set: repo={rr or '(default)'}, "
                    f"catalog_url={cu or '(default)'}, tag={rt or '(default)'}"
                ),
                subject_kind="settings",
                subject_id="upstream",
                actor="operator",
                source_ip=_client_ip(request),
            )
            conn.commit()
        return RedirectResponse("/ui/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)

    @app.post(
        "/ui/settings/tftp-control",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_tftp_control(
        request: Request,
        action: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        # Start / Stop / Restart dnsmasq.service (the local TFTP
        # daemon). Operator triage: "TFTP isn't responding -> restart
        # it"; "I want to take PXE offline briefly -> stop it".
        client_ip = _client_ip(request)
        try:
            _sysconfig.control_tftp(action)
        except _sysconfig.SysConfigError as exc:
            with _db.open_db(state_path) as conn:
                _events_log.record(
                    conn,
                    kind="netboot.tftp.control_failed",
                    summary=f"TFTP {action!r} failed: {exc}",
                    subject_kind="netboot",
                    subject_id="tftp",
                    actor="operator",
                    source_ip=client_ip,
                    details={"action": action, "error": str(exc)},
                )
                conn.commit()
            return _render_netboot_page(
                request,
                flash=f"{action} of TFTP daemon failed: {exc}",
                flash_kind="danger",
            )
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="netboot.tftp.controlled",
                summary=f"TFTP daemon {action}",
                subject_kind="netboot",
                subject_id="tftp",
                actor="operator",
                source_ip=client_ip,
                details={"action": action},
            )
            conn.commit()
        return _render_netboot_page(
            request,
            flash=f"{action.capitalize()}ed TFTP daemon.",
            flash_kind="success",
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
                    kind="netboot.artifacts.fetch_failed",
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


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Decode a sqlite3.Row of ``machines`` into a plain dict.

    ``known_disks`` is stored as JSON text in the column;
    decode it here so the Jinja template can iterate it
    directly. Bad JSON degrades to ``None`` so a stale row
    can't 500 the detail page.

    The new columns are part of the current schema (see
    ``_db._REQUIRED_COLUMNS``), so a missing-column
    StaleSchemaError fires at startup rather than letting a
    KeyError surface here. We can index directly.
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
    the audit log. ``BTY_TRUSTED_PROXY`` opts into reading
    ``X-Forwarded-For`` for deployments behind a reverse proxy.
    Duplicated here rather than imported because ``_app`` already
    imports this module (circular)."""
    if os.environ.get("BTY_TRUSTED_PROXY"):
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
