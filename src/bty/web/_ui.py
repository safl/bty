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

import bty
from bty import images as bty_images
from bty.web import _db, _events_log, _releases, _sysconfig
from bty.web._auth import SESSION_AUTHED_KEY
from bty.web._events_log import KNOWN_ACTORS, KNOWN_EVENT_KINDS, KNOWN_SUBJECT_KINDS
from bty.web._events_log import normalize_ip as _normalize_ip
from bty.web._models import (
    BOOT_POLICIES,
    DEFAULT_BOOT_POLICY,
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
        with _db.open_db(state_path) as conn:
            machine_count = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
            discovered_count = conn.execute(
                "SELECT COUNT(*) FROM machines WHERE bty_image_ref IS NULL"
            ).fetchone()[0]
            # Recent activity slice for the dashboard's "what just
            # happened?" widget. Reuses ``_events_card.html`` so the
            # styling matches the per-machine and per-image cards;
            # the dashboard renders at request time only (no SSE
            # update) so a reload is the refresh gesture.
            recent_events = _events_log.list_events(conn, limit=10)
        image_count = len(bty_images.list_images(image_root))
        return render(
            "ui/dashboard.html",
            request,
            machine_count=machine_count,
            discovered_count=discovered_count,
            image_count=image_count,
            recent_events=recent_events,
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
        if section not in ("list", "add"):
            section = "list"
        with _db.open_db(state_path) as conn:
            rows = conn.execute(sql).fetchall()
        machines = [_row_to_dict(r) for r in rows]
        return render(
            "ui/machines.html",
            request,
            machines=machines,
            active_filter=active_filter,
            section=section,
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
                "SELECT bty_image_ref, name, format, src, disk_image_sha "
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
            boot_policies=list(BOOT_POLICIES),
            machine_events=machine_events,
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
        bty_image_ref: Annotated[str, Form()] = "",
        hostname: Annotated[str, Form()] = "",
        boot_policy: Annotated[str, Form()] = DEFAULT_BOOT_POLICY,
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
                boot_policy=boot_policy,
                target_disk_serial=target_disk_serial or None,
            )
        except ValueError as exc:
            return RedirectResponse(
                f"/ui/machines/{normalised}?error="
                + urllib.parse.quote(f"validation failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        # ``boot_policy`` is pattern-checked by Pydantic above; the
        # explicit set membership check is therefore redundant but
        # kept so the operator gets an enum-style error message.
        if validated.boot_policy not in BOOT_POLICIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid boot_policy: {validated.boot_policy!r}",
            )
        # Safety gate: boot_policy=flash / flash-once require a
        # picked target_disk_serial. Without it /pxe/<mac>/plan
        # would fall back to mode=interactive anyway (the auto-
        # flash branch needs a serial); catching it here gives
        # the operator a clear flash-banner error in /ui/machines
        # instead of the operator being surprised by a wizard
        # prompt at flash time.
        if validated.boot_policy in ("flash", "flash-once") and not validated.target_disk_serial:
            return RedirectResponse(
                f"/ui/machines/{normalised}?error="
                + urllib.parse.quote(
                    "boot_policy=flash requires a target disk to be picked; "
                    "power-cycle the machine in 'tui' mode first so it can "
                    "report its disk inventory, then pick one here",
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
                    (mac, bty_image_ref, hostname, boot_policy,
                     target_disk_serial, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    bty_image_ref      = excluded.bty_image_ref,
                    hostname           = excluded.hostname,
                    boot_policy        = excluded.boot_policy,
                    target_disk_serial = excluded.target_disk_serial,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    validated.bty_image_ref,
                    validated.hostname,
                    validated.boot_policy,
                    validated.target_disk_serial,
                    created_at,
                    now,
                ),
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

    @app.get(
        "/ui/images",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_images(request: Request) -> HTMLResponse:
        """Unified images page with sub-nav. Default landing
        (``?section=list``, or no query) shows the SHA-keyed catalog
        merge + the live downloads / hashes panes. Operators land
        on the OBSERVABLE state of the catalog; "add" paths are one
        click away via the sub-nav:

        * ``?section=fetch``: one-button fetch of the bty release
          catalog.toml (the common-case default for populating).
        * ``?section=add-url``: form to add by http(s):// or
          oras:// URL.
        * ``?section=upload-catalog``: file picker for a
          ``catalog.toml`` upload.
        * ``?section=upload-image``: file picker for streaming an
          image into the image root.

        Unrecognised ``section`` values fall back to ``list`` so
        bookmark drift can't 500 the page.

        ``?error=<msg>`` lands in the layout's flash slot. The form-
        style ``POST /ui/catalog/entries`` 303s back here with that
        param on validation failure, sha-resolve failure, or
        duplicate-src 409.
        """
        unified = list_unified_images() if list_unified_images is not None else []
        flash = request.query_params.get("error")
        section = request.query_params.get("section") or "list"
        if section not in ("list", "fetch", "add-url", "upload-catalog", "upload-image"):
            section = "list"
        # Catalog manifest path + release repo for the "Catalog
        # manifest" card. The path mirrors the resolution in
        # _app.create_app (env override -> default under state dir).
        catalog_manifest_path = os.environ.get("BTY_CATALOG_FILE")
        if not catalog_manifest_path:
            state_dir = os.environ.get("BTY_STATE_DIR", "/var/lib/bty")
            catalog_manifest_path = str(Path(state_dir) / "catalog.toml")
        # ``or DEFAULT_REPO`` rather than the dict default so an
        # empty-string env value (``BTY_BOOT_RELEASE_REPO=``) falls
        # back instead of breaking the page's release link. Matches
        # the pattern in _releases.fetch_release + ui_boot.
        release_repo = os.environ.get("BTY_BOOT_RELEASE_REPO") or _releases.DEFAULT_REPO
        # Image-relevant slice of the event log: uploads, hash
        # completions, catalog entry add/delete. Top 15 keeps the
        # page short; full timeline at /ui/events.
        with _db.open_db(state_path) as conn:
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

    @app.post(
        "/ui/catalog/entries",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_catalog_entry_add(
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
            now = datetime.now(UTC).isoformat()
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
        now = datetime.now(UTC).isoformat()
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
                conn.commit()
            except sqlite3.IntegrityError:
                return RedirectResponse(
                    "/ui/images?error=already+exists",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    # ----- boot artifacts ------------------------------------------------

    def _render_boot_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        """Sub-nav-aware boot-artifacts page. ``?section=list``
        (the default landing) shows the four artefacts +
        present/missing + recent fetch table. ``?section=fetch``
        is the one-button "fetch from latest release" form. The
        upload route is intentionally NOT in the sub-nav -- the
        appliance is designed around release fetches, and
        operator-side artefact uploads are scripted via the
        auth-gated ``PUT /boot/{name}`` route, not the browser.

        Unrecognised ``section`` values fall back to ``list``.
        """
        section = request.query_params.get("section") or "list"
        if section not in ("list", "fetch"):
            section = "list"
        # Recent activity for boot artefacts: release fetches /
        # fetch failures.
        with _db.open_db(state_path) as conn:
            boot_events = _events_log.list_events(conn, subject_kind="boot", limit=10)
        # Network + TFTP context the operator needs to wire up the
        # netboot side. Lives here (rather than under /ui/settings)
        # so the "what do I configure on my router" cheatsheet sits
        # next to the netboot artefacts it depends on.
        interfaces = _sysconfig.list_interfaces()
        primary = next((i for i in interfaces if i.ipv4), interfaces[0] if interfaces else None)
        return render(
            "ui/boot.html",
            request,
            boot_root=str(boot_root),
            artifacts=_releases.inspect_boot_dir(boot_root),
            release_repo=os.environ.get("BTY_BOOT_RELEASE_REPO") or _releases.DEFAULT_REPO,
            boot_events=boot_events,
            section=section,
            flash=flash,
            flash_kind=flash_kind,
            interfaces=interfaces,
            primary=primary,
            tftp=_sysconfig.tftp_status(),
            tftp_controllable=_sysconfig.tftp_controllable(),
            missing_netboot_artifacts=_releases.missing_netboot_artifacts(boot_root),
        )

    @app.get(
        "/ui/boot",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_boot(request: Request) -> HTMLResponse:
        return _render_boot_page(request)

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

    # ----- settings -------------------------------------------------------

    def _render_settings_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        # /ui/settings is now an informational page: operator config
        # for the PXE flow happens on the LAN router (DHCP option 60
        # / 66 / 67 tagging), not in bty-web. We surface this
        # appliance's IP + interfaces so the operator has the values
        # they need to paste into UniFi / pfSense / dnsmasq. The
        # appliance side that IS controllable from here is the local
        # dnsmasq (TFTP) -- Start / Stop / Restart in the panel
        # below.
        interfaces = _sysconfig.list_interfaces()
        missing_netboot = _releases.missing_netboot_artifacts(boot_root)
        tftp = _sysconfig.tftp_status()
        tftp_controllable = _sysconfig.tftp_controllable()
        return render(
            "ui/settings.html",
            request,
            interfaces=interfaces,
            tftp=tftp,
            tftp_controllable=tftp_controllable,
            missing_netboot_artifacts=missing_netboot,
            boot_root=str(boot_root),
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.get(
        "/ui/settings",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings(request: Request) -> HTMLResponse:
        return _render_settings_page(request)

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
                    kind="settings.tftp.control_failed",
                    summary=f"TFTP {action!r} failed: {exc}",
                    subject_kind="settings",
                    subject_id="tftp",
                    actor="operator",
                    source_ip=client_ip,
                    details={"action": action, "error": str(exc)},
                )
                conn.commit()
            return _render_boot_page(
                request,
                flash=f"{action} of TFTP daemon failed: {exc}",
                flash_kind="danger",
            )
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="settings.tftp.controlled",
                summary=f"TFTP daemon {action}",
                subject_kind="settings",
                subject_id="tftp",
                actor="operator",
                source_ip=client_ip,
                details={"action": action},
            )
            conn.commit()
        return _render_boot_page(
            request,
            flash=f"{action.capitalize()}ed TFTP daemon.",
            flash_kind="success",
        )

    @app.post(
        "/ui/boot/fetch-release",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_boot_fetch(
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
                    kind="boot.release.fetch_failed",
                    summary=f"boot release {resolved_tag!r} fetch failed: {exc}",
                    subject_kind="boot",
                    subject_id=resolved_tag,
                    actor="operator",
                    source_ip=client_ip,
                    details={"tag": resolved_tag, "error": str(exc)},
                )
                conn.commit()
            return _render_boot_page(
                request,
                flash=f"Fetch failed: {exc}",
                flash_kind="danger",
            )
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="boot.release.fetched",
                summary=f"boot release {resolved_tag!r} fetched from {result.base_url}",
                subject_kind="boot",
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
        return _render_boot_page(
            request,
            flash=(
                f"Fetched {len(result.artifacts)} artifacts ({result.total_bytes:,} bytes) "
                f"from {result.base_url}"
            ),
            flash_kind="success",
        )


# ---------- helpers ---------------------------------------------------------


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
        "boot_policy": row["boot_policy"],
        "last_flashed_at": row["last_flashed_at"],
        "known_disks": parsed_disks,
        "known_disks_at": row["known_disks_at"],
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
