"""FastAPI application for bty-web.

``create_app(state_path, service_user, secret_key, boot_root)`` returns a fully
wired FastAPI instance. Tests construct one with a tmp_path SQLite +
a fixture service user (PAM gets monkeypatched in those tests).
``main()`` (in :mod:`bty.web.__init__`) builds one from environment
+ defaults and hands it to uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
import re
import shutil
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
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from nbdmux import client as nbdmux_client
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware
from withcache import oras as _oras

import bty
from bty import catalog as _catalog
from bty import images
from bty.web import (
    _backup,
    _db,
    _labels,
    _models,
    _release_mgr,
    _security,
    _settings_store,
    _ui,
    _withcache,
)
from bty.web._auth import (
    DEFAULT_ADMIN_PASSWORD,
    SESSION_COOKIE,
    require_auth,
    using_default_password,
)
from bty.web._events import (
    WORKER_STATE_CHANGED,
    MachineEvent,
    MachineEventBus,
    sse_format,
    worker_event,
)
from bty.web._events_log import acknowledge_event as _acknowledge_event
from bty.web._events_log import list_events as _list_events
from bty.web._events_log import record as _log_event
from bty.web._reqctx import client_ip as _client_ip
from bty.web._reqctx import normalise_mac as _normalise_mac

# Session cookie max-age. Sliding TTL on the browser side; Starlette's
# SessionMiddleware refreshes the cookie on each authed response, so
# active sessions stay alive while idle ones eventually expire.
_SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

# Cap on the stored ``lshw -json`` blob (POST /pxe/{mac}/inventory). A
# real lshw tree is tens of KB; 4 MiB tolerates a big server without
# letting a pathological / wrong payload bloat the machine row. Over
# the cap the blob is skipped (the prior one is kept) rather than
# truncated to invalid JSON.
LSHW_MAX_BYTES = 4 * 1024 * 1024

TEMPLATES_DIR = Path(__file__).parent / "_templates"
STATIC_DIR = Path(__file__).parent / "_static"


# Per-state_path display-timezone cache. The TZ rarely changes across
# a bty-web process lifetime so a single DB read per process (per
# state.db, in case tests stand up multiple) is enough. The Settings
# POST handler invalidates by calling :func:`invalidate_display_tz_cache`
# after a successful write so the next render picks up the new value.
_DISPLAY_TZ_CACHE: dict[str, Any] = {}  # str(state_path) -> ZoneInfo


def _cached_display_tz(state_path: Path) -> ZoneInfo:
    key = str(state_path)
    if key in _DISPLAY_TZ_CACHE:
        return _DISPLAY_TZ_CACHE[key]  # type: ignore[no-any-return]
    try:
        with _db.open_db(state_path) as conn:
            tz: ZoneInfo = _settings_store.resolve_display_timezone(conn)
    except Exception:
        # A bad stored value or a transient DB error must not 500
        # every template render. Fall back to UTC silently; the
        # Settings page is where the operator sees the parse error.
        tz = ZoneInfo("UTC")
    _DISPLAY_TZ_CACHE[key] = tz
    return tz


def invalidate_display_tz_cache(state_path: Path) -> None:
    """Drop the cached display TZ for ``state_path``. Called by the
    Settings POST handler after a successful display.timezone write
    so the next render reflects the change without a process restart.
    """
    _DISPLAY_TZ_CACHE.pop(str(state_path), None)


def create_app(
    *,
    state_path: Path,
    service_user: str,
    secret_key: str,
    boot_root: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app. All config flows through this function.

    Re-resolves the active config from the current environment before
    building the app. ``main()`` already calls ``set_active_config``
    upstream; the re-load here is idempotent for production AND lets
    tests monkeypatch ``BTY_*`` env vars after the conftest's default
    install: their changes show up because we re-read on every
    ``create_app`` call.

    ``service_user`` is the Linux account bty-web runs as (resolved from
    ``geteuid`` in :func:`bty.web.main`), shown in the UI for context.
    ``POST /ui/login`` is gated by ``cfg.admin.password`` (default
    ``"bty"``, env override ``BTY_ADMIN_PASSWORD``).

    ``secret_key`` is the per-server random key used by Starlette's
    :class:`SessionMiddleware` to sign session cookies. It must persist
    across bty-web restarts (otherwise every restart logs everyone out)
    and must be unique per server (otherwise a cookie minted by one
    server is valid on another). bty-web generates a 32-byte random key
    at ``<state_dir>/session-secret`` on first start when none is
    supplied via ``cfg.server.session_secret`` (TOML or env) or an
    existing file.

    ``boot_root`` is where the live-env artifacts (kernel + initrd +
    squashfs) live for the ``GET /boot/{name}`` endpoint; defaults to
    ``state_path.parent / "boot"`` (i.e. ``/var/lib/bty/boot`` in the
    default layout).
    """
    from bty.web import _config as _config_mod

    _config_mod.set_active_config(_config_mod.load_config(None))

    resolved_boot_root: Path = boot_root or (state_path.parent / "boot")
    # Scheduled + on-demand backups land under ``backups/`` next to
    # state.db so they survive the same migrate-the-state-dir flow as
    # the image cache. Operators wanting them off the OS disk override
    # via ``[paths] backup_dir`` in bty.toml (env override:
    # ``BTY_PATHS_BACKUP_DIR``). The cfg
    # field's blank-default resolves to ``<state_dir>/backups`` but
    # state_path here may diverge from cfg.state_dir (test fixtures
    # pass a temp state_path without setting cfg.state_dir), so honour
    # the explicit cfg override iff set; else hang the dir off
    # state_path's parent (the caller's actual state dir).
    from bty.web._config import cfg as _cfg

    _cfg_backup = _cfg().paths.backup_dir
    resolved_backups_root: Path = (
        Path(_cfg_backup) if _cfg_backup else (state_path.parent / "backups")
    )

    # schema mismatches are handled by ``_db.init_db``
    # auto-rotating ``state.db`` to ``state.db.<from>.<ts>.bak`` and
    # creating a fresh DB. The rotation is recorded as a
    # ``system.schema.reset`` event so the dashboard tripwire
    # surfaces it. No recovery-wizard branch here; ``open_db`` at
    # lifespan start does the right thing on its own.

    event_bus = MachineEventBus()

    # Optional catalog file. ``[paths] catalog_file`` (env override
    # ``BTY_PATHS_CATALOG_FILE``) wins over the ``<state_dir>/catalog.toml``
    # default. The catalog path is treated as the "always this file"
    # location so the UI can write a fresh ``catalog.toml`` to it and
    # reload in-process. When unset and the default file doesn't
    # exist yet, we still pin the default path so a
    # ``/ui/catalog/upload`` upload knows where to land.
    manifest_path = _catalog.default_manifest_path()
    if manifest_path is None:
        _cfg_catalog = _cfg().paths.catalog_file
        manifest_path = Path(_cfg_catalog) if _cfg_catalog else (state_path.parent / "catalog.toml")

    # Mutable holder so a runtime reload (operator uploads a new
    # catalog.toml from /ui/images) propagates to every closure-
    # captured handler below. Without this indirection,
    # ``catalog_state.catalog`` would be a local variable that no other
    # function can reassign.
    class _CatalogState:
        def __init__(self) -> None:
            self.catalog: _catalog.Catalog | None = None

    catalog_state = _CatalogState()
    if manifest_path.is_file():
        try:
            catalog_state.catalog = _catalog.load(manifest_path)
        except _catalog.CatalogError as exc:
            # Don't crash bty-web startup over a malformed catalog;
            # log it and proceed without the catalog feature. The
            # operator sees the empty catalog page + can upload a
            # fresh catalog from the UI to recover.
            print(f"bty-web: catalog at {manifest_path}: {exc}", file=sys.stderr)
            catalog_state.catalog = None
    release_fetch_manager = _release_mgr.ReleaseFetchManager()
    backup_manager = _backup.BackupManager()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        import logging as _logging

        _lifespan_log = _logging.getLogger(__name__)
        if using_default_password():
            _lifespan_log.warning(
                "Admin password is the well-known default %r. Change "
                "[admin] password in bty.toml (or $BTY_ADMIN_PASSWORD) "
                "for any deploy reachable beyond localhost.",
                DEFAULT_ADMIN_PASSWORD,
            )
        # The SSE event bus accepts publishes from worker threads -
        # capture the loop now so cross-thread publishes can hop in
        # via call_soon_threadsafe.
        event_bus.attach(asyncio.get_running_loop())
        # Wire each worker manager's state-change listener to the
        # bus. Every observable status transition (queued -> running,
        # queued -> cancelled, running -> terminal) fans out as a
        # ``worker-state-changed`` SSE event so the Backups / Hashing
        # / Downloads / Netboot pages get push-driven refreshes
        # instead of waiting on their safety poll.
        release_fetch_manager.set_state_listener(
            lambda s: event_bus.publish(worker_event("release", s.tag, s.status))
        )
        backup_manager.set_state_listener(
            lambda s: event_bus.publish(worker_event("backup", s.backup_id, s.status))
        )
        # Release-fetch manager: powers the trackable
        # /boot/releases endpoints (and the /ui/netboot page's
        # progress + cancel buttons). Default parallelism is 1
        # because fetching two GitHub releases in parallel is
        # operator-confusing and bandwidth-saturating.
        # Seed boot_root with baked bootstrap artifacts (the container
        # image bakes bty's custom iPXE binary here) so UEFI HTTP-Boot
        # clients can fetch GET /boot/ipxe.efi out of the box. No-op on
        # host / dev installs (BTY_BOOT_SEED_DIR unset).
        _seed_boot_dir(resolved_boot_root)
        release_fetch_manager.start(resolved_boot_root, state_path=state_path)
        # Backup manager: powers ``/workers/backups`` + the Backup
        # tab's "Back up now" button. Wraps ``_portability.export_bundle``
        # so a scheduled / on-demand backup ships the same operator-
        # owned bundle the ``bty-web export`` CLI does.
        backup_manager.start(
            state_path,
            resolved_backups_root,
        )
        # Auto-import manifest entries: a catalog.toml that survived a
        # restart (operator uploaded it, then bty-web restarted) needs
        # its entries reflected in catalog_entries so the
        # /ui/machines/{mac} dropdown shows them.
        if catalog_state.catalog is not None:
            _auto_import_manifest_rows(catalog_state.catalog)
        # Backup scheduler loop. Ticks every 60s; reads cadence +
        # last_run_at on every tick so a Settings change reflects
        # without restart. Stop signalled by ``backup_stop_event``,
        # which lets the loop wake immediately on shutdown rather
        # than waiting out the 60s sleep.
        backup_stop_event = asyncio.Event()
        backup_scheduler_task = asyncio.create_task(
            _backup.scheduler_loop(state_path, backup_manager, backup_stop_event)
        )
        try:
            yield
        finally:
            # Teardown order matters: drain the workers FIRST so any
            # final state-change publish (e.g., a hash that completes
            # 100ms before SIGTERM) makes it through the bus and to
            # the SSE subscribers BEFORE the bus closes. Pre-fix the
            # bus closed first, then ``stop()`` was awaited -- the
            # last-instant worker publish saw ``loop.is_running()``
            # still True and ``call_soon_threadsafe`` succeeded, but
            # the loop was already past the point where SSE
            # subscribers would drain it; the event was effectively
            # dropped.
            backup_stop_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_scheduler_task
            await release_fetch_manager.stop()
            await backup_manager.stop()
            # Wake every SSE subscribe() generator so the
            # StreamingResponse exits its yield loop. Without this,
            # browser tabs left open on /ui/machines or /ui/dashboard
            # hold the HTTP connection alive until uvicorn's 90s
            # graceful-shutdown timeout SIGKILLs the worker.
            await event_bus.close()

    jinja = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        # Autoescape only HTML (UI) templates; the iPXE ``.j2`` files
        # are plain text and would be mangled by escaping.
        autoescape=select_autoescape(enabled_extensions=("html",)),
        keep_trailing_newline=True,
    )
    # Expose the running bty-web version as a Jinja global so the iPXE
    # templates can construct versioned /boot/<name> URLs without each
    # render-site repeating the context plumbing. The netboot fetcher
    # writes files with this same version into BTY_BOOT_DIR.
    jinja.globals["bty_version"] = bty.__version__

    def _fmt_ts(value: object) -> str:
        """Render a timestamp as ``YYYY-MM-DD HH:MM:SS <TZ>``.

        The on-disk shape (``_now_iso``) is
        ``2026-05-17T20:21:09.155109+00:00`` -- microseconds and the
        raw ``+HH:MM`` offset are noise for an operator scanning a
        row. The renderer trims to second precision and converts to
        the configured display timezone
        (:func:`_settings_store.resolve_display_timezone`), then
        appends the zone's short name (e.g. ``UTC``, ``CEST``,
        ``EST``) so the value is unambiguous even when the operator
        cross-references against a shell clock in their local time.

        Default zone is UTC. An operator can override per-instance via
        the Settings UI or ``$BTY_DISPLAY_TZ``. The resolved zone is
        cached per state_path; the Settings POST handler invalidates
        the cache when it persists a new value.

        Accepts either an ISO-8601 string (DB columns) or a
        ``datetime`` (e.g. a file mtime). Defensive: returns the input
        unchanged on parse failure so a malformed value renders as
        itself rather than 500-ing the template render.
        """
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str) and value:
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return value
        else:
            return ""
        # All bty timestamps are written UTC; convert to the display
        # zone (UTC by default) before trimming. ``dt`` may be naive
        # if a caller passed e.g. a file mtime -- treat that as UTC
        # since that's bty's storage standard.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        tz = _cached_display_tz(state_path)
        dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S ") + (dt.tzname() or "")

    jinja.filters["fmt_ts"] = _fmt_ts

    # Linkify free-text event summaries: turn MAC addresses into links to
    # the machine page and http(s) URLs into clickable links, so an
    # operator scanning /ui/events can jump straight to the machine /
    # resource a row mentions. Everything outside a match is HTML-escaped,
    # so this stays XSS-safe even though it returns markup.
    _LINKIFY_RE = re.compile(
        r"(?P<url>https?://[^\s<>\"']+)"
        r"|(?P<mac>\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b)"
    )

    def _linkify(value: object) -> Markup:
        text = "" if value is None else str(value)
        out: list[str] = []
        last = 0
        for m in _LINKIFY_RE.finditer(text):
            out.append(html.escape(text[last : m.start()]))
            if m.lastgroup == "url":
                esc = html.escape(m.group("url"))
                out.append(f'<a href="{esc}" target="_blank" rel="noopener">{esc}</a>')
            else:
                esc = html.escape(m.group("mac"))
                # Machine MACs are stored normalised lowercase; link to
                # that, show the original casing.
                out.append(f'<a href="/ui/machines/{esc.lower()}"><code>{esc}</code></a>')
            last = m.end()
        out.append(html.escape(text[last:]))
        return Markup("".join(out))

    jinja.filters["linkify"] = _linkify

    def _boot_state(m: Any) -> str:
        """Lifecycle state for a machine -- the 'where in the cycle' half
        of the mode/state model. Empty for the non-alternating modes
        (ipxe-exit, bty-tui). The mode is the operator's intent; this
        is the transient position within it.

        Two signals feed the state:

        - ``saw_flasher_boot`` -- the bit armed when the box fetched a
          ``/boot`` artifact. Proves the iPXE chain ran, NOT that the
          live env actually reached ``bty`` and reported back.
        - ``known_disks_at`` / ``last_flashed_at`` -- the canonical
          completion signal for the mode (inventory POSTed / flash
          /done POSTed). Set ONLY by the live env on success.

        Pre-v0.33.22: state derived only from ``saw_flasher_boot``, so
        "inventoried; booting disk" / "flashed; booting disk" lit up
        the moment iPXE pulled the kernel -- BEFORE the live env had
        a chance to run, let alone report back. Operator-visible
        symptom: machine shows "inventoried" within seconds of
        discovery, well before the box could have actually inventoried.
        Fixed by gating the "done" labels on the matching completion
        signal AND surfacing a distinct "live env in progress" label
        for the in-between state.

        ``m`` is ``Any`` because Jinja can pass us a ``sqlite3.Row``,
        a plain dict, or a Pydantic dataclass depending on the call
        site -- they all index by string key.
        """
        try:
            mode = m["boot_mode"]
            armed = bool(m["saw_flasher_boot"])
        except (KeyError, TypeError, IndexError):
            return ""

        # Safe lookups for the completion signals -- some call sites
        # (e.g. mid-discovery rows surfacing on /events) might lack
        # these columns yet. Treat absent as "no signal".
        def _has(key: str) -> bool:
            try:
                return bool(m[key])
            except (KeyError, TypeError, IndexError):
                return False

        if mode == "bty-flash-once":
            if armed and _has("last_flashed_at"):
                return "flashed; booting disk"
            if armed:
                return "live env running; awaiting flash"
            return "pending flash"
        if mode == "bty-flash-always":
            if armed and _has("last_flashed_at"):
                return "flashed; booting disk"
            if armed:
                return "live env running; awaiting flash"
            return "ready to flash"
        if mode == "bty-inventory":
            if armed and _has("known_disks_at"):
                return "inventoried; booting disk"
            if armed:
                return "live env running; awaiting inventory"
            return "pending inventory"
        return ""

    jinja.filters["boot_state"] = _boot_state

    _db.init_db(state_path)

    app = FastAPI(title="bty-web", version=bty.__version__, lifespan=_lifespan)

    # Server-signed session cookie via Starlette's SessionMiddleware.
    # ``same_site="strict"`` blocks cross-site cookie attachment;
    # browsers won't auto-send the cookie on third-party requests.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=_SESSION_MAX_AGE,
        same_site="strict",
        https_only=False,  # bty-web serves plain HTTP on a homelab segment
    )

    # Vendored client-side assets (Bootstrap CSS, HTMX, htmx-ext-sse)
    # ship inside the wheel so bty-web has no runtime CDN
    # dependency. See ``_static/README.md`` for provenance.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render_machines_tbody() -> str:
        """Render the rows fragment used by /ui/machines and the SSE stream."""
        with _db.open_db(state_path) as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
            # Batch-fetch labels for the SSE-pushed tbody too (the
            # request-time list endpoint does the same).
            label_map: dict[str, list[str]] = {}
            for r in conn.execute(
                "SELECT mac, label FROM machine_labels ORDER BY mac, label"
            ).fetchall():
                label_map.setdefault(r["mac"], []).append(r["label"])
        machines = [_ui._row_to_dict(r, label_map.get(r["mac"], [])) for r in rows]
        return jinja.get_template("ui/_machines_tbody.html").render(machines=machines)

    def render_dashboard_panels() -> tuple[str, str]:
        """Render the two LIVE dashboard panels (machine summary,
        images) as separate SSE fragments, returned as
        ``(machine_html, images_html)``. Both come from the same
        ``_ui.dashboard_counts_context`` builder as the request-time
        dashboard render so they can't drift. They're published as
        distinct ``dashboard-machine`` / ``dashboard-images`` events
        because each lives in its own (independent, equally-spaced)
        dashboard column."""
        with _db.open_db(state_path) as conn:
            ctx = _ui.dashboard_counts_context(conn, _list_unified_images())
        return (
            jinja.get_template("ui/_dashboard_machine.html").render(**ctx),
            jinja.get_template("ui/_dashboard_images.html").render(**ctx),
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
        machine_html, images_html = render_dashboard_panels()
        event_bus.publish(MachineEvent(name="dashboard-machine", html=machine_html))
        event_bus.publish(MachineEvent(name="dashboard-images", html=images_html))

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
        host = _request_host(request)
        template = jinja.get_template("pxe_bootstrap.j2")
        return template.render(host=host)

    @app.get("/pxe/{mac}", response_class=PlainTextResponse)
    def pxe(mac: str, request: Request) -> str:
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            # Race-safe discovery. Two concurrent /pxe requests for
            # the same fresh MAC (iPXE retry, dnsmasq retransmit)
            # used to UNIQUE-violate the plain INSERT path; v0.33.6
            # moved to INSERT ... ON CONFLICT DO UPDATE ... RETURNING
            # which was race-safe on the row but had a subtle
            # discriminator race: the ``(created_at = ?) AS is_new``
            # synthetic column relied on timestamp comparison, so two
            # requests whose ``_now_iso()`` happened to tie (possible
            # on systems with lower clock resolution) BOTH saw
            # is_new=True and both logged a discovery event.
            #
            # split into INSERT-or-skip + UPDATE-touch.
            # The INSERT carries ``ON CONFLICT DO NOTHING RETURNING
            # 1`` -- the RETURNING row materialises iff the insert
            # actually fired (DO NOTHING suppresses it on conflict).
            # That's the canonical race-safe "did I create the row?"
            # signal in SQLite. The UPDATE then runs unconditionally
            # to refresh last_seen_*. Both statements share one
            # transaction so the pair stays atomic.
            inserted = conn.execute(
                """
                INSERT INTO machines
                    (mac, boot_mode,
                     discovered_at, last_seen_at, last_seen_ip,
                     created_at, updated_at)
                VALUES (?, 'bty-inventory', ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO NOTHING
                RETURNING 1
                """,
                (normalised, now, now, client_ip, now, now),
            ).fetchone()
            is_new = inserted is not None
            row = conn.execute(
                """
                UPDATE machines
                   SET last_seen_at  = ?,
                       last_seen_ip  = ?,
                       updated_at    = ?,
                       discovered_at = COALESCE(discovered_at, ?)
                 WHERE mac = ?
                RETURNING *
                """,
                (now, client_ip, now, now, normalised),
            ).fetchone()
            assert row is not None
            if is_new:
                # First /pxe contact -- worth a row in the audit log
                # so the operator can see "this MAC first checked in
                # at X" without paging through stale records. Only
                # logged on the discovery path; the upsert branch
                # would be too noisy to log every chain into the
                # live env.
                _log_event(
                    conn,
                    kind="machine.discovered",
                    summary=f"{normalised} first contacted /pxe from {client_ip or 'unknown IP'}",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                    # Mirror the shape of ``machine.created`` /
                    # ``machine.upserted``: same 5 keys with the
                    # row's actual values at discovery time. The
                    # auto-created row carries the bty-inventory
                    # default; everything else is NULL until the
                    # operator binds it. Symmetric payloads let
                    # the operator pivot on a MAC across the audit
                    # log without each event having a different shape.
                    details={
                        "bty_image_ref": None,
                        "boot_mode": "bty-inventory",
                        "sanboot_drive": None,
                        "labels": [],
                        "target_disk_serial": None,
                    },
                )
            conn.commit()

        machine = dict(row)
        # The ipxe templates render ``machine.labels`` in the
        # comment header. labels live in a side-table, so they
        # aren't on the row; fetch + plumb them in here.
        with _db.open_db(state_path) as _conn:
            machine["labels"] = _labels.get_labels(_conn, normalised)
        publish_state_changed()
        # Boot-mode decision tree (highest priority first):
        #   - bty-tui                       -> live env, interactive wizard
        #   - ipxe-exit                     -> iPXE ``sanboot`` verb / firmware
        #                                      exit -- boots the local disk
        #   - bty-flash-always / -once + ref + target disk -> live env auto-flash
        #     EXCEPT bty-flash-always with saw_flasher_boot set -> the
        #     ipxe-exit chain once (one-shot loop-break, see below), so
        #     the just-flashed disk boots between reflashes.
        #   - else (no usable binding / stale policy) -> ipxe_unknown.j2
        #     (sanboot 0x80 || exit)
        # Completion signal (POST /pxe/{mac}/done) updates
        # last_flashed_at + saw_flasher_boot regardless of policy;
        # boot_mode itself is not mutated (as of v0.25.0). For
        # bty-flash-once the next plan-emit observes the machine as
        # flashed and returns the ipxe-exit chain instead of the flash
        # chain, so the box boots its freshly-flashed disk. For
        # bty-flash-always the same saw_flasher_boot bit drives the
        # one-shot loop-break above: alternates flash-chain and
        # ipxe-exit across boots so the just-flashed disk actually
        # boots once before the next reflash.
        # The flip is driven by ``saw_flasher_boot``: armed when the box
        # fetches a /boot artifact (proof it booted the flasher),
        # consumed here on the following /pxe contact. Without this,
        # PXE-first firmware would reflash on every reboot forever.
        host = _request_host(request)
        policy = machine.get("boot_mode")
        ref = machine.get("bty_image_ref")

        # First decide the offer (template + summary + details) and
        # gather any pending side-effects (saw_flasher_boot clear).
        # The single ``with _db.open_db`` at the end of the handler
        # applies them in one transaction alongside the always-runs
        # ``pxe.offered`` event. Pre-v0.33.26 each branch opened its
        # own connection; six open_db calls per request was
        # gratuitous on a hot path. Flash-failure branches (orphan
        # ref / no target disk) used to log standalone events too;
        # that info is already encoded as the ``reason`` in
        # ``pxe.offered.details`` so the standalone events were
        # duplicative noise.
        rendered: str
        offer_kind: str
        offer_summary: str
        offer_details: dict[str, Any]
        clear_saw_flasher_boot = False

        if policy == "bty-tui":
            template = jinja.get_template("ipxe_tui.j2")
            rendered = template.render(mac=normalised, machine=machine, host=host)
            offer_kind = "bty-tui"
            offer_summary = f"{normalised} offered tui (operator picks via bty on tty1)"
            offer_details = {"offer": "bty-tui"}
        elif policy == "bty-inventory":
            # Inventory-then-sanboot, alternating like bty-flash-always
            # (same saw_flasher_boot bit). When the box has booted the
            # live env (bit armed via GET /boot/...?mac=) AND the live
            # env actually POSTed inventory (``known_disks_at`` is set),
            # serve a sanboot + clear the bit. Otherwise serve the
            # live-env chain.
            #
            # the bit ALONE used to gate the sanboot serve.
            # If the live env crashed between fetching /boot and POSTing
            # /pxe/{mac}/inventory, the bit stayed armed and the server
            # served sanboot of an empty disk -- the box failed to boot,
            # cycled, the next /pxe cleared the bit, then re-served the
            # inventory chain. One wasted sanboot cycle per crashed
            # inventory. Now: armed-without-known_disks_at is treated as
            # "live env didn't complete; retry the chain". Self-healing
            # without the wasted sanboot.
            armed = bool(machine.get("saw_flasher_boot"))
            has_inventory = bool(machine.get("known_disks_at"))
            if armed and has_inventory:
                drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
                template = jinja.get_template("ipxe_sanboot.j2")
                rendered = template.render(
                    mac=normalised, machine=machine, drive=drive, policy=policy
                )
                clear_saw_flasher_boot = True
                offer_kind = "bty-inventory-sanboot"
                offer_summary = (
                    f"{normalised} booting disk (drive {drive}) after inventory; "
                    f"bty-inventory re-arms on next netboot"
                )
                offer_details = {
                    "offer": "sanboot",
                    "sanboot_drive": drive,
                    "after_inventory": True,
                }
            else:
                template = jinja.get_template("ipxe_tui.j2")
                rendered = template.render(mac=normalised, machine=machine, host=host)
                offer_kind = "bty-inventory"
                if armed and not has_inventory:
                    offer_summary = (
                        f"{normalised} re-offered inventory boot "
                        f"(prior live env armed but didn't POST inventory)"
                    )
                    offer_details = {
                        "offer": "bty-inventory",
                        "retry_after_armed_no_post": True,
                    }
                else:
                    offer_summary = (
                        f"{normalised} offered inventory boot (bty collects disks + reboots)"
                    )
                    offer_details = {"offer": "bty-inventory"}
        elif policy == "ipxe-exit":
            # iPXE boots the local disk itself (drive override, default
            # 0x80), with ``|| exit`` falling back to the firmware boot
            # order. Checked before the generic ``ref`` branch so a
            # sanboot machine with an image bound still sanboots rather
            # than falling through to the ``exit`` (local) template.
            drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
            template = jinja.get_template("ipxe_sanboot.j2")
            rendered = template.render(mac=normalised, machine=machine, drive=drive, policy=policy)
            offer_kind = "ipxe-exit"
            offer_summary = f"{normalised} offered sanboot (iPXE boots local drive {drive})"
            offer_details = {"offer": "sanboot", "sanboot_drive": drive}
        elif policy == "ramboot":
            # ramboot chains the slim ``ramboot-init`` live env (kernel
            # + initrd only, no squashfs). The initramfs nbd-client
            # connects to the operator-configured nbdmux, mounts the
            # catalog image's largest partition, overlays a tmpfs for
            # writes, and pivot_roots before /sbin/init.
            #
            # Gates: nbdmux URL configured, ref bound, AND the ref is
            # registered with nbdmux at status='ready'. The readiness
            # check delegates to nbdmux (since v0.2.0 nbdmux owns the
            # warming pipeline + its own ramboot_cache-equivalent
            # state machine); bty doesn't keep a local mirror. Any
            # gate open falls back to ipxe_tui so the operator hits
            # the wizard instead of the box hard-paniccing in the
            # initramfs.
            with _db.open_db(state_path) as conn:
                nbdmux_url = _settings_store.resolve_nbdmux_url(conn)
                overlay_size = _settings_store.resolve_ramboot_overlay_size(conn)
            ramboot_ready = False
            if nbdmux_url and ref:
                try:
                    exports = nbdmux_client.list_exports(server=nbdmux_url, timeout=2.0)
                except nbdmux_client.NbdmuxError:
                    exports = []
                ramboot_ready = any(
                    e.get("name") == str(ref) and e.get("status") == "ready" for e in exports
                )
            if nbdmux_url and ref and ramboot_ready:
                # Derive the NBD host from the configured HTTP control
                # plane URL: same hostname, port 10809 (nbd-server's
                # listener; bty-web posts exports against port 8082).
                parsed = urllib.parse.urlsplit(nbdmux_url)
                nbd_host = parsed.hostname or host.split(":")[0]
                template = jinja.get_template("ipxe_ramboot.j2")
                rendered = template.render(
                    mac=normalised,
                    machine=machine,
                    host=host,
                    nbd_host=nbd_host,
                    nbd_port=10809,
                    image_ref=ref,
                    overlay_size=overlay_size,
                )
                offer_kind = "ramboot"
                offer_summary = (
                    f"{normalised} offered ramboot via nbd://{nbd_host}:10809/{ref[:8]}..."
                )
                offer_details = {
                    "offer": "ramboot",
                    "nbd_endpoint": f"tcp://{nbd_host}:10809",
                    "image_ref": ref,
                    "overlay_size": overlay_size,
                }
            else:
                template = jinja.get_template("ipxe_tui.j2")
                rendered = template.render(mac=normalised, machine=machine, host=host)
                offer_kind = "ramboot-fallback-tui"
                if not nbdmux_url:
                    reason = "nbdmux URL not configured"
                elif not ref:
                    reason = "no bty_image_ref bound"
                else:
                    reason = "image not pre-warmed yet"
                offer_summary = f"{normalised} offered tui (boot_mode=ramboot but {reason})"
                offer_details = {
                    "offer": "bty-tui",
                    "reason": f"ramboot misconfigured: {reason}",
                }
        elif ref and policy in ("bty-flash-always", "bty-flash-once"):
            # Safety gate: refuse the flash chain unless the operator
            # has picked a target disk by serial. Without this, ``bty``
            # in auto-flash mode would have no disk pinned and refuse
            # at the plan endpoint anyway -- but landing on ipxe.j2
            # (sanboot fallback) here makes the misconfiguration
            # immediately visible: the box doesn't even chain into
            # the live env. The matching pxe.flash.no_target_disk
            # event surfaces the refusal on /ui/events.
            target_disk_serial = machine.get("target_disk_serial")
            image_name = _flash_target_for_ref(str(ref))
            if image_name is not None and target_disk_serial:
                armed = bool(machine.get("saw_flasher_boot"))
                has_flashed = bool(machine.get("last_flashed_at"))
                if armed and has_flashed:
                    # The box fetched a /boot artifact AND POSTed
                    # /pxe/{mac}/done since we served the flash chain --
                    # proof it actually flashed (not just iPXE-armed).
                    # Serve sanboot of the just-flashed disk. The bit
                    # handling is what makes the two modes differ:
                    #   * bty-flash-always: CLEAR the bit, so the next
                    #     real netboot flips back to the flash chain --
                    #     the flash<->sanboot alternation that stops a
                    #     PXE-first reflash loop.
                    #   * bty-flash-once: KEEP the bit. Terminal state:
                    #     the box sanboots its disk from now on. The mode
                    #     STAYS bty-flash-once; re-arms only when the
                    #     operator re-saves the machine.
                    #
                    # armed-without-last_flashed_at used to
                    # also serve sanboot. That sanbooted a half-flashed
                    # disk -- bty-flash-always recovered via the next
                    # cycle (wasted one sanboot); bty-flash-once was
                    # TERMINALLY STUCK on the half-flashed disk and
                    # required operator intervention. Now armed-without-
                    # last_flashed_at re-serves the flash chain so the
                    # crashed flasher retries until /done lands.
                    drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
                    template = jinja.get_template("ipxe_sanboot.j2")
                    rendered = template.render(
                        mac=normalised, machine=machine, drive=drive, policy=policy
                    )
                    if policy == "bty-flash-always":
                        clear_saw_flasher_boot = True
                    offer_kind = f"{policy}-sanboot"
                    offer_summary = f"{normalised} booting just-flashed disk (drive {drive}); " + (
                        "bty-flash-always re-arms on next netboot"
                        if policy == "bty-flash-always"
                        else "bty-flash-once complete (stays on this disk)"
                    )
                    offer_details = {
                        "offer": "sanboot",
                        "sanboot_drive": drive,
                        "sanboot_after_flash": True,
                    }
                else:
                    template = jinja.get_template("ipxe_flash.j2")
                    # The kernel cmdline only carries bty.server + bty.mac
                    # (v0.22.10+); the image URL + target serial come from
                    # /pxe/<mac>/plan. The flash_key + target_disk_serial
                    # context vars feed the template's HEADER COMMENT block
                    # so an operator inspecting curl output can see what
                    # this chain is bound to.
                    rendered = template.render(
                        mac=normalised,
                        machine=machine,
                        host=host,
                        flash_key=str(ref),
                        target_disk_serial=target_disk_serial,
                    )
                    offer_kind = policy  # "bty-flash-always" or "bty-flash-once"
                    short = str(ref)[:12]
                    # Distinguish a fresh-cycle offer from a retry-because-
                    # crashed-flasher offer for the audit log. Both serve
                    # the same template (the flash chain), but the
                    # ``retry_after_armed_no_done`` flag tells the
                    # operator "the last cycle armed the bit but never
                    # /done'd; the flasher crashed mid-flash".
                    if armed and not has_flashed:
                        offer_summary = (
                            f"{normalised} re-offered {policy} for ref={short}... "
                            f"(prior live env armed but didn't POST /done)"
                        )
                        offer_details = {
                            "offer": policy,
                            "bty_image_ref": ref,
                            "image_name": image_name,
                            "target_disk_serial": target_disk_serial,
                            "retry_after_armed_no_done": True,
                        }
                    else:
                        offer_summary = (
                            f"{normalised} offered {policy} for ref={short}... "
                            f"({image_name}, target serial {target_disk_serial})"
                        )
                        offer_details = {
                            "offer": policy,
                            "bty_image_ref": ref,
                            "image_name": image_name,
                            "target_disk_serial": target_disk_serial,
                        }
            elif image_name is not None and not target_disk_serial:
                # Image binding is resolvable but no target disk
                # picked. Fall back to ipxe.j2 (exit to firmware).
                # The ``reason: no_target_disk`` flag in the always-
                # runs pxe.offered event makes this distinguishable
                # from "orphan ref / no bindable image" on /ui/events.
                template = jinja.get_template("ipxe.j2")
                rendered = template.render(mac=normalised, machine=machine)
                offer_kind = "exit-fallback"
                offer_summary = (
                    f"{normalised} offered exit (firmware boot) "
                    f"(boot_mode={policy} but no target_disk_serial picked)"
                )
                offer_details = {
                    "offer": "exit-fallback",
                    "bty_image_ref": ref,
                    "reason": "no_target_disk",
                    "boot_mode": policy,
                }
            else:
                # Orphaned binding: machine targets a ref that no
                # catalog_entries row resolves. Falls back to ipxe.j2
                # (exit to firmware). The ``reason: orphan_ref`` flag
                # in the always-runs pxe.offered event surfaces this
                # to the operator on /ui/events.
                short = str(ref)[:12]
                template = jinja.get_template("ipxe.j2")
                rendered = template.render(mac=normalised, machine=machine)
                offer_kind = "exit-fallback"
                offer_summary = (
                    f"{normalised} offered exit (firmware boot) "
                    f"(boot_mode={policy} but ref={short}... is orphaned)"
                )
                offer_details = {
                    "offer": "exit-fallback",
                    "bty_image_ref": ref,
                    "reason": "orphan_ref",
                }
        else:
            # Known machine that doesn't resolve to a chain: a flash
            # policy with no image ref bound, or a stale/invalid
            # boot_mode. ipxe_unknown.j2 sanboots the first disk
            # (``sanboot --drive 0x80 || exit``), so the box still boots
            # whatever it has rather than wedging on the network.
            template = jinja.get_template("ipxe_unknown.j2")
            rendered = template.render(mac=normalised, machine=machine)
            offer_kind = "unknown"
            offer_summary = f"{normalised} offered sanboot/exit -- no bty_image_ref bound"
            offer_details = {"offer": "unknown"}

        # Apply collected side-effects + audit every PXE hit in one
        # transaction. The events table is append-only with no
        # retention cap today; long-running per-job CI loops will
        # grow it indefinitely. If that becomes a problem, the
        # ``netboot.pxe.offered`` kind is a natural candidate for a
        # subject-id-keyed rolling-window prune ("keep the last 100
        # per MAC").
        with _db.open_db(state_path) as conn:
            if clear_saw_flasher_boot:
                conn.execute(
                    "UPDATE machines SET saw_flasher_boot = 0, updated_at = ? WHERE mac = ?",
                    (now, normalised),
                )
            _log_event(
                conn,
                kind="netboot.pxe.offered",
                summary=offer_summary,
                subject_kind="machine",
                subject_id=normalised,
                actor="pxe-client",
                source_ip=client_ip,
                details={"boot_mode": policy, **offer_details, "offer_kind": offer_kind},
            )
            conn.commit()

        return rendered

    @app.post(
        "/pxe/{mac}/status",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def pxe_status(mac: str, body: _models.PxeStatus, request: Request) -> Response:
        # Terminal flash signal from the live env; ``status`` in the body
        # picks the outcome. Open route: the live env hits this from the
        # PXE-booted target, which has no token. Trust model: bty-web is for
        # trusted networks (homelab / CI), not the open internet -- same as
        # the other ``/pxe/*`` endpoints.
        #
        #   done   -> records last_flashed_at + a machine.flashed event. It
        #             does NOT mutate boot_mode (the mode is the operator's
        #             intent and stays put); the post-flash "boot the disk"
        #             behaviour comes from the saw_flasher_boot bit instead,
        #             armed when the box fetched the flasher's /boot
        #             artifacts. For bty-flash-once that bit is terminal; for
        #             bty-flash-always the /pxe handler clears it to re-arm.
        #   failed -> records a machine.flash_failed event carrying ``reason``
        #             and leaves last_flashed_at untouched, so the machine
        #             shows the failure instead of sitting at "awaiting flash"
        #             forever while the live env has already given up.
        #
        # Either way last_seen is refreshed (the POST is a live-env heartbeat).
        normalised = _normalise_mac(mac)
        now = _now_iso()
        client_ip = _client_ip(request)
        failed = body.status == "failed"
        reason = body.reason.strip()[:500]
        with _db.open_db(state_path) as conn:
            if failed:
                cur = conn.execute(
                    "UPDATE machines SET last_seen_at = ?, last_seen_ip = ?, "
                    "updated_at = ? WHERE mac = ?",
                    (now, client_ip, now, normalised),
                )
            else:
                cur = conn.execute(
                    "UPDATE machines SET last_flashed_at = ?, last_seen_at = ?, "
                    "last_seen_ip = ?, updated_at = ? WHERE mac = ?",
                    (now, now, client_ip, now, normalised),
                )
            if cur.rowcount > 0 and failed:
                _log_event(
                    conn,
                    kind="machine.flash_failed",
                    summary=(
                        f"{normalised} reported flash failure" + (f": {reason}" if reason else "")
                    ),
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                    details={"reason": reason} if reason else None,
                )
            elif cur.rowcount > 0:
                _log_event(
                    conn,
                    kind="machine.flashed",
                    summary=f"{normalised} signalled flash completion",
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                )
            else:
                # Surface the orphan to /ui/events: a live env reported a
                # terminal status for a MAC we have no row for (operator
                # deleted mid-cycle, a foreign bty-web, or someone poking the
                # endpoint directly).
                _log_event(
                    conn,
                    kind="pxe.client.orphan",
                    summary=(
                        f"{normalised} POSTed /status ({body.status}) but no machine "
                        f"record exists (operator deleted mid-cycle, or MAC mismatch "
                        f"from a foreign live env)"
                    ),
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                    details={"signal": body.status},
                )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        publish_state_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/pxe/{mac}/plan")
    def pxe_plan(mac: str, request: Request) -> dict[str, Any]:
        """Return the per-MAC plan as JSON so ``bty --server X --mac
        Y`` can dispatch without operator input.

        Plan shapes (mode is the dispatch token):

        * ``{"mode": "flash", "image": URL, "target_disk_serial": S,
          "disk_image_sha": HEX}`` -- boot_mode in (bty-flash-always,
          bty-flash-once) with a bindable ref AND a target_disk_serial
          picked. ``bty`` runs the flash without prompts and verifies the
          streamed bytes against ``disk_image_sha``.
        * ``{"mode": "interactive", "catalog": URL}`` -- boot_mode
          ``tui``, OR a flash policy that can't be auto-resolved
          (no target serial, orphan ref). ``bty`` drops the operator
          into the wizard with the server's catalog pre-loaded.
        * ``{"mode": "exit"}`` -- boot_mode=ipxe-exit (handled at the
          iPXE layer, so the box doesn't reach the live env) or any
          unrecognised policy. ``bty`` exits cleanly; the firmware /
          ipxe-exit path handles boot.

        Unknown MACs auto-register (matches the ``/pxe/{mac}`` iPXE
        endpoint) with boot_mode=bty-tui so a hand-launched ``bty
        --mac X`` from a fresh box gets a wizard plan rather than
        a 404.

        Server-vs-client truth asymmetry: ``mode=flash`` is the only
        path that makes the server the source of truth for what
        gets flashed. ``mode=interactive`` returns a catalog URL
        but ``bty`` does NOT report back which entry the operator
        picks -- the only feedback channels are
        ``/pxe/<mac>/inventory`` (disk list, posted at startup) and
        ``/pxe/<mac>/done`` (boolean "a flash completed", posted
        after success). The machine record's ``bty_image_ref`` /
        ``target_disk_serial`` fields stay untouched by interactive
        flashes. Operators who want server-tracked flashes must
        configure boot_mode=bty-flash-always + bind a ref + pick a serial.

        Open endpoint: same trust model as the rest of /pxe/*
        (homelab / CI network, not the internet).
        """
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            # Race-safe discovery: see /pxe/{mac}'s upsert comment for
            # the rationale on INSERT...DO NOTHING RETURNING 1 (race-
            # safe is_new) followed by an unconditional UPDATE.
            inserted = conn.execute(
                """
                INSERT INTO machines
                    (mac, boot_mode,
                     discovered_at, last_seen_at, last_seen_ip,
                     created_at, updated_at)
                VALUES (?, 'bty-inventory', ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO NOTHING
                RETURNING 1
                """,
                (normalised, now, now, client_ip, now, now),
            ).fetchone()
            is_new = inserted is not None
            row = conn.execute(
                """
                UPDATE machines
                   SET last_seen_at  = ?,
                       last_seen_ip  = ?,
                       updated_at    = ?,
                       discovered_at = COALESCE(discovered_at, ?)
                 WHERE mac = ?
                RETURNING *
                """,
                (now, client_ip, now, now, normalised),
            ).fetchone()
            assert row is not None
            if is_new:
                _log_event(
                    conn,
                    kind="machine.discovered",
                    summary=(
                        f"{normalised} first contacted /pxe/{normalised}/plan "
                        f"from {client_ip or 'unknown IP'}"
                    ),
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                    # Mirror ``machine.created`` / ``machine.upserted``;
                    # see /pxe/{mac} for rationale.
                    details={
                        "bty_image_ref": None,
                        "boot_mode": "bty-inventory",
                        "sanboot_drive": None,
                        "labels": [],
                        "target_disk_serial": None,
                    },
                )
            conn.commit()

        machine = dict(row)
        # The ipxe templates render ``machine.labels`` in the
        # comment header. labels live in a side-table, so they
        # aren't on the row; fetch + plumb them in here.
        with _db.open_db(state_path) as _conn:
            machine["labels"] = _labels.get_labels(_conn, normalised)
        publish_state_changed()

        host = _request_host(request)
        base = f"http://{host}"
        policy = machine.get("boot_mode")
        ref = machine.get("bty_image_ref")

        plan: dict[str, Any]
        offer_kind: str
        # Set on the flash path when the image source is an HTTP(S) URL;
        # folded into the plan event details below for observability.
        cache_decision: dict[str, Any] | None = None
        if policy in ("bty-flash-always", "bty-flash-once") and ref:
            target_disk_serial = machine.get("target_disk_serial")
            # One DB connection for the whole flash-plan resolution: the
            # catalog binding (name/format/src/resolved_src) plus the
            # withcache lookup, rather than an open-per-field on this hot
            # path. is_cached's network HEAD stays OUTSIDE the connection.
            with _db.open_db(state_path) as conn:
                _b = conn.execute(
                    "SELECT name, format, src, resolved_src, disk_image_sha "
                    "FROM catalog_entries WHERE bty_image_ref = ?",
                    (str(ref),),
                ).fetchone()
                image_name = str(_b["name"]) if _b and _b["name"] else None
                fmt = str(_b["format"]) if _b and _b["format"] else None
                src = str(_b["src"]) if _b and _b["src"] else None
                resolved_src = str(_b["resolved_src"]) if _b and _b["resolved_src"] else None
                # Content hash for on-wire verification. Distinct from
                # ``ref`` (= bty_image_ref = sha256 of the canonical URL,
                # an identifier, NOT the bytes). NULL when the entry was
                # imported without a known sha -> omitted below so the
                # live env flashes without verifying.
                disk_image_sha = str(_b["disk_image_sha"]) if _b and _b["disk_image_sha"] else None
                # withcache's lookup keys on ``src`` only (is_cached / blob_url
                # both take ``src``, never ``resolved_src``), so do NOT gate it
                # on resolved_src being populated -- that wrongly forced entries
                # whose import left resolved_src NULL to origin even when the
                # cache was warm. Match the UI/warm path (``_ui.py``), which
                # resolves the cache URL from ``conn`` alone. ``resolved_src``
                # stays purely the non-withcache fallback below.
                withcache_url = (
                    _settings_store.resolve_withcache_url(conn)
                    if image_name is not None and target_disk_serial
                    else None
                )
            if image_name is not None and target_disk_serial:
                # The client detects image format from the URL name's
                # extension. An oras title ("nosi fedora-sysdev (x86_64,
                # rolling)") has none, so the flash gets rejected as
                # "format not recognised". When the catalog name carries
                # no usable extension, synthesise a filename from the
                # stored format so even older clients detect it; the
                # ``{ref}`` segment is what actually resolves the bytes,
                # so the name is free to change.
                # The live env reaches the bytes one of three ways:
                # withcache (when configured + warm), the canonical
                # plain-HTTPS ``resolved_src`` (for catalog rows whose
                # import-time resolution populated it), or the original
                # ``src`` -- ``oras://`` URLs included, which the live
                # env's bty TUI handles via ``withcache.oras`` (resolve
                # + bearer mint + curl in the same process).
                is_oras = src is not None and src.startswith("oras://")
                image_url = src or ""
                if src is not None:
                    if withcache_url and _withcache.is_cached(withcache_url, src):
                        image_url = _withcache.blob_url(withcache_url, src)
                        cache_hit = True
                    elif resolved_src is not None:
                        # Plain-HTTPS canonical URL stored at import
                        # time. Same URL as src for an http(s) entry;
                        # the resolved blob URL for oras (which the
                        # live env can't fetch anonymously, so prefer
                        # ``src`` below if no withcache).
                        image_url = resolved_src if not is_oras else src
                        cache_hit = False
                    else:
                        cache_hit = False
                    cache_decision = {
                        "configured": bool(withcache_url),
                        "hit": cache_hit if withcache_url else None,
                        "served_from": "withcache" if cache_hit else "origin",
                    }
                plan = {
                    "mode": "flash",
                    "image": image_url,
                    "target_disk_serial": str(target_disk_serial),
                    # Descriptive catalog name for display: the image URL's
                    # last segment may be a synthesised "image.<fmt>" (so
                    # the client can detect format), which is uninformative
                    # on the flash screen. ``name`` carries the real title.
                    "name": image_name,
                }
                # Content sha so the live env verifies the bytes on the
                # wire even when ``image`` is a withcache / direct-origin
                # URL that doesn't embed the digest. Omitted when unknown
                # (the live env then flashes without verification).
                if disk_image_sha:
                    plan["disk_image_sha"] = disk_image_sha
                # Also pass it explicitly for newer clients.
                if fmt:
                    plan["format"] = fmt
                offer_kind = f"plan:flash:{policy}"
            else:
                # Flash policy but the auto-resolve failed (no target
                # serial picked or orphan ref). Drop the operator into
                # the wizard so they can pick + finish manually.
                plan = {"mode": "interactive", "catalog": f"{base}/catalog.toml"}
                offer_kind = "plan:interactive:flash-unresolved"
        elif policy == "bty-tui":
            plan = {"mode": "interactive", "catalog": f"{base}/catalog.toml"}
            offer_kind = "plan:interactive:tui"
        elif policy == "bty-inventory":
            # The live env booted to (re)collect inventory. ``bty`` has
            # already auto-posted /pxe/<mac>/inventory by the time it
            # GETs the plan; ``mode=inventory`` tells it to reboot rather
            # than wait at a wizard. The reboot lands on the
            # saw_flasher_boot-armed /pxe contact, which sanboots the
            # disk. (If the box never armed the bit, it just re-collects
            # next cycle -- self-healing, like bty-flash-always.)
            plan = {"mode": "inventory"}
            offer_kind = "plan:inventory"
        else:
            # boot_mode=ipxe-exit (or any other / missing) -- ``bty``
            # has nothing to do (ipxe-exit is handled at the iPXE layer,
            # the box never chains into the live env); plan mode=exit
            # means "exit cleanly, let firmware / disk boot".
            plan = {"mode": "exit"}
            offer_kind = f"plan:exit:{policy}"

        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="netboot.pxe.plan",
                summary=f"{normalised} plan offered: mode={plan['mode']} (policy={policy})",
                subject_kind="machine",
                subject_id=normalised,
                actor="pxe-client",
                source_ip=client_ip,
                details={
                    "plan": plan,
                    "boot_mode": policy,
                    "offer_kind": offer_kind,
                    **({"withcache": cache_decision} if cache_decision else {}),
                },
            )
            conn.commit()
        return plan

    @app.post(
        "/pxe/{mac}/inventory",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def pxe_inventory(mac: str, body: _models.InventoryPost, request: Request) -> Response:
        """Receive the per-MAC disk inventory from the live env's
        ``bty`` on startup.

        Open endpoint -- the live env has no operator session.
        Trust model matches the rest of ``/pxe/*``: bty-web is for
        trusted networks (homelab / CI).

        Persists the inventory as a JSON blob on the machine row
        so the /ui/machines/{mac} dropdown can show real
        path/model/serial values picked from the box's actual
        hardware (instead of asking the operator to type the
        serial by hand). Updates ``known_disks_at`` to the receive
        time so the UI can show "last inventory: X seconds ago".

        404s if the MAC has no machine record. The live env only
        posts after a successful PXE chain, so the machine row
        should always exist; a 404 here means the operator
        deleted the machine in /ui/machines while the live env
        was still booting.
        """
        normalised = _normalise_mac(mac)
        now = _now_iso()
        client_ip = _client_ip(request)
        # Serialise as JSON with stable key order so two inventories
        # with the same disks produce byte-identical column values.
        disks_payload = [d.model_dump() for d in body.disks]
        disks_json = json.dumps(disks_payload, sort_keys=True)
        # Supplementary lshw -json blob. Cap its size so a pathological
        # tree can't bloat the row; over the cap we skip storing it
        # (and keep any prior blob) rather than truncating to invalid
        # JSON. ``None`` (lshw not posted / failed) leaves the prior
        # blob untouched via COALESCE -- a boot where lshw hiccuped
        # shouldn't wipe good hardware data.
        lshw_json: str | None = None
        lshw_oversize = False
        if body.lshw is not None:
            candidate = json.dumps(body.lshw, sort_keys=True)
            if len(candidate.encode("utf-8")) <= LSHW_MAX_BYTES:
                lshw_json = candidate
            else:
                lshw_oversize = True
        with _db.open_db(state_path) as conn:
            # last_seen_at + last_seen_ip touched alongside the
            # completion signal: an inventory POST is a live-env
            # heartbeat too. Pre-fix the operator's "last seen"
            # timestamp on /ui/machines could lag the most recent
            # contact by minutes if the live env POSTed inventory
            # then sat at the wizard.
            cur = conn.execute(
                "UPDATE machines SET known_disks = ?, known_disks_at = ?, "
                "hw_lshw = COALESCE(?, hw_lshw), "
                "hw_lshw_at = CASE WHEN ? IS NOT NULL THEN ? ELSE hw_lshw_at END, "
                "last_seen_at = ?, last_seen_ip = ?, "
                "updated_at = ? WHERE mac = ?",
                (disks_json, now, lshw_json, lshw_json, now, now, client_ip, now, normalised),
            )
            if cur.rowcount == 0:
                _log_event(
                    conn,
                    kind="pxe.client.orphan",
                    summary=(
                        f"{normalised} POSTed /inventory but no machine record exists "
                        f"(operator deleted mid-cycle, or MAC mismatch from a foreign live env)"
                    ),
                    subject_kind="machine",
                    subject_id=normalised,
                    actor="pxe-client",
                    source_ip=client_ip,
                    details={
                        "signal": "inventory",
                        "disk_count": len(body.disks),
                    },
                )
                conn.commit()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no machine record for {normalised}",
                )
            _log_event(
                conn,
                kind="machine.inventory",
                summary=(
                    f"{normalised} reported {len(body.disks)} disk(s)"
                    + (" + lshw" if lshw_json is not None else "")
                    + (" (lshw too large, skipped)" if lshw_oversize else "")
                ),
                subject_kind="machine",
                subject_id=normalised,
                actor="pxe-client",
                source_ip=client_ip,
                details={
                    "count": len(body.disks),
                    "serials": [d.serial for d in body.disks if d.serial],
                    "lshw": lshw_json is not None,
                },
            )
            conn.commit()
        publish_state_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---------- release-fetch manager ---------------------------
    # Registered BEFORE the ``GET /boot/{name}`` catch-all so
    # ``/boot/releases`` doesn't get eaten as a missing artifact name.
    # Powers the trackable "Fetch from GitHub releases" action on
    # /ui/netboot: ``POST /boot/releases`` enqueues, ``GET /boot/releases``
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
    async def enqueue_release_fetch(
        body: _models.ReleaseFetchRequest, request: Request
    ) -> dict[str, Any]:
        state = await release_fetch_manager.enqueue(body.tag)
        # Lifecycle audit event: operator-initiated release fetch.
        # Pairs with the worker-side ``netboot.artifacts.fetch.started``
        # and the eventual terminal event.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="netboot.artifacts.fetch.requested",
                summary=f"operator requested release fetch for tag {body.tag!r}",
                subject_kind="netboot",
                subject_id=body.tag,
                actor="operator",
                source_ip=_client_ip(request),
                details={"tag": body.tag},
            )
            conn.commit()
        return state.to_dict()

    @app.delete("/boot/releases/{tag}", dependencies=[Depends(require_auth)])
    async def cancel_release_fetch(tag: str, request: Request) -> dict[str, Any]:
        try:
            state = await release_fetch_manager.cancel(tag)
        except ValueError as exc:
            # Symmetric with enqueue: a malformed tag (path traversal,
            # whitespace, escaped slashes) hits the same _TAG_RE guard
            # and surfaces as 422 here rather than the previous 404
            # plus an audit-log row carrying the attacker-controlled
            # text in subject_id.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active release fetch for tag {tag!r}",
            )
        # Operator-side cancel event. The worker writes its own
        # ``netboot.artifacts.fetch.cancelled`` when it observes the
        # flag; this row carries source_ip + actor=operator so the
        # /ui/events filter on actor=operator picks up the intent.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="netboot.artifacts.fetch.cancelled",
                summary=f"operator cancelled release fetch for tag {tag!r}",
                subject_kind="netboot",
                subject_id=tag,
                actor="operator",
                source_ip=_client_ip(request),
                details={"tag": tag, "source": "operator"},
            )
            conn.commit()
        return state.to_dict()

    # ---------- backups -----------------------------------------
    # Mirrors the /boot/releases shape (the only other worker-pool
    # manager left after the v0.40 catalog/download + hash cleanup):
    # GET lists active jobs (queued + running + recent terminal
    # states); POST enqueues; DELETE cancels by backup_id.
    # ``/ui/backups`` filters to queued + running only; terminal
    # rows evict from the UI on completion, and history lives in
    # the events log.

    @app.get("/workers/backups", dependencies=[Depends(require_auth)])
    async def list_backups() -> dict[str, Any]:
        states = await backup_manager.list()
        return {
            "backups_root": str(resolved_backups_root),
            "max_parallel": backup_manager.max_parallel,
            "backups": [s.to_dict() for s in states],
        }

    @app.post(
        "/workers/backups",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def enqueue_backup(
        body: _models.BackupEnqueueRequest, request: Request
    ) -> dict[str, Any]:
        state = await backup_manager.enqueue(trigger=body.trigger)
        # Lifecycle audit event: operator-initiated backup. Scheduler-
        # driven backups go through ``_backup.scheduler_loop`` which
        # emits its own request event with actor=system; this handler
        # is operator-only (the ``trigger`` field defaults to "manual"
        # but the scheduler uses the same enqueue path with
        # "scheduled" -- so we look at body.trigger to set the actor
        # correctly even though most callers will hit "manual" here).
        is_scheduler = body.trigger == "scheduled"
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="backup.create.requested",
                summary=(
                    f"scheduler requested {state.backup_id!r}"
                    if is_scheduler
                    else f"operator requested backup {state.backup_id!r}"
                ),
                subject_kind="backup",
                subject_id=state.backup_id,
                actor="system" if is_scheduler else "operator",
                source_ip=None if is_scheduler else _client_ip(request),
                details={"backup_id": state.backup_id, "trigger": body.trigger},
            )
            conn.commit()
        return state.to_dict()

    @app.delete("/workers/backups/{backup_id}", dependencies=[Depends(require_auth)])
    async def cancel_backup(backup_id: str, request: Request) -> dict[str, Any]:
        state = await backup_manager.cancel(backup_id)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active backup for id {backup_id!r}",
            )
        # Operator-side cancel event. Unlike the catalog/release
        # workers, the backup worker does NOT emit a sibling
        # backup.create.cancelled: a running backup completes in
        # milliseconds and isn't interrupted mid-flight, so this
        # handler's event is the only cancelled record.
        with _db.open_db(state_path) as conn:
            _log_event(
                conn,
                kind="backup.create.cancelled",
                summary=f"operator cancelled backup {backup_id!r}",
                subject_kind="backup",
                subject_id=backup_id,
                actor="operator",
                source_ip=_client_ip(request),
                details={"backup_id": backup_id, "source": "operator"},
            )
            conn.commit()
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

    @app.post("/events/{event_id}/ack", dependencies=[Depends(require_auth)])
    def acknowledge_event_endpoint(event_id: int) -> dict[str, Any]:
        """Mark one event acknowledged. Clears it from the dashboard
        Health Monitoring tripwire (which counts only unacknowledged
        failures) without deleting the audit row. 404 if no such id."""
        with _db.open_db(state_path) as conn:
            changed = _acknowledge_event(conn, event_id)
            conn.commit()
        if not changed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no event with id {event_id}",
            )
        return {"id": event_id, "acknowledged": True}

    def _arm_flasher_boot(raw_mac: str, client_ip: str | None) -> None:
        """Mark that ``raw_mac`` fetched a live-env artifact -- proof it
        actually booted the live env, which is stronger evidence than
        the ``/pxe`` config GET (that only means "we told it to boot").
        One-shot state transition for the bit-consuming policies: the
        next ``GET /pxe/{mac}`` reads the bit and serves a sanboot of
        the local disk instead of re-running the live-env boot.
        ``bty-flash-always`` uses it to boot the just-flashed disk
        once before alternating back to flash; ``bty-flash-once`` keeps
        it set as the terminal state (re-armed only when the operator
        re-saves the machine); ``bty-inventory`` uses it to boot the
        disk after re-collecting inventory. The WHERE clause confines
        arming to those three policies so the bit's lifecycle can't
        leak into others (a typo'd or stale ``?mac=`` is a no-op on
        sanboot / bty-tui).

        last_seen_at + last_seen_ip get touched too: a /boot fetch
        is a live-env heartbeat, and an operator looking at
        /ui/machines should see the boot-time contact reflected
        even if no /pxe call lands between the chain and the live
        env's eventual /done or /inventory POST.
        """
        try:
            mac = _normalise_mac(raw_mac)
        except HTTPException:
            return  # malformed ?mac= -- ignore, just serve the file
        now = _now_iso()
        with _db.open_db(state_path) as conn:
            # Restrict the saw_flasher_boot WRITE to the 0->1
            # transition so an idempotent re-arm (the live env pulls
            # kernel + initrd + squashfs in one boot -> three /boot
            # fetches -> three arm calls) doesn't spam the audit log.
            # rowcount == 1 iff the bit actually transitioned this
            # call. The last_seen_* updates ARE unconditional via a
            # separate UPDATE so every fetch refreshes the heartbeat
            # regardless of bit state.
            conn.execute(
                "UPDATE machines SET last_seen_at = ?, last_seen_ip = ? WHERE mac = ?",
                (now, client_ip, mac),
            )
            cur = conn.execute(
                """
                UPDATE machines
                SET saw_flasher_boot = 1, updated_at = ?
                WHERE mac = ?
                  AND boot_mode IN (
                    'bty-flash-always', 'bty-flash-once', 'bty-inventory'
                  )
                  AND saw_flasher_boot = 0
                """,
                (now, mac),
            )
            if cur.rowcount > 0:
                # log the 0->1 transition so operators see
                # "iPXE chain pulled the kernel" in the audit timeline
                # without correlating /boot fetches to /pxe contacts.
                # Combined with the v0.33.22 state-label honesty fix,
                # an operator can now distinguish:
                #   * pre-arm (state=pending) - box hasn't PXE-booted
                #   * armed but no completion (state=live env running) -
                #     iPXE fetched the kernel; live env in flight
                #   * armed + completion (state=inventoried/flashed) -
                #     the live env actually finished its job
                # without needing to know about saw_flasher_boot at all.
                _log_event(
                    conn,
                    kind="netboot.flasher.armed",
                    summary=(
                        f"{mac} fetched a /boot artifact: saw_flasher_boot armed; live env booting"
                    ),
                    subject_kind="machine",
                    subject_id=mac,
                    actor="pxe-client",
                )
            conn.commit()

    @app.api_route(
        "/boot/{name}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    def boot_artifact(name: str, request: Request) -> FileResponse:
        # Live-env artifacts (kernel + initrd + squashfs) the iPXE chain
        # references PLUS the HTTP-Boot bootfile (ipxe.efi) UEFI
        # HTTPClient targets fetch directly via DHCP option 67 URL.
        # Open route: PXE clients have no token. HEAD is included
        # because some UEFI HTTP-Boot firmware HEADs the URL first
        # to size the fetch buffer before issuing the GET; Starlette's
        # FileResponse handles the HEAD shape (200 + Content-Length,
        # empty body) automatically.
        #
        # The flash chain tags these URLs with ``?mac=<MAC>`` (the
        # server already knows the MAC when it renders ipxe_flash.j2,
        # so it's free to embed). A fetch here is therefore proof the
        # machine booted the flasher -- arm the bty-flash-always
        # one-shot sanboot off it. HEADs arm too: a HEAD still means
        # the firmware committed to fetching this artifact.
        raw_mac = request.query_params.get("mac")
        if raw_mac:
            _arm_flasher_boot(raw_mac, _client_ip(request))
        return _serve_safe_file(resolved_boot_root, name)

    def _flash_target_for_ref(ref: str) -> str | None:
        """Resolve a ``bty_image_ref`` to a display name for the iPXE
        flash template's URL emit step (still emitted as a stable
        identifier for the live env to look up, even though the bytes
        path no longer goes through bty-web).

        Returns the entry's ``name`` (preserves format-by-extension
        on the live-env side) or ``None`` for an orphaned binding
        (no catalog row matches this ref).
        """
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT name FROM catalog_entries WHERE bty_image_ref = ?",
                (ref,),
            ).fetchone()
        if row is None:
            return None
        return str(row["name"])

    # The ``/images/{key}[/{name}]`` oras-fallback stream-proxy was
    # removed in v0.60.0. Its historical role was "let bty-web do the
    # OCI manifest dance for the live env on cold withcache"; both
    # backstops are gone now -- withcache 0.6.0 is oras-aware on the
    # cache-host side, and the live env's bty TUI handles ``oras://``
    # itself via ``withcache.oras`` (resolve + bearer + curl) when it
    # gets the raw URL from the plan endpoint. The plan endpoint
    # therefore ships the ``oras://`` src directly on cold / no-cache
    # paths now; see ``pxe_plan`` above.

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
            _machine_html, _images_html = render_dashboard_panels()
            yield sse_format("dashboard-machine", _machine_html)
            yield sse_format("dashboard-images", _images_html)
            async for event in event_bus.subscribe():
                # Filter out worker-state events: the machines-stream
                # client only cares about machine + dashboard fragments.
                # Worker events are served on /events/workers.
                if event.name == WORKER_STATE_CHANGED:
                    continue
                yield sse_format(event.name, event.html)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get(
        "/events/workers",
        dependencies=[Depends(require_auth)],
        include_in_schema=False,
    )
    async def events_workers() -> StreamingResponse:
        """Server-sent worker state-change stream.

        Each ``worker-state-changed`` event carries a JSON payload --
        ``{"kind": "<backup|hash|download|release>", "key": "...",
        "status": "queued|running|completed|cancelled|failed"}`` --
        emitted by every observable state transition in the four
        worker managers. The client uses it as a push-driven "refresh
        your table" signal; the JSON endpoints under ``/workers/...``
        / ``/catalog/...`` / ``/boot/...`` remain the authoritative
        state read.
        """

        async def stream() -> AsyncIterator[bytes]:
            async for event in event_bus.subscribe():
                if event.name != WORKER_STATE_CHANGED:
                    continue
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
            # Batch-fetch labels for every MAC in a single query. The
            # alternative (a per-row ``get_labels`` call) is N+1 SELECTs.
            label_map: dict[str, list[str]] = {}
            for r in conn.execute(
                "SELECT mac, label FROM machine_labels ORDER BY mac, label"
            ).fetchall():
                label_map.setdefault(r["mac"], []).append(r["label"])
        return [_row_to_machine(r, label_map.get(r["mac"], [])) for r in rows]

    @app.get(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_auth)],
    )
    def get_machine(mac: str) -> _models.Machine:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            labels = _labels.get_labels(conn, normalised) if row is not None else []
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        return _row_to_machine(row, labels)

    @app.get(
        "/machines/{mac}/lshw.json",
        dependencies=[Depends(require_auth)],
    )
    def get_machine_lshw(mac: str) -> Response:
        """Raw ``lshw -json`` blob the live env last reported for this
        MAC, served verbatim for other tools to consume. 404 if the
        machine has never posted lshw (e.g. only ever sanbooted, or the
        live env's ``lshw`` failed)."""
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT hw_lshw FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        blob = row["hw_lshw"]
        if not blob:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no lshw hardware inventory for {normalised}",
            )
        # Colons are invalid in filenames on Windows (and awkward on
        # every OS), so the download name uses the hyphen-separated MAC.
        mac_fname = normalised.replace(":", "-")
        return Response(
            content=blob,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{mac_fname}-lshw.json"'},
        )

    @app.get(
        "/machines/{mac}/disks.json",
        dependencies=[Depends(require_auth)],
    )
    def get_machine_disks(mac: str) -> Response:
        """The lsblk-derived disk inventory (``known_disks``) the live
        env last reported for this MAC, served verbatim for other tools.
        404 if the machine has never posted an inventory."""
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT known_disks FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no machine record for {normalised}",
            )
        blob = row["known_disks"]
        if not blob:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no disk inventory for {normalised}",
            )
        mac_fname = normalised.replace(":", "-")
        return Response(
            content=blob,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{mac_fname}-disks.json"'},
        )

    @app.put(
        "/machines/{mac}",
        response_model=_models.Machine,
        dependencies=[Depends(require_auth)],
    )
    def upsert_machine(mac: str, body: _models.MachineUpsert, request: Request) -> _models.Machine:
        normalised = _normalise_mac(mac)
        now = _now_iso()
        # Ramboot bind-time gate: the bound ref must already be
        # registered with nbdmux at ``status='ready'``. nbdmux owns
        # the warming pipeline since v0.2.0; bty just validates. An
        # operator who has not yet populated nbdmux gets a 422 here
        # rather than a silent "would-boot-but-misconfigured" record
        # that surfaces only at PXE chain time.
        if body.boot_mode == "ramboot" and body.bty_image_ref:
            with _db.open_db(state_path) as _conn:
                nbdmux_url = _settings_store.resolve_nbdmux_url(_conn)
            if not nbdmux_url:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "ramboot: nbdmux URL not configured "
                        "(Settings -> Ramboot, or [nbdmux] url in bty.toml)"
                    ),
                )
            try:
                exports = nbdmux_client.list_exports(server=nbdmux_url, timeout=2.0)
            except nbdmux_client.NbdmuxError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"ramboot: nbdmux unreachable: {exc}",
                ) from exc
            if not any(
                e.get("name") == body.bty_image_ref and e.get("status") == "ready" for e in exports
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"ramboot: ref {body.bty_image_ref[:8]}... is not "
                        f"registered with nbdmux at status='ready'. "
                        f"Populate it via nbdmux's dashboard at "
                        f"{nbdmux_url}/ (POST /exports with src_url) first."
                    ),
                )
        with _db.open_db(state_path) as conn:
            existing = conn.execute(
                "SELECT created_at FROM machines WHERE mac = ?", (normalised,)
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            conn.execute(
                """
                INSERT INTO machines
                    (mac, bty_image_ref, boot_mode, sanboot_drive,
                     target_disk_serial, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    bty_image_ref      = excluded.bty_image_ref,
                    boot_mode          = excluded.boot_mode,
                    sanboot_drive      = excluded.sanboot_drive,
                    target_disk_serial = excluded.target_disk_serial,
                    -- reset the one-shot alternation bit
                    -- ONLY when a policy-affecting field changes.
                    -- Pre-v0.33.22 the reset fired on every upsert,
                    -- so an operator renaming a box mid-cycle (or
                    -- tweaking sanboot_drive) silently interrupted
                    -- the in-flight flash / inventory cycle. The
                    -- three fields that DO require a reset:
                    --
                    --   * boot_mode -- the intent changed; the
                    --     current cycle no longer applies.
                    --   * bty_image_ref -- the bound image changed;
                    --     a sanboot of the disk that holds the OLD
                    --     image would be wrong on the next contact.
                    --   * target_disk_serial -- the target changed;
                    --     same reason, the cycle's identity moved.
                    --
                    -- labels / sanboot_drive are display + boot
                    -- modifiers that don't invalidate the cycle.
                    -- saw_flasher_boot resets on any of the three
                    -- policy-affecting changes; same CASE expression
                    -- mirrored across last_flashed_at + known_disks_at
                    -- below because those completion signals belong
                    -- to the OLD cycle. Pre-fix, an operator rebinding
                    -- a flashed machine (e.g. flash-once -> ipxe-exit
                    -- -> flash-once for a fresh flash) left
                    -- last_flashed_at intact -- so a future crashed
                    -- flasher cycle that armed the bit but never
                    -- /done'd would still see has_flashed=True and
                    -- the /pxe consume would boot the disk (via
                    -- ipxe-exit) half-flashed. Clearing the completion
                    -- signal on policy change closes that hole.
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
                    body.bty_image_ref,
                    body.boot_mode,
                    body.sanboot_drive,
                    body.target_disk_serial,
                    created_at,
                    now,
                ),
            )
            _labels.set_labels(conn, normalised, body.labels)
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
                    "boot_mode": body.boot_mode,
                    "sanboot_drive": body.sanboot_drive,
                    "labels": list(body.labels),
                    "target_disk_serial": body.target_disk_serial,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            labels = _labels.get_labels(conn, normalised) if row is not None else []
        assert row is not None
        publish_state_changed()
        return _row_to_machine(row, labels)

    @app.delete(
        "/machines/{mac}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_auth)],
    )
    def delete_machine(mac: str, request: Request) -> Response:
        normalised = _normalise_mac(mac)
        with _db.open_db(state_path) as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (normalised,))
            # sqlite isn't running with FK enforcement, so the
            # cascade to ``machine_labels`` is explicit.
            if cur.rowcount > 0:
                _labels.delete_labels(conn, normalised)
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
        publish_state_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/images", response_model=list[_models.ImageEntry])
    def list_images_endpoint(request: Request) -> list[_models.ImageEntry]:
        """Unified catalog listing.

        Each entry carries a single ``url``: server URL for
        cached / imported / dir-scan-with-sidecar images,
        upstream URL for manifest entries that have not been
        cached yet. The client just flashes from ``url`` --
        no need to know about manifests, sidecars, or cache.

        Open route: the PXE-booted ``bty`` flow needs to enumerate
        the catalog without first bootstrapping auth. The
        byte-serving route ``GET /images/{name}`` is already
        open. Same homelab-network trust model as /pxe / /boot.
        """
        unified = _list_unified_images()
        origin = _request_origin(request)
        out: list[_models.ImageEntry] = []
        for u in unified:
            if u.cached:
                # Local file or cached manifest blob -- bty-web
                # serves the bytes. URL shape is
                # ``/images/<sha>/<name>``: the SHA binds the
                # content; the trailing name is decorative so a
                # client that derives format from URL filename
                # extension (``bty.flash.probe_image_url``) gets
                # ``foo.img.zst`` instead of a bare 64-hex digest.
                # The server route ignores ``<name>`` for the
                # lookup.
                if u.sha256 is None:
                    continue  # cached + no sha is impossible; defensive
                # URL-encode the trailing name. Catalog ``name`` is
                # human text -- ``nosi fedora-sysdev (x86_64, rolling)``
                # is real -- and Python's ``http.client._validate_path``
                # rejects any URL path that contains a space or
                # control character (CVE-2019-9740 mitigation), so
                # an unencoded space here makes a downstream
                # ``urllib.request.urlopen`` from ``bty`` raise
                # ``InvalidURL`` before the request ever leaves
                # the box. ``safe=""`` percent-encodes
                # every special character (parens, spaces, etc.)
                # so the URL is reliably valid; the server route
                # is ``GET /images/{key}/{name:path}`` and only
                # reads ``key`` for resolution, so the encoded
                # form is purely decorative on the wire.
                encoded_name = urllib.parse.quote(u.names[0], safe="")
                url = f"{origin}/images/{u.sha256}/{encoded_name}"
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
                    ref=u.ref,
                    sha_short=u.sha256[:12] if u.sha256 else None,
                    cached=u.cached,
                    arch=u.arch,
                )
            )
        return out

    @app.get("/catalog.toml", response_class=PlainTextResponse)
    def list_catalog_toml(request: Request) -> Response:
        """Serve the unified image catalog as a TOML manifest matching
        the ``bty.catalog.Catalog`` schema (``version=1``, ``[[images]]``
        entries with ``name``/``src``/``sha256``/``format``/``size_bytes``).

        Same set of rows as ``GET /images`` (manifest + dir-scan +
        operator-curated DB entries), but serialised so ``bty
        --catalog`` clients can consume it with the same code path
        they use for static files hosted on e.g. GitHub. Open route,
        same trust model as ``/images``.

        Contract: a catalog manifest carries only REMOTE srcs (the
        receiver can't resolve ``file://`` off the publisher's host).
        Locally-cached entries are rewritten to
        ``http://<this-server>/images/...``; dir-scan-only entries
        (file:// only, no sha + no upstream URL) are skipped. Any
        entry that would still leak a ``file://`` is dropped
        defensively below.

        Withcache rewrite: when a withcache URL is configured the
        upstream branch rewrites EVERY entry's src (oras + https)
        to ``<withcache>/b/<b64(origin)>/<basename>``. Withcache
        0.6.0+ is oras-aware (it parses ``oras://...``, mints its
        own bearer, fetches from the registry), so the catalog the
        live env sees is uniform: every remote entry is an HTTPS URL
        on the LAN cache regardless of original scheme. Cold misses
        on withcache are absorbed by withcache's own Range-resume
        + auto-fetch (no bty-side proxy fallback once the catalog
        commits to withcache as the bytes-source).

        Implemented as ``application/toml`` so a curl-then-eyeball
        round-trip shows a human-readable manifest, not a binary blob.
        Entries without a sha256 are skipped (the catalog schema
        requires one); a future cache-hashing pass over dir-scan
        files will surface them.
        """
        unified = _list_unified_images()
        origin = _request_origin(request)
        with _db.open_db(state_path) as conn:
            withcache_url = _settings_store.resolve_withcache_url(conn)
        lines: list[str] = ["version = 1", ""]
        for u in unified:
            if u.sha256 is None:
                # Catalog manifest schema requires a sha; skip dir-scan
                # entries that haven't been hashed yet.
                continue
            if u.cached:
                # See the matching site in ``list_images_endpoint`` for
                # the rationale on percent-encoding the name segment.
                # Catalog ``name`` is human text and may contain
                # spaces / parens that would otherwise produce a URL
                # ``http.client`` rejects with InvalidURL.
                encoded_name = urllib.parse.quote(u.names[0], safe="")
                src = f"{origin}/images/{u.sha256}/{encoded_name}"
            else:
                upstream = next(
                    (s.location for s in u.sources if s.kind in ("manifest", "url")),
                    None,
                )
                if upstream is None:
                    continue
                src = upstream
                # Withcache canonicalisation: the live env sees the
                # same HTTPS URL shape (``<withcache>/b/<b64>/<name>``)
                # regardless of whether the original upstream is oras
                # or https. Withcache 0.6.0+ handles the OCI dance
                # internally on a cold miss.
                if withcache_url:
                    src = _withcache.blob_url(withcache_url, upstream)
            # Defense-in-depth: a catalog manifest never publishes
            # ``file://`` srcs (see the contract in the docstring).
            # The cached/upstream branches above shouldn't emit one,
            # but a future code path could regress; skip rather than
            # ship a manifest that won't parse on the receiver
            # (bty.catalog.load_bytes rejects file://).
            if src.startswith("file://"):
                continue
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
            if u.arch:
                lines.append(f'arch = "{u.arch}"')
            lines.append("")
        body = "\n".join(lines).rstrip() + "\n"
        return PlainTextResponse(content=body, media_type="application/toml")

    @app.put(
        "/boot/{name}",
        dependencies=[Depends(require_auth)],
        include_in_schema=False,
    )
    async def upload_boot_artifact(name: str, request: Request) -> dict[str, object]:
        """Stream-upload a live-env artifact into the boot dir.

        The live trio (vmlinuz / initrd / squashfs) lands here so the
        iPXE chain finds it via the open ``GET /boot/{name}`` route.
        Body is capped at ``cfg.tuning.max_upload_bytes`` and the name
        is checked against path traversal via ``_safe_path``.
        """
        return await _stream_upload(request, resolved_boot_root, name)

    # Browser UI under /ui/ (Jinja + Bootstrap, cookie-auth).

    def _load_db_catalog_entries() -> tuple[_catalog.CatalogEntry, ...]:
        """Load all rows from ``catalog_entries`` as :class:`CatalogEntry`
        records.

        Single shape regardless of whether ``disk_image_sha`` is set:
        ``CatalogEntry.sha256`` is either the observed content hash
        or ``None``. The downstream merge keys on both
        ``bty_image_ref`` (always derivable from ``src``) and on
        ``sha256`` (when known), so a row without content sha still
        collapses with the matching manifest entry that produced it.

        Previously this method split into sha-keyed CatalogEntry +
        url-only UnifiedImage records. The split caused the
        duplicate-rendering regression on /ui/images: every entry
        without a pinned sha appeared once in the merge's unhashed
        tail and once in the url-only verbatim tail. Folding both
        into one shape lets the merge dedupe by ref.

        ``ORDER BY added_at`` matches the ``list_catalog_entries``
        API endpoint so the UI's catalog table renders in the same
        insertion order regardless of which code path populated the
        page.
        """
        with _db.open_db(state_path) as conn:
            rows = conn.execute(
                "SELECT disk_image_sha, name, src, format, size_bytes, description "
                "FROM catalog_entries ORDER BY added_at"
            ).fetchall()
        return tuple(
            _catalog.CatalogEntry(
                name=row["name"],
                src=row["src"],
                sha256=row["disk_image_sha"],  # may be None
                format=row["format"],
                size_bytes=row["size_bytes"],
                description=row["description"],
                arch=images.detect_arch_from_name(row["name"]),
            )
            for row in rows
        )

    def _list_unified_images() -> list[images.UnifiedImage]:
        """Build the unified image listing from ``catalog_entries`` rows.

        v0.40: bty-web no longer owns image bytes; ``catalog_entries``
        is the only source of truth. Each row produces one
        :class:`UnifiedImage`. ``cached`` is always False -- bty-web
        doesn't track withcache's contents here; the live env / wizard
        flashes whichever URL the plan or catalog hands it.
        """
        out: list[images.UnifiedImage] = []
        for entry in _load_db_catalog_entries():
            ref = _catalog.image_ref_for_src(entry.src)
            source = images.ImageSource(kind="manifest", location=entry.src)
            out.append(
                images.UnifiedImage(
                    ref=ref,
                    sha256=entry.sha256,
                    names=(entry.name,),
                    format=entry.format,
                    size_bytes=entry.size_bytes,
                    sources=(source,),
                    cached=False,
                    arch=entry.arch,
                )
            )
        return out

    _ui.register_ui_routes(
        app,
        jinja=jinja,
        state_path=state_path,
        service_user=service_user,
        boot_root=resolved_boot_root,
        backups_root=resolved_backups_root,
        publish_state_changed=publish_state_changed,
        list_unified_images=_list_unified_images,
    )

    # ---------- operator-curated catalog entries -----------------------
    # ``catalog_entries`` table in state.db backs a UI form where the
    # operator pastes ``image-url`` + optional ``sha-url`` and hits
    # Add. The shape mirrors a catalog.toml manifest entry, so once
    # written the row appears on the operator's catalog page like any
    # other entry. No filesystem dance; no TOML editing.

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
                        "(bty_image_ref, src, resolved_src, disk_image_sha, name, sha_url, "
                        "format, size_bytes, description, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bty_image_ref,
                            body.image_url,
                            resolved.blob_url,
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
                    "(bty_image_ref, src, resolved_src, disk_image_sha, name, sha_url, "
                    "format, size_bytes, description, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bty_image_ref,
                        body.image_url,
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
        via the withcache warm-fetch path (oras + https) or bty-web's
        own ``/images/{ref}`` proxy on a cold cache.

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
        imported, skipped, errors = _import_parsed_catalog(
            parsed, source=source, source_ip=_client_ip(request)
        )
        return {
            "source": source,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }

    def _import_parsed_catalog(
        parsed: _catalog.Catalog,
        *,
        source: str,
        source_ip: str | None,
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
        now = _now_iso()
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
                        errors.append(
                            {"name": entry.name, "error": f"oras (kept without sha): {exc}"}
                        )
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
                    conn.execute(
                        "INSERT INTO catalog_entries "
                        "(bty_image_ref, src, resolved_src, disk_image_sha, name, sha_url, "
                        "format, size_bytes, description, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bty_image_ref,
                            entry.src,
                            resolved_src,
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

    # ---------- catalog download manager ----------------------------------
    # Authenticated endpoints; only operators logged into the bty-web
    # UI can enqueue / cancel fetches. Skipped silently when no
    # manifest is configured.

    # Runtime catalog reload helper. The upload + fetch-release
    # endpoints both end here: write the manifest file, restart the
    # download manager with the freshly-parsed catalog, then
    # propagate via ``catalog_state.catalog`` so every closure-
    # captured handler sees the new value on its next call.
    def _auto_import_manifest_rows(catalog: _catalog.Catalog) -> None:
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
        now = _now_iso()
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
        _auto_import_manifest_rows(new_catalog)
        publish_state_changed()

    # URL for "Fetch from bty project release" -- mirrors the
    # ``bty`` wizard's ``d`` keystroke (loads
    # ``releases/latest/download/catalog.toml`` from the bty repo).
    # The same release page hosts boot artifacts + catalog.toml, so the
    # netboot release repo and the catalog URL share one default; both
    # are operator-overridable via the Settings page (resolved per
    # request below from ``_settings_store`` so a change takes effect
    # without a restart).

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
        * Size cap: ``_CATALOG_UPLOAD_MAX_BYTES`` (1 MiB). A real
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
        content = await upload.read(_CATALOG_UPLOAD_MAX_BYTES + 1)
        if len(content) > _CATALOG_UPLOAD_MAX_BYTES:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    f"catalog upload exceeded {_CATALOG_UPLOAD_MAX_BYTES} bytes; "
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
        _import_parsed_catalog(parsed, source="<upload>", source_ip=_client_ip(request))
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
          ``_CATALOG_UPLOAD_MAX_BYTES`` before parse.
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
                body: bytes = resp.read(_CATALOG_UPLOAD_MAX_BYTES + 1)
                return body

        try:
            content = await asyncio.to_thread(_fetch_sync)
        except (urllib.error.URLError, TimeoutError) as exc:
            return RedirectResponse(
                "/ui/images?error=" + urllib.parse.quote(f"release fetch failed: {exc}", safe=""),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if len(content) > _CATALOG_UPLOAD_MAX_BYTES:
            return RedirectResponse(
                "/ui/images?error="
                + urllib.parse.quote(
                    f"fetched catalog exceeded {_CATALOG_UPLOAD_MAX_BYTES} bytes; "
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
        _import_parsed_catalog(parsed, source=catalog_url, source_ip=None)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(content)
        await _reload_catalog_from_disk()
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    return app


# ---------- helpers -----------------------------------------------------------


def _row_to_machine(row: sqlite3.Row, labels: list[str]) -> _models.Machine:
    """Decode a sqlite3.Row into a ``_models.Machine``.

    ``known_disks`` is stored as a JSON string in the column;
    deserialise it lazily here so callers don't have to juggle the
    text/list distinction. A None or unparseable column means "no
    inventory yet"; missing fields don't crash the model.

    ``labels`` is sourced from the ``machine_labels`` side-table by
    the caller; it's plumbed in rather than fetched here so the
    list endpoint can read them in one batch (a JOIN) instead of
    N+1 queries.
    """
    raw_disks = row["known_disks"]
    parsed_disks: list[dict[str, object]] | None = None
    if raw_disks:
        try:
            decoded = json.loads(raw_disks)
            if isinstance(decoded, list):
                parsed_disks = decoded
        except (TypeError, ValueError):
            # Stale / malformed JSON in the column shouldn't crash
            # the listing endpoint; surface as "no inventory" and
            # the next /pxe/{mac}/inventory post replaces it cleanly.
            parsed_disks = None
    return _models.Machine(
        mac=row["mac"],
        bty_image_ref=row["bty_image_ref"],
        labels=labels,
        discovered_at=_iso_or_none(row["discovered_at"]),
        last_seen_at=_iso_or_none(row["last_seen_at"]),
        last_seen_ip=row["last_seen_ip"],
        boot_mode=row["boot_mode"],
        sanboot_drive=_db.row_value(row, "sanboot_drive"),
        last_flashed_at=_iso_or_none(row["last_flashed_at"]),
        known_disks=parsed_disks,
        known_disks_at=_iso_or_none(row["known_disks_at"]),
        target_disk_serial=row["target_disk_serial"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
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


def _request_host(request: Request) -> str:
    """Return the ``host:port`` the client used to reach this server.

    Prefers the ``Host`` header (what the client actually typed in the
    URL bar); falls back to the parsed request URL when the header is
    missing -- bare TestClient and tightly-curated reverse proxies can
    omit it. Default port mirrors the server's listen port.

    If both the Host header AND ``request.url.hostname`` are unset
    (synthetic Request constructed without scope, rare), returns a
    plausible loopback host instead of a string with ``"None"`` in
    it. The iPXE flash chain interpolates this value into a
    ``set bty-base http://{host}`` line, so a broken host would
    break the live env's HTTP fetches.
    """
    header_host = request.headers.get("host")
    if header_host:
        return header_host
    hostname = request.url.hostname or "127.0.0.1"
    port = request.url.port or 8080
    return f"{hostname}:{port}"


def _request_origin(request: Request) -> str:
    """Return the ``scheme://host:port`` origin the client used."""
    scheme = request.url.scheme or "http"
    return f"{scheme}://{_request_host(request)}"


def _seed_boot_dir(boot_root: Path) -> None:
    """Seed ``boot_root`` with baked bootstrap artifacts on startup.

    The container image bakes bty's custom iPXE binary (the one whose
    embedded script chains to ``/pxe-bootstrap.ipxe``, so the operator's
    DHCP only needs a single bootfile) under ``$BTY_BOOT_SEED_DIR``. Copy
    any file from there into ``boot_root`` when it isn't already present,
    so UEFI HTTP-Boot clients can fetch ``GET /boot/ipxe.efi`` out of the
    box.

    A no-op when ``BTY_BOOT_SEED_DIR`` is unset (host / dev installs) or
    its directory is absent. Existing files are never overwritten, so an
    operator-placed bootfile always wins.
    """
    import logging as _logging

    seed_dir = os.environ.get("BTY_BOOT_SEED_DIR")
    if not seed_dir:
        return
    src = Path(seed_dir)
    if not src.is_dir():
        return
    boot_root.mkdir(parents=True, exist_ok=True)
    seed_log = _logging.getLogger(__name__)
    for item in sorted(src.iterdir()):
        # Skip dotfiles so a ``.gitkeep`` placeholder in an
        # otherwise-empty seed dir (dev builds) isn't published.
        if item.name.startswith(".") or not item.is_file():
            continue
        dst = boot_root / item.name
        if dst.exists():
            continue
        try:
            shutil.copy2(item, dst)
            seed_log.info("seeded boot artifact %s into %s", item.name, boot_root)
        except OSError as exc:
            seed_log.warning("could not seed boot artifact %s: %s", item.name, exc)


def _safe_path(root: Path, name: str) -> Path:
    """Resolve ``root / name`` with path-traversal checks, return the path.

    Rejects names with slashes, ``..``, NULs, etc. Caller decides
    what to do with the resolved path (404 vs. open-for-write).
    """
    # Single-source the "is this a bare basename?" rule via _security;
    # keep this endpoint's own wording so the message stays stable.
    try:
        _security.validate_basename(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid name {name!r}: must be a bare filename "
            "(no '/', '\\', '..', or NUL bytes)",
        ) from exc
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid name {name!r}: resolves outside the allowed directory",
        ) from exc
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
# anything useful". Operators can raise via ``BTY_TUNING_MAX_UPLOAD_BYTES``
# if they have a legitimate use case for bigger images.
_DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024 * 1024

# Hard cap for ``/ui/catalog/upload``. A catalog.toml is plain TOML
# (typically a few KB; a fleet manifest with hundreds of entries
# stays well under 100 KB). 1 MiB is generous enough to never block
# a legitimate manifest and small enough to reject the "wrong form
# target" case (operator dropped an ISO / image into the catalog
# form by mistake) before parsing it as text.
_CATALOG_UPLOAD_MAX_BYTES = 1 * 1024 * 1024


def _max_upload_bytes() -> int:
    """Resolve the upload size cap from ``[tuning] max_upload_bytes``
    (env override ``BTY_TUNING_MAX_UPLOAD_BYTES``) or the schema
    default. Non-positive values clamp to the default -- a
    pathological ``0`` would otherwise reject every upload."""
    from bty.web._config import cfg as _cfg

    value = _cfg().tuning.max_upload_bytes
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
    default; ``BTY_TUNING_MAX_UPLOAD_BYTES`` overrides). A runaway script
    or hostile request that streams forever otherwise fills the
    image-root partition; the cap kills the upload + unlinks the
    partial well before that. The partial is also unlinked
    upfront so a prior aborted upload doesn't leak.
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
                                f"(BTY_TUNING_MAX_UPLOAD_BYTES). Aborted at {size} bytes."
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
