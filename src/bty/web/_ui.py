"""Browser UI routes for bty-web (Jinja + Bootstrap, milestone 12 phase 1).

Routes live under ``/ui``. They render server-side HTML and use cookie
auth (the ``bty-token`` cookie set by ``POST /ui/login``); the API
surface at ``/`` is unchanged. ``register_ui_routes(app, ...)``
attaches all the UI handlers to an existing FastAPI app.

Live updates via SSE arrive in milestone 12 phase 2; this iteration is
plain server-rendered pages with HTMX-friendly form posts.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Cookie,
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
from bty.web import _db, _releases, _sysconfig
from bty.web._auth import SESSION_COOKIE, token_matches
from bty.web._models import BOOT_POLICIES, PROVISIONING_MODES

# How long the browser cookie lives.
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60


class NotAuthenticated(Exception):
    """Raised by UI dependencies when the request lacks a valid session cookie.

    The exception handler redirects to ``/ui/login``; UI requests get
    a redirect, API requests would still hit the regular 401 dependency.
    """


_UICookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def register_ui_routes(
    app: FastAPI,
    *,
    jinja: Environment,
    state_path: Path,
    expected_token: str,
    image_root: Path,
    boot_root: Path,
    publish_machines_changed: Callable[[], None] = lambda: None,
) -> None:
    """Attach the ``/ui`` HTML routes (and exception handler) to ``app``.

    ``publish_machines_changed`` is invoked after any UI form mutates a
    machine record, so SSE subscribers see the change immediately. The
    default no-op makes this module testable in isolation; the real app
    passes the bus-publishing callable.
    """

    def render(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("version", bty.__version__)
        ctx.setdefault("logged_in", _request_is_authed(request, expected_token))
        ctx.setdefault("flash", None)
        ctx.setdefault("flash_kind", None)
        template = jinja.get_template(name)
        return HTMLResponse(template.render(**ctx))

    @app.exception_handler(NotAuthenticated)
    async def _not_authed(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        del request, exc
        return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    def require_ui_auth(cookie_token: _UICookie = None) -> None:
        if not cookie_token or not token_matches(expected_token, cookie_token):
            raise NotAuthenticated

    # ----- entry / auth ----------------------------------------------------

    @app.get("/ui", include_in_schema=False)
    def ui_root() -> RedirectResponse:
        return RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/login", response_class=HTMLResponse, include_in_schema=False)
    def ui_login_form(request: Request) -> HTMLResponse:
        return render("ui/login.html", request)

    @app.post("/ui/login", include_in_schema=False)
    def ui_login_submit(
        request: Request,
        token: Annotated[str, Form()],
    ) -> Response:
        if not token_matches(expected_token, token):
            return render(
                "ui/login.html",
                request,
                error="Invalid token; check BTY_WEB_TOKEN on the server.",
            )
        response = RedirectResponse("/ui/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/ui/logout", include_in_schema=False)
    def ui_logout() -> Response:
        response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE)
        return response

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
                "SELECT COUNT(*) FROM machines WHERE image IS NULL"
            ).fetchone()[0]
        image_count = len(bty_images.list_images(image_root))
        return render(
            "ui/dashboard.html",
            request,
            machine_count=machine_count,
            discovered_count=discovered_count,
            image_count=image_count,
        )

    @app.get(
        "/ui/machines",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machines(request: Request) -> HTMLResponse:
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        machines = [_row_to_dict(r) for r in rows]
        return render("ui/machines.html", request, machines=machines)

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
        return render(
            "ui/machine_detail.html",
            request,
            m=_row_to_dict(row),
            images=bty_images.list_images(image_root),
            provisioning_modes=list(PROVISIONING_MODES),
            boot_policies=list(BOOT_POLICIES),
        )

    @app.post(
        "/ui/machines/{mac}",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machine_upsert(
        mac: str,
        image: Annotated[str, Form()] = "",
        provisioning_mode: Annotated[str, Form()] = "none",
        hostname: Annotated[str, Form()] = "",
        cijoe_workflow_ref: Annotated[str, Form()] = "",
        boot_policy: Annotated[str, Form()] = "local",
    ) -> RedirectResponse:
        if provisioning_mode not in PROVISIONING_MODES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid provisioning_mode: {provisioning_mode!r}",
            )
        if boot_policy not in BOOT_POLICIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid boot_policy: {boot_policy!r}",
            )
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
                    image or None,
                    provisioning_mode,
                    hostname or None,
                    cijoe_workflow_ref or None,
                    boot_policy,
                    created_at,
                    now,
                ),
            )
            conn.commit()
        publish_machines_changed()
        return RedirectResponse("/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.post(
        "/ui/machines/{mac}/delete",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_machine_delete(mac: str) -> RedirectResponse:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            conn.commit()
        publish_machines_changed()
        return RedirectResponse("/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.get(
        "/ui/images",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_images(request: Request) -> HTMLResponse:
        listed = bty_images.list_images(image_root)
        return render(
            "ui/images.html",
            request,
            images=listed,
            image_root=str(image_root),
        )

    # ----- boot artifacts (Phase D-3b.2) ----------------------------------

    def _render_boot_page(
        request: Request,
        *,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        return render(
            "ui/boot.html",
            request,
            boot_root=str(boot_root),
            artifacts=_releases.inspect_boot_dir(boot_root),
            release_repo=os.environ.get("BTY_BOOT_RELEASE_REPO") or _releases.DEFAULT_REPO,
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.get(
        "/ui/boot",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_boot(request: Request) -> HTMLResponse:
        return _render_boot_page(request)

    # ----- settings (Phase E) ---------------------------------------------

    def _render_settings_page(
        request: Request,
        *,
        new_token: str | None = None,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> HTMLResponse:
        return render(
            "ui/settings.html",
            request,
            interfaces=_sysconfig.list_interfaces(),
            pxe=_sysconfig.pxe_active(),
            new_token=new_token,
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
        "/ui/settings/rotate-token",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_rotate_token(request: Request) -> HTMLResponse:
        try:
            new_token = _sysconfig.rotate_token()
        except _sysconfig.SysConfigError as exc:
            return _render_settings_page(
                request, flash=f"Token rotation failed: {exc}", flash_kind="danger"
            )
        return _render_settings_page(
            request,
            new_token=new_token,
            flash=(
                "New token written to /etc/default/bty-web. The change takes "
                "effect after the next bty-web restart - copy the value below "
                "first, then restart the service (or reboot)."
            ),
            flash_kind="warning",
        )

    @app.post(
        "/ui/settings/pxe-activate",
        include_in_schema=False,
        dependencies=[Depends(require_ui_auth)],
    )
    def ui_settings_pxe_activate(
        request: Request,
        interface: Annotated[str, Form()] = "",
        subnet: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        try:
            _sysconfig.activate_pxe(interface, subnet)
        except _sysconfig.SysConfigError as exc:
            return _render_settings_page(
                request, flash=f"PXE activation failed: {exc}", flash_kind="danger"
            )
        return _render_settings_page(
            request,
            flash=f"PXE activated on {interface!r} for {subnet!r}.",
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
        try:
            result = _releases.fetch_release(boot_root, tag=tag or "latest")
        except _releases.FetchError as exc:
            return _render_boot_page(
                request,
                flash=f"Fetch failed: {exc}",
                flash_kind="danger",
            )
        return _render_boot_page(
            request,
            flash=(
                f"Fetched {len(result.artifacts)} artifacts ({result.total_bytes:,} bytes) "
                f"from {result.base_url}"
            ),
            flash_kind="success",
        )


# ---------- helpers ---------------------------------------------------------


def _request_is_authed(request: Request, expected_token: str) -> bool:
    """Used by the layout template to show/hide the nav and logout button."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        return False
    return token_matches(expected_token, cookie)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "mac": row["mac"],
        "image": row["image"],
        "provisioning_mode": row["provisioning_mode"],
        "hostname": row["hostname"],
        "cijoe_workflow_ref": row["cijoe_workflow_ref"],
        "discovered_at": row["discovered_at"],
        "last_seen_at": row["last_seen_at"],
        "last_seen_ip": row["last_seen_ip"],
        "boot_policy": row["boot_policy"],
        "last_flashed_at": row["last_flashed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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
