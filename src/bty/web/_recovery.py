"""Recovery-mode bty-web: ultra-explicit UI when state.db doesn't
match the running release.

When :func:`bty.web._db.check_db` returns a :class:`DbState.MISMATCH`
or :class:`DbState.PRE_VERSIONING` result, ``create_app`` builds this
minimal FastAPI app on the same port instead of the full app. The
operator hits the appliance URL in a browser, lands on a styled
checklist (``/ui/recovery``), and works through:

1. ``State detected`` (auto, informational).
2. ``Choose recovery strategy`` (operator picks).
3. ``Wipe / import / etc.`` (POST actions; bty-web executes the
   chosen step then ``os._exit(0)`` so systemd's
   ``Restart=on-failure`` brings up a fresh process).
4. ``Verify`` (next request lands on the NORMAL app -- recovery
   mode exits itself).

Pre-1.0 stance: this is operator-facing recovery, not a migration
framework. The actions are all "wipe + maybe re-seed"; the data
that travels across the wipe is what :func:`bty.web._portability.
export_bundle` carries (mac + lshw + known_disks + image files),
nothing else.

The recovery app deliberately does NOT touch the failing state.db
on its own. It reads the catalog row counts for the wizard via
:class:`bty.web._db.check_db` (read-only) so the operator sees a
faithful summary; the wipe happens only on explicit operator POST.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

import bty

if TYPE_CHECKING:
    from fastapi import FastAPI

from . import _db, _portability


class _WipeAndImportBody(BaseModel):
    """``POST /ui/recovery/wipe-and-import`` body shape.

    Defined at module level so FastAPI's type-hint resolution sees
    a stable class to bind to the body. (A locally-scoped class
    inside ``build_recovery_app`` gets treated as a query-param
    annotation by FastAPI's signature analyser, producing a
    confusing ``Field required for query parameter 'body'`` error.)
    """

    backup_id: str


# Where vendored assets + templates live -- same paths as the full
# app, so the recovery UI inherits the Bootstrap + Bootstrap-icons
# styling without re-vending anything.
_STATIC_DIR = Path(__file__).parent / "_static"
_TEMPLATES_DIR = Path(__file__).parent / "_templates"


def _read_backup_listing(backups_root: Path) -> list[dict[str, Any]]:
    """Enumerate ``<backups_root>/<id>`` directories that look like
    bty-web export bundles, newest first.

    Each entry carries: ``backup_id``, ``exported_at``,
    ``exported_by_bty_version``, ``machines``, ``files``,
    ``bty_export_version``. Bundles whose manifest is unparseable
    surface with ``unreadable=True`` so the wizard can hint at why
    they're not selectable.

    The recovery flow uses this to populate the "wipe + import from
    backup" picker. Reads only -- the failing state.db is untouched.
    """
    import json

    if not backups_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(backups_root.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        info: dict[str, Any] = {
            "backup_id": entry.name,
            "path": str(entry),
            "exported_at": None,
            "exported_by_bty_version": None,
            "machines": 0,
            "files": 0,
            "bty_export_version": None,
            "unreadable": False,
        }
        if manifest_path.is_file():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    info["exported_at"] = m.get("exported_at")
                    info["exported_by_bty_version"] = m.get("exported_by_bty_version") or m.get(
                        "bty_version"
                    )
                    info["bty_export_version"] = m.get("bty_export_version")
                    ms = m.get("machines")
                    if isinstance(ms, list):
                        info["machines"] = len(ms)
            except (OSError, ValueError):
                info["unreadable"] = True
        else:
            info["unreadable"] = True
        files_dir = entry / "files"
        if files_dir.is_dir():
            try:
                info["files"] = sum(1 for f in files_dir.iterdir() if f.is_file())
            except OSError:
                # Unreadable files/ subdir -- the operator should see
                # the bundle disabled in the picker rather than silently
                # showing "0 files." Map it to the same UI state as
                # unreadable=True so the template renders a warning.
                info["unreadable"] = True
        out.append(info)
    return out


def _read_at_risk_counts(state_path: Path) -> dict[str, int]:
    """Best-effort row counts for the at-risk summary on the wizard.

    Reads ``machines``, ``catalog_entries``, ``events`` via a
    read-only connection so the operator sees "what's about to be
    wiped." Any error (locked DB, missing table) folds into 0 for
    that bucket -- the wizard renders a "partial" tag rather than
    crashing on a sick state.db.
    """
    counts = {"machines": 0, "catalog_entries": 0, "events": 0}
    if not state_path.exists():
        return counts
    uri = f"file:{state_path}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            for table in counts:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    if row is not None:
                        counts[table] = int(row[0])
                except sqlite3.Error:
                    pass
    except sqlite3.Error:
        pass
    return counts


def _wipe_state_db(state_path: Path) -> None:
    """Unlink ``state.db`` and its sqlite sidecars (-journal / -wal /
    -shm) if present. Idempotent.

    After this returns the directory is in the same shape as a
    fresh appliance start: next ``init_db`` creates + stamps a clean
    DB with the running version.
    """
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = state_path.parent / (state_path.name + suffix)
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()


def build_recovery_app(
    *,
    state_path: Path,
    image_root: Path,
    backups_root: Path,
    secret_key: str,
    service_user: str,
    db_check: _db.DbCheckResult,
) -> FastAPI:
    """Return a minimal FastAPI app for recovery mode.

    Routes (every non-recovery route returns 503):

    * ``GET  /``                       -> redirect to ``/ui/recovery``
    * ``GET  /ui/recovery``            -> wizard page
    * ``GET  /ui/recovery/status``     -> JSON snapshot of checklist
                                         state (the page polls this
                                         to auto-advance ticks)
    * ``POST /ui/recovery/wipe``       -> wipe state.db, exit(0)
    * ``POST /ui/recovery/wipe-and-import``
        body=``{"backup_id": "..."}``  -> wipe + import bundle from
                                         ``backups_root/<backup_id>``,
                                         exit(0)
    * ``GET  /healthz``                -> 503 with reason
    * ``GET  /static/*``               -> vendored assets

    The wizard's POST actions execute the chosen recovery, then
    call ``os._exit(0)`` so systemd's ``Restart=on-failure`` brings
    up a fresh bty-web process. The operator's browser reload-polls
    ``/ui/recovery/status`` -- once the next process answers (it
    will be the NORMAL app, not this recovery one), the page
    redirects to ``/ui/dashboard``.
    """
    # Local imports keep this module loadable without [web] extras
    # for stdlib-only callers (e.g. unit tests that introspect the
    # function signature).
    from fastapi import FastAPI, HTTPException, status
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from starlette.middleware.sessions import SessionMiddleware

    # ``service_user`` is plumbed through for symmetry with the
    # normal app's PAM /ui/login flow; v0.32.0 recovery is open on
    # the lab segment (same trust model as the rest of /ui), so
    # we don't gate the POST actions on auth for now -- the operator
    # is already past the appliance's network perimeter. v0.33.0
    # may layer PAM on top once we're sure the wizard is operator-
    # safe under unauthenticated browsers on the same LAN.
    del service_user

    jinja = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="bty-web (recovery mode)",
        version=bty.__version__,
        lifespan=_lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="bty-session",
        same_site="strict",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    def _render_wizard() -> str:
        """Render the checklist with a fresh probe of the DB +
        backups list. Called on every page load + every status
        poll so the operator sees live state."""
        fresh = _db.check_db(state_path)
        backups = _read_backup_listing(backups_root)
        at_risk = _read_at_risk_counts(state_path)
        # ``recovered`` is True when this page renders against a DB
        # that no longer needs recovery -- e.g. the operator's
        # browser polled /ui/recovery between the wipe and the
        # systemd restart, so the OLD process answered with the
        # checklist while the NEW (normal-mode) process was still
        # binding the port. The template hides the destructive
        # action cards and shows an "already recovered" banner +
        # auto-redirect.
        recovered = not fresh.needs_recovery
        # Map db state -> human reason; the template renders these
        # as the first checklist item's body.
        if fresh.state == _db.DbState.PRE_VERSIONING:
            reason = (
                "state.db has data tables but no bty_version marker -- "
                "this is a pre-versioning DB from a bty release that "
                "predates the strict version check (v0.30.x and earlier)."
            )
        elif fresh.state == _db.DbState.MISMATCH:
            reason = (
                f"state.db was created by bty v{fresh.stored_version}, "
                f"but the running code is v{fresh.running_version}. "
                f"Pre-1.0 policy: no migration apparatus -- every "
                f"release wipes state (or migrates via export+wipe+import)."
            )
        else:
            # OK / FRESH ended up here. Operator's polling browser
            # probably hit the OLD process between wipe and systemd
            # restart -- the template renders an "already recovered"
            # banner + auto-redirect so the operator doesn't stare
            # at a confused checklist.
            reason = (
                "state.db is now compatible with this bty-web release. "
                "Recovery is complete -- redirecting to the dashboard."
            )
        return jinja.get_template("ui/recovery.html").render(
            db_state=fresh.state,
            stored_version=fresh.stored_version,
            running_version=fresh.running_version,
            reason=reason,
            recovered=recovered,
            at_risk=at_risk,
            backups=backups,
            state_path=str(state_path),
        )

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/ui/recovery", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/recovery", include_in_schema=False)
    def _wizard() -> HTMLResponse:
        return HTMLResponse(_render_wizard())

    @app.get("/ui/recovery/status", include_in_schema=False)
    def _status() -> dict[str, Any]:
        fresh = _db.check_db(state_path)
        return {
            "state": fresh.state,
            "stored_version": fresh.stored_version,
            "running_version": fresh.running_version,
            "needs_recovery": fresh.needs_recovery,
            "at_risk": _read_at_risk_counts(state_path),
            "backups_count": len(_read_backup_listing(backups_root)),
        }

    @app.post("/ui/recovery/wipe", include_in_schema=False)
    def _wipe() -> JSONResponse:
        _wipe_state_db(state_path)
        # Schedule a clean process exit AFTER the response flushes
        # so the operator's browser sees a 200 with a redirect hint.
        # systemd's Restart=on-failure brings up a fresh process
        # with a clean state.db (init_db will stamp the current
        # version on its first call).
        _schedule_exit_after_response()
        return JSONResponse(
            {
                "status": "wiped",
                "path": str(state_path),
                "next": "systemd will restart bty-web; reload this page in a few seconds.",
            }
        )

    @app.post("/ui/recovery/wipe-and-import", include_in_schema=False)
    def _wipe_and_import(body: _WipeAndImportBody) -> JSONResponse:
        backup_id = body.backup_id
        if (
            not _db.check_db(state_path).needs_recovery
            and _db.check_db(state_path).state != _db.DbState.FRESH
        ):
            # OK state -- bail rather than wipe a healthy DB.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="state.db is OK; refusing to wipe + import",
            )
        # Validate backup_id shape (basename, no traversal).
        if (
            not backup_id
            or backup_id in (".", "..")
            or "/" in backup_id
            or "\\" in backup_id
            or "\x00" in backup_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid backup_id: {backup_id!r}",
            )
        bundle_path = backups_root / backup_id
        if not bundle_path.is_dir() or not (bundle_path / "manifest.json").is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no backup bundle at {bundle_path}",
            )
        _wipe_state_db(state_path)
        now = datetime.now(UTC).isoformat()
        try:
            summary = _portability.import_bundle(state_path, image_root, bundle_path, now=now)
        except _portability.BundleVersionMismatch as exc:
            # Format-version mismatch (bundle is from a release with
            # a different export schema). 409 because the operator
            # CAN retry with a different bundle.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            # ``manifest.json`` missing from the bundle dir, or the
            # bundle disappeared between the dir-check above and
            # import_bundle's read. Operator-actionable: pick a
            # different bundle.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"bundle is incomplete: {exc}",
            ) from exc
        except PermissionError as exc:
            # EACCES on the wipe or copy step. Operator-actionable:
            # check filesystem permissions on the appliance.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"permission denied during import: {exc}. "
                    f"Check ownership/mode of {state_path.parent} and "
                    f"{image_root} on the appliance."
                ),
            ) from exc
        except OSError as exc:
            # Disk full, I/O error, partial-copy rollback that
            # ``import_bundle`` already cleaned up. Operator-
            # actionable: free space and retry.
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail=(
                    f"storage error during import: {exc}. "
                    f"Free up space under {image_root.parent} and retry. "
                    f"Partial copies (if any) have been cleaned up; "
                    f"state.db was wiped and will be re-stamped on next start."
                ),
            ) from exc
        except Exception as exc:  # surface unknown import errors verbatim
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"import failed: {type(exc).__name__}: {exc}",
            ) from exc
        _schedule_exit_after_response()
        return JSONResponse(
            {
                "status": "imported",
                "backup_id": backup_id,
                "machines": summary.machines,
                "files": summary.files,
                "next": "systemd will restart bty-web; reload this page in a few seconds.",
            }
        )

    @app.get("/healthz", include_in_schema=False)
    def _healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "recovery",
                "reason": (
                    "state.db version mismatch -- bty-web is serving "
                    "the recovery wizard. See /ui/recovery."
                ),
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Catch-all 503 for any non-recovery path the operator hits by
    # habit (/ui/dashboard, /ui/machines, etc.). The HTML body
    # links back to /ui/recovery so a stuck bookmark doesn't leave
    # the operator on a JSON error.
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    def _catchall(full_path: str) -> HTMLResponse:
        del full_path
        body = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0; url=/ui/recovery">'
            "<title>bty-web (recovery mode)</title></head>"
            "<body><p>bty-web is in recovery mode. "
            '<a href="/ui/recovery">Continue to the recovery wizard.</a></p>'
            "</body></html>"
        )
        return HTMLResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    # Stash the check result on app state so tests can introspect.
    app.state.db_check = db_check
    app.state.state_path = state_path
    app.state.backups_root = backups_root
    app.state.image_root = image_root
    app.state.recovery_mode = True

    return app


def _schedule_exit_after_response() -> None:
    """Schedule ``os._exit(0)`` to fire shortly after the current
    HTTP response flushes.

    Implementation: spawn a background thread that sleeps briefly
    then exits the process. Uvicorn writes the response from the
    main asyncio loop; the thread's sleep covers that flush. We use
    ``os._exit`` (not ``sys.exit``) so atexit hooks don't run
    against the half-wiped state -- systemd's restart picks up a
    clean process.

    Side effect by design: a 200 OK response reaches the operator's
    browser, then the process dies. The browser's status poll on
    ``/ui/recovery/status`` gets connection errors for a few seconds
    until the new process binds the port; the wizard's polling code
    treats that as "wipe in progress, keep retrying."
    """
    import threading
    import time

    def _exit_soon() -> None:
        time.sleep(0.5)
        os._exit(0)

    t = threading.Thread(target=_exit_soon, daemon=True)
    t.start()
    # Surface that we're exiting so a test that imports the function
    # and pokes at it without a running app can see + skip the path.
    sys.stderr.flush()
