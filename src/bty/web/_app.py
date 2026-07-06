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
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from nbdmux import client as nbdmux_client
from starlette.middleware.sessions import SessionMiddleware

import bty
from bty import images
from bty.web import (
    _backup,
    _db,
    _labels,
    _models,
    _ramboot,
    _release_mgr,
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
from bty.web._events_log import record as _log_event
from bty.web._helpers import (
    boot_state,
    cached_display_tz,
    now_iso,
    request_host,
    row_to_machine,
    seed_boot_dir,
    serve_safe_file,
    stream_upload,
)
from bty.web._reqctx import client_ip as _client_ip
from bty.web._reqctx import normalise_mac as _normalise_mac
from bty.web._routes_backups import register_backup_routes
from bty.web._routes_events import register_event_routes
from bty.web._routes_releases import register_release_routes
from bty.web._withcache_catalog import WithcacheCatalog as _WithcacheCatalog

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
        seed_boot_dir(resolved_boot_root)
        release_fetch_manager.start(resolved_boot_root, state_path=state_path)
        # Backup manager: powers ``/workers/backups`` + the Backup
        # tab's "Back up now" button. Wraps ``_portability.export_bundle``
        # so a scheduled / on-demand backup ships the same operator-
        # owned bundle the ``bty-web export`` CLI does.
        backup_manager.start(
            state_path,
            resolved_backups_root,
        )
        # Prime the withcache-catalog cache. Since withcache 0.9.1 the
        # catalog is single-sourced from withcache; the ``GET /catalog``
        # roundtrip populates ``app.state.withcache_catalog`` so the
        # first render doesn't have to wait for the operator to click
        # Refresh. Silent no-op when the withcache URL isn't
        # configured yet (fresh deploy pre-Settings-save).
        with _db.open_db(state_path) as _wc_conn:
            _wc_url = _settings_store.resolve_withcache_url(_wc_conn)
        if _wc_url:
            _app.state.withcache_catalog.set_withcache_url(_wc_url)
            try:
                _app.state.withcache_catalog.refresh()
            except Exception as exc:
                _lifespan_log.warning("withcache catalog refresh at startup failed: %s", exc)
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

        The on-disk shape (``now_iso``) is
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
        tz = cached_display_tz(state_path)
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

    jinja.filters["boot_state"] = boot_state

    _db.init_db(state_path)

    app = FastAPI(title="bty-web", version=bty.__version__, lifespan=_lifespan)

    # Withcache-backed catalog cache. Since withcache 0.9.1 the catalog
    # is single-sourced from withcache; bty caches a snapshot in
    # memory here and every image-lookup + machine-binding read site
    # will hit this cache instead of a local ``catalog_entries``
    # table. Initialised empty because the withcache URL comes from
    # settings (which needs a DB open); the lifespan hook does the
    # first refresh once ``_settings_store.resolve_withcache_url`` is
    # callable.
    app.state.withcache_catalog = _WithcacheCatalog(withcache_url=None)

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

    def render_dashboard_machine_panel() -> str:
        """Render the LIVE Machine Summary dashboard panel as an SSE
        fragment. Built from the same ``_ui.dashboard_counts_context``
        the request-time dashboard render uses so counters can't
        drift between page load and live update."""
        with _db.open_db(state_path) as conn:
            ctx = _ui.dashboard_counts_context(conn, _list_unified_images())
        return jinja.get_template("ui/_dashboard_machine.html").render(**ctx)

    def publish_state_changed() -> None:
        """Publish fresh snapshots of every SSE-driven UI fragment.

        Mutating routes call this on commit. Subscribers receive all
        events on the same stream and route to elements with matching
        ``sse-swap`` attributes - the machines table swaps the
        ``machines-update`` event, the Machine Summary tile swaps
        the ``dashboard-machine`` event.
        """
        event_bus.publish(MachineEvent(name="machines-update", html=render_machines_tbody()))
        event_bus.publish(
            MachineEvent(name="dashboard-machine", html=render_dashboard_machine_panel())
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
        host = request_host(request)
        template = jinja.get_template("pxe_bootstrap.j2")
        return template.render(host=host)

    @app.get("/pxe/{mac}", response_class=PlainTextResponse)
    def pxe(mac: str, request: Request) -> str:
        normalised = _normalise_mac(mac)
        client_ip = _client_ip(request)
        now = now_iso()
        with _db.open_db(state_path) as conn:
            # Race-safe discovery. Two concurrent /pxe requests for
            # the same fresh MAC (iPXE retry, dnsmasq retransmit)
            # used to UNIQUE-violate the plain INSERT path; v0.33.6
            # moved to INSERT ... ON CONFLICT DO UPDATE ... RETURNING
            # which was race-safe on the row but had a subtle
            # discriminator race: the ``(created_at = ?) AS is_new``
            # synthetic column relied on timestamp comparison, so two
            # requests whose ``now_iso()`` happened to tie (possible
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
        host = request_host(request)
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
            # Inventory-then-ipxe-exit, alternating like bty-flash-always
            # (same saw_flasher_boot bit). When the box has booted the
            # live env (bit armed via GET /boot/...?mac=) AND the live
            # env actually POSTed inventory (``known_disks_at`` is set),
            # serve the ipxe-exit chain + clear the bit. Otherwise serve
            # the live-env chain.
            #
            # the bit ALONE used to gate the ipxe-exit serve.
            # If the live env crashed between fetching /boot and POSTing
            # /pxe/{mac}/inventory, the bit stayed armed and the server
            # served the ipxe-exit chain against an empty disk -- the box
            # failed to boot, cycled, the next /pxe cleared the bit, then
            # re-served the inventory chain. One wasted ipxe-exit cycle
            # per crashed inventory. Now: armed-without-known_disks_at is
            # treated as "live env didn't complete; retry the chain".
            # Self-healing without the wasted ipxe-exit cycle.
            armed = bool(machine.get("saw_flasher_boot"))
            has_inventory = bool(machine.get("known_disks_at"))
            if armed and has_inventory:
                drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
                template = jinja.get_template("ipxe_sanboot.j2")
                rendered = template.render(
                    mac=normalised, machine=machine, drive=drive, policy=policy
                )
                clear_saw_flasher_boot = True
                offer_kind = "bty-inventory-ipxe-exit"
                offer_summary = (
                    f"{normalised} booting disk (drive {drive}) after inventory; "
                    f"bty-inventory re-arms on next netboot"
                )
                offer_details = {
                    "offer": "ipxe-exit",
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
            # order. Checked before the generic ``ref`` branch so an
            # ipxe-exit machine with an image bound still boots the disk
            # rather than falling through to the ``exit`` (local) template.
            drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
            template = jinja.get_template("ipxe_sanboot.j2")
            rendered = template.render(mac=normalised, machine=machine, drive=drive, policy=policy)
            offer_kind = "ipxe-exit"
            offer_summary = f"{normalised} offered ipxe-exit (iPXE boots local drive {drive})"
            offer_details = {"offer": "ipxe-exit", "sanboot_drive": drive}
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
            # Look up the ready nbdmux export whose src_url matches
            # the bound catalog entry's src. Since PR #33 export
            # names are the URL basename (not the ref), so we key
            # on src_url; the resolved ``export_name`` is what the
            # initramfs asks nbd-server for.
            catalog_entry = request.app.state.withcache_catalog.get_by_ref(ref) if ref else None
            entry_src = (
                (catalog_entry.get("src") or catalog_entry.get("resolved_src"))
                if catalog_entry is not None
                else None
            )
            export_name: str | None = None
            if nbdmux_url and ref:
                for row in _ramboot.exports_by_src(nbdmux_url):
                    if row.get("status") != "ready":
                        continue
                    # Prefer matching by src_url (canonical since PR
                    # #33); fall back to name==ref for legacy exports
                    # still keyed on the ref.
                    if entry_src and row.get("src_url") == entry_src:
                        export_name = str(row.get("name") or "") or None
                        break
                    if row.get("name") == ref:
                        export_name = ref
                        break
            ramboot_ready = export_name is not None
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
                    export_name=export_name,
                    overlay_size=overlay_size,
                )
                offer_kind = "ramboot"
                offer_summary = (
                    f"{normalised} offered ramboot via nbd://{nbd_host}:10809/{export_name}"
                )
                offer_details = {
                    "offer": "ramboot",
                    "nbd_endpoint": f"tcp://{nbd_host}:10809",
                    "image_ref": ref,
                    "export_name": export_name,
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
                elif catalog_entry is None:
                    reason = "catalog entry not in withcache"
                else:
                    reason = "no ready nbdmux export for this src"
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
            # (ipxe-exit fallback) here makes the misconfiguration
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
                    # Serve the ipxe-exit chain against the just-flashed
                    # disk. The bit handling is what makes the two modes
                    # differ:
                    #   * bty-flash-always: CLEAR the bit, so the next
                    #     real netboot flips back to the flash chain --
                    #     the flash<->ipxe-exit alternation that stops a
                    #     PXE-first reflash loop.
                    #   * bty-flash-once: KEEP the bit. Terminal state:
                    #     the box boots its disk (via ipxe-exit) from now
                    #     on. The mode STAYS bty-flash-once; re-arms only
                    #     when the operator re-saves the machine.
                    #
                    # armed-without-last_flashed_at used to also serve
                    # the ipxe-exit chain. That booted a half-flashed
                    # disk -- bty-flash-always recovered via the next
                    # cycle (wasted one ipxe-exit); bty-flash-once was
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
                    offer_kind = f"{policy}-ipxe-exit"
                    offer_summary = f"{normalised} booting just-flashed disk (drive {drive}); " + (
                        "bty-flash-always re-arms on next netboot"
                        if policy == "bty-flash-always"
                        else "bty-flash-once complete (stays on this disk)"
                    )
                    offer_details = {
                        "offer": "ipxe-exit",
                        "sanboot_drive": drive,
                        "ipxe_exit_after_flash": True,
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
            offer_summary = f"{normalised} offered ipxe-exit -- no bty_image_ref bound"
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
        now = now_iso()
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
        now = now_iso()
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

        host = request_host(request)
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
            # Look up the entry from the in-memory withcache-catalog
            # cache; withcache is the single source of truth for
            # what's flashable. ``withcache_url`` still comes from the
            # settings store (nbdmux + the flash-plan cache decision
            # both key on the URL, not on catalog membership).
            entry = app.state.withcache_catalog.get_by_ref(str(ref))
            image_name = entry.get("name") if entry else None
            fmt = entry.get("format") if entry else None
            src = entry.get("src") if entry else None
            resolved_src = entry.get("resolved_src") if entry else None
            # Content hash for on-wire verification. Distinct from
            # ``ref`` (= bty_image_ref = sha256 of the canonical URL,
            # an identifier, NOT the bytes). NULL when the entry was
            # imported without a known sha -> omitted below so the
            # live env flashes without verifying.
            disk_image_sha = entry.get("sha256") if entry else None
            # withcache's lookup keys on ``src`` only (``blob_url``
            # takes ``src``, never ``resolved_src``). ``resolved_src``
            # stays purely the no-withcache fallback below.
            with _db.open_db(state_path) as conn:
                withcache_url = (
                    _settings_store.resolve_withcache_url(conn)
                    if image_name is not None and target_disk_serial
                    else None
                )
            if image_name is not None and target_disk_serial:
                # Since withcache v0.11.0 an entry only appears in
                # ``WithcacheCatalog.entries`` when its bytes are on
                # disk in withcache. So if we have a catalog entry
                # and a configured withcache, we always route the
                # flash chain through withcache. No HEAD probe.
                # Without a withcache we fall back to the canonical
                # ``resolved_src`` (plain-HTTPS for http entries;
                # resolved blob URL for oras) or the original
                # ``src`` -- ``oras://`` URLs included, which the
                # live env's bty TUI handles via ``withcache.oras``
                # (resolve + bearer mint + curl in the same
                # process).
                is_oras = src is not None and src.startswith("oras://")
                image_url = src or ""
                if src is not None:
                    if withcache_url:
                        image_url = _withcache.blob_url(withcache_url, src)
                        cache_hit = True
                    elif resolved_src is not None:
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
            # saw_flasher_boot-armed /pxe contact, which serves the
            # ipxe-exit chain to boot the disk. (If the box never
            # armed the bit, it just re-collects
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
        now = now_iso()
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

    # Registered BEFORE the ``GET /boot/{name}`` catch-all so
    # ``/boot/releases`` doesn't get eaten as a missing artifact name.
    register_release_routes(
        app,
        release_fetch_manager=release_fetch_manager,
        resolved_boot_root=resolved_boot_root,
        state_path=state_path,
    )

    register_backup_routes(
        app,
        backup_manager=backup_manager,
        resolved_backups_root=resolved_backups_root,
        state_path=state_path,
    )

    # Slim audit log of operator + machine activity. Backs the
    # /ui/events page + per-subject embedded lists on
    # /ui/machines/{mac} and /ui/images.
    register_event_routes(app, state_path=state_path)

    def _arm_flasher_boot(raw_mac: str, client_ip: str | None) -> None:
        """Mark that ``raw_mac`` fetched a live-env artifact -- proof it
        actually booted the live env, which is stronger evidence than
        the ``/pxe`` config GET (that only means "we told it to boot").
        One-shot state transition for the bit-consuming policies: the
        next ``GET /pxe/{mac}`` reads the bit and serves the
        ``ipxe-exit`` chain (boots the local disk) instead of
        re-running the live-env boot.
        ``bty-flash-always`` uses it to boot the just-flashed disk
        once before alternating back to flash; ``bty-flash-once`` keeps
        it set as the terminal state (re-armed only when the operator
        re-saves the machine); ``bty-inventory`` uses it to boot the
        disk after re-collecting inventory. The WHERE clause confines
        arming to those three policies so the bit's lifecycle can't
        leak into others (a typo'd or stale ``?mac=`` is a no-op on
        ``ipxe-exit`` / ``bty-tui``).

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
        now = now_iso()
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
        # one-shot ipxe-exit chain off it. HEADs arm too: a HEAD still means
        # the firmware committed to fetching this artifact.
        raw_mac = request.query_params.get("mac")
        if raw_mac:
            _arm_flasher_boot(raw_mac, _client_ip(request))
        return serve_safe_file(resolved_boot_root, name)

    def _flash_target_for_ref(ref: str) -> str | None:
        """Resolve a ``bty_image_ref`` to a display name for the iPXE
        flash template's URL emit step (still emitted as a stable
        identifier for the live env to look up, even though the bytes
        path no longer goes through bty-web).

        Returns the entry's ``name`` (preserves format-by-extension
        on the live-env side) or ``None`` for an orphaned binding
        (no catalog row matches this ref).
        """
        entry = app.state.withcache_catalog.get_by_ref(ref)
        if entry is None:
            return None
        name = entry.get("name")
        return str(name) if name else None

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
            yield sse_format("dashboard-machine", render_dashboard_machine_panel())
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
        return [row_to_machine(r, label_map.get(r["mac"], [])) for r in rows]

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
        return row_to_machine(row, labels)

    @app.get(
        "/machines/{mac}/lshw.json",
        dependencies=[Depends(require_auth)],
    )
    def get_machine_lshw(mac: str) -> Response:
        """Raw ``lshw -json`` blob the live env last reported for this
        MAC, served verbatim for other tools to consume. 404 if the
        machine has never posted lshw (e.g. only ever booted the disk
        via ipxe-exit, or the live env's ``lshw`` failed)."""
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
        now = now_iso()
        # Look up the catalog entry once so both the ramboot gate
        # below and any later logic can inspect it. Since withcache
        # v0.11.0 GET /catalog returns ONLY downloaded entries, so
        # any entry that's in bty's cache is by definition
        # flash-ready; no separate downloaded_first gate needed.
        catalog_entry: dict[str, Any] | None = None
        if (
            body.boot_mode in ("bty-flash-always", "bty-flash-once", "ramboot")
            and body.bty_image_ref
        ):
            catalog_entry = request.app.state.withcache_catalog.get_by_ref(body.bty_image_ref)
        # Ramboot bind-time gate: also needs the entry registered as
        # an nbdmux export at status='ready'. nbdmux owns the
        # decompress + register pipeline; bty validates. Since PR
        # #33 the export is keyed on src_url (not the ref), so we
        # match by the entry's src rather than by ref. If the ref
        # isn't in the catalog, we still enforce the nbdmux-known
        # requirement using the ref-as-name legacy fallback so
        # tests that stage before seeding still exercise the check.
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
            entry_src = (
                catalog_entry.get("src") or catalog_entry.get("resolved_src")
                if catalog_entry is not None
                else None
            )

            def _export_matches(e: dict[str, Any]) -> bool:
                if e.get("status") != "ready":
                    return False
                # Prefer matching by src_url (canonical since PR #33).
                # Fall back to name==ref for legacy exports still
                # keyed on the ref.
                if entry_src and e.get("src_url") == entry_src:
                    return True
                return e.get("name") == body.bty_image_ref

            if not any(_export_matches(e) for e in exports):
                subject_desc = (
                    f"catalog entry {catalog_entry.get('name')!r}"
                    if catalog_entry is not None
                    else f"ref {body.bty_image_ref[:8]}..."
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"ramboot: {subject_desc} is not registered with nbdmux "
                        f"at status='ready'. Pick it in nbdmux's /ui/exports "
                        f"(or POST /exports to nbdmux at {nbdmux_url}/) before "
                        f"binding this machine to ramboot."
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
        return row_to_machine(row, labels)

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

    @app.post(
        "/admin/withcache/refresh",
        status_code=204,
        dependencies=[Depends(require_auth)],
    )
    def admin_withcache_refresh(request: Request) -> Response:
        """Re-poll the configured withcache and rebuild bty's
        in-memory catalog cache.

        Since v0.66.0 bty pulls the catalog from withcache at
        process start; a running bty won't automatically see
        entries added afterward. This admin endpoint lets an
        operator (or an integration test) force a refresh after a
        catalog change without restarting the process. No-op with
        a 204 when withcache isn't configured.
        """
        try:
            request.app.state.withcache_catalog.refresh()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"withcache refresh failed: {exc}",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/images", response_model=list[_models.ImageEntry])
    def list_images_endpoint(request: Request) -> list[_models.ImageEntry]:
        """Unified catalog listing.

        Each entry carries a single ``url`` -- the upstream location
        the client streams the image bytes from. Since v0.60.0 bty-web
        does not host image bytes: the withcache sidecar (or the raw
        origin) fulfils the fetch. The ``cached`` field on the entry
        is always False and remains only to preserve the existing
        response schema; a future minor may drop it.

        Open route: the PXE-booted ``bty`` flow needs to enumerate
        the catalog without first bootstrapping auth. Same
        homelab-network trust model as /pxe / /boot.
        """
        del request  # v0.60.0: URLs are always upstream; the request origin no longer matters here
        unified = _list_unified_images()
        out: list[_models.ImageEntry] = []
        for u in unified:
            # Every catalog row carries an upstream URL (manifest
            # or operator-curated). Skip anything that doesn't -- a
            # dir-scan-only entry with no source can't be flashed
            # over the wire.
            upstream = next(
                (s.location for s in u.sources if s.kind in ("manifest", "url")),
                None,
            )
            if upstream is None:
                continue
            out.append(
                _models.ImageEntry(
                    name=u.names[0],
                    format=u.format or "",
                    size_bytes=u.size_bytes or 0,
                    url=upstream,
                    ref=u.ref,
                    sha_short=u.sha256[:12] if u.sha256 else None,
                    cached=False,
                    arch=u.arch,
                )
            )
        return out

    @app.get("/catalog.toml", response_class=PlainTextResponse)
    def list_catalog_toml() -> Response:
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
        with _db.open_db(state_path) as conn:
            withcache_url = _settings_store.resolve_withcache_url(conn)
        lines: list[str] = ["version = 1", ""]
        for u in unified:
            if u.sha256 is None:
                # Catalog manifest schema requires a sha; skip dir-scan
                # entries that haven't been hashed yet.
                continue
            upstream = next(
                (s.location for s in u.sources if s.kind in ("manifest", "url")),
                None,
            )
            if upstream is None:
                continue
            # Withcache canonicalisation: the live env sees the
            # same HTTPS URL shape (``<withcache>/b/<b64>/<name>``)
            # regardless of whether the original upstream is oras
            # or https. Withcache 0.6.0+ handles the OCI dance
            # internally on a cold miss. Since v0.60.0 bty-web no
            # longer hosts image bytes itself; when no withcache is
            # configured we ship the raw upstream URL and let the
            # live env fetch it directly.
            src = _withcache.blob_url(withcache_url, upstream) if withcache_url else upstream
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
        is checked against path traversal via ``safe_path``.
        """
        return await stream_upload(request, resolved_boot_root, name)

    # Browser UI under /ui/ (Jinja + Bootstrap, cookie-auth).

    def _list_unified_images() -> list[images.UnifiedImage]:
        """Build the unified image listing from withcache's catalog.

        Since v0.66.0 bty consumes withcache's catalog directly (see
        :mod:`bty.web._withcache_catalog`); ``catalog_entries`` no
        longer exists. Each entry produces one
        :class:`UnifiedImage`. ``cached`` is always False -- bty-web
        doesn't track withcache's contents here; the live env / wizard
        flashes whichever URL the plan or catalog hands it.
        """
        out: list[images.UnifiedImage] = []
        for entry in app.state.withcache_catalog.entries:
            src = entry.get("src")
            ref = entry.get("bty_image_ref")
            if not src or not ref:
                continue
            name = entry.get("name") or ""
            source = images.ImageSource(kind="manifest", location=src)
            out.append(
                images.UnifiedImage(
                    ref=ref,
                    sha256=entry.get("sha256"),
                    names=(name,),
                    format=entry.get("format"),
                    size_bytes=entry.get("size_bytes"),
                    sources=(source,),
                    cached=False,
                    arch=entry.get("arch") or images.detect_arch_from_name(name),
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

    return app
