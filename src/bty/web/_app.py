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
import html
import json
import os
import re
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
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

import bty
from bty import catalog as _catalog
from bty import images
from bty import oras as _oras
from bty.web import _backup, _db, _hash, _models, _release_mgr, _settings_store, _ui
from bty.web import _catalog as _web_catalog
from bty.web._auth import SESSION_COOKIE, require_auth
from bty.web._events import MachineEvent, MachineEventBus, sse_format
from bty.web._events_log import acknowledge_event as _acknowledge_event
from bty.web._events_log import list_events as _list_events
from bty.web._events_log import normalize_ip as _normalize_ip
from bty.web._events_log import record as _log_event

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
    # Scheduled + on-demand backups land under ``backups/`` next to
    # state.db so they survive the same migrate-the-state-dir flow as
    # the image cache. Operators wanting them off the OS disk override
    # via ``BTY_BACKUP_DIR``.
    resolved_backups_root: Path = Path(
        os.environ.get("BTY_BACKUP_DIR") or (state_path.parent / "backups")
    )
    event_bus = MachineEventBus()

    # Optional catalog file + cache + DownloadManager. If no
    # catalog is configured (operator hasn't authored one), the
    # ``DownloadManager`` simply isn't started and the
    # ``/catalog/downloads/*`` + ``/catalog/hashes/*`` endpoints
    # return 404. Operator-curated entries (``POST /catalog/entries``)
    # work independently of the catalog. ``BTY_CATALOG_FILE`` and
    # ``BTY_CATALOG_CACHE_DIR`` override the defaults derived from
    # ``BTY_STATE_DIR``.
    #
    # The catalog path is treated as the "always this file" location
    # so the UI can write a fresh ``catalog.toml`` to it and reload
    # in-process. When ``$BTY_CATALOG_FILE`` is unset and the default
    # file doesn't exist yet, we still pin the default path so a
    # ``/ui/catalog/upload`` upload knows where to land.
    manifest_path = _catalog.default_manifest_path()
    if manifest_path is None:
        state_dir = Path(os.environ.get("BTY_STATE_DIR", "/var/lib/bty"))
        manifest_path = state_dir / "catalog.toml"
    catalog_cache_dir = _catalog.default_cache_dir()

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
    download_manager = _web_catalog.DownloadManager()
    hash_manager = _hash.HashManager()
    release_fetch_manager = _release_mgr.ReleaseFetchManager()
    backup_manager = _backup.BackupManager()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The SSE event bus accepts publishes from worker threads -
        # capture the loop now so cross-thread publishes can hop in
        # via call_soon_threadsafe.
        event_bus.attach(asyncio.get_running_loop())
        if catalog_state.catalog is not None:
            download_manager.start(
                catalog_state.catalog,
                catalog_cache_dir,
                state_path=state_path,
            )
        # The hash manager always starts -- it operates on
        # ``image_root``, which exists for every bty-web shape
        # (appliance, container, dev). Default parallelism is 1
        # so a Pi-class box doesn't get hammered if multiple big
        # images need importing at once.
        hash_manager.start(resolved_image_root, state_path=state_path)
        # Release-fetch manager: powers the trackable
        # /boot/releases endpoints (and the /ui/netboot page's
        # progress + cancel buttons). Default parallelism is 1
        # because fetching two GitHub releases in parallel is
        # operator-confusing and bandwidth-saturating.
        release_fetch_manager.start(resolved_boot_root, state_path=state_path)
        # Backup manager: powers ``/workers/backups`` + the Backup
        # tab's "Back up now" button. Wraps ``_portability.export_bundle``
        # so a scheduled / on-demand backup ships the same operator-
        # owned bundle the ``bty-web export`` CLI does.
        backup_manager.start(
            state_path,
            resolved_image_root,
            resolved_backups_root,
            bty_version=bty.__version__,
        )
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
        # And the symmetric path for manifest entries: a catalog.toml
        # that survived a restart (operator uploaded it, then bty-web
        # restarted) needs its entries reflected in catalog_entries
        # so the /ui/machines/{mac} dropdown shows them.
        if catalog_state.catalog is not None:
            _auto_import_manifest_rows(catalog_state.catalog)
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
            # Wake every SSE subscribe() generator so the
            # StreamingResponse exits its yield loop. Without this,
            # browser tabs left open on /ui/machines or /ui/dashboard
            # hold the HTTP connection alive until uvicorn's 90s
            # graceful-shutdown timeout SIGKILLs the worker.
            await event_bus.close()
            backup_stop_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_scheduler_task
            if catalog_state.catalog is not None:
                await download_manager.stop()
            await hash_manager.stop()
            await release_fetch_manager.stop()
            await backup_manager.stop()

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
    # Expose the running bty-web version as a Jinja global so the iPXE
    # templates can construct versioned /boot/<name> URLs without each
    # render-site repeating the context plumbing. The netboot fetcher
    # writes files with this same version into BTY_BOOT_DIR.
    jinja.globals["bty_version"] = bty.__version__

    def _fmt_ts(value: object) -> str:
        """Render a timestamp compactly as ``YYYY-MM-DD HH:MM:SS``.

        The on-disk shape (``_now_iso``) is
        ``2026-05-17T20:21:09.155109+00:00`` -- microseconds and the
        ``+00:00`` offset are noise for an operator scanning a row, so
        this trims to second precision and drops the offset. The single
        display format used everywhere a timestamp is shown.

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
        # All bty timestamps are written UTC; normalise any attached
        # offset to UTC, then drop tzinfo for the bare-second display.
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

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

    def _boot_state(m: object) -> str:
        """Lifecycle state for a machine -- the 'where in the cycle' half
        of the mode/state model, derived from boot_mode + the
        ``saw_flasher_boot`` bit (no stored state column). Empty for the
        non-alternating modes (ipxe-exit, bty-tui). The mode is the
        operator's intent; this is the transient position within it.
        """
        try:
            mode = m["boot_mode"]  # type: ignore[index]
            armed = bool(m["saw_flasher_boot"])  # type: ignore[index]
        except (KeyError, TypeError, IndexError):
            return ""
        if mode == "bty-flash-once":
            return "flashed; booting disk" if armed else "pending flash"
        if mode == "bty-flash-always":
            return "flashed; booting disk" if armed else "ready to flash"
        if mode == "bty-inventory":
            return "inventoried; booting disk" if armed else "pending inventory"
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
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            if row is None:
                # Auto-discovery: record an unassigned machine so the
                # operator can see this MAC in /machines and decide
                # what to do with it. ``boot_mode='bty-inventory'``
                # makes the unknown MAC chain into the live env to self-
                # report its disks, then sanboot -- so a new box auto-
                # collects its inventory and just boots, with no prior
                # server-side configuration. The operator then assigns a
                # flash policy from the now-populated disk dropdown.
                conn.execute(
                    """
                    INSERT INTO machines
                        (mac, boot_mode,
                         discovered_at, last_seen_at, last_seen_ip,
                         created_at, updated_at)
                    VALUES (?, 'bty-inventory', ?, ?, ?, ?, ?)
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
        publish_state_changed()
        # Boot-mode decision tree (highest priority first):
        #   - bty-tui                       -> live env, interactive wizard
        #   - sanboot                       -> iPXE sanboot the local disk
        #   - bty-flash-always / -once + ref + target disk -> live env auto-flash
        #     EXCEPT bty-flash-always with saw_flasher_boot set -> sanboot
        #     the just-flashed disk once (one-shot loop-break, see below)
        #   - else (no usable binding / stale policy) -> ipxe_unknown.j2
        #     (sanboot 0x80 || exit)
        # Completion signal (POST /pxe/{mac}/done) updates
        # last_flashed_at always, and flips bty-flash-once -> sanboot so
        # the box boots its freshly-flashed disk and stops reflashing.
        # bty-flash-always never changes policy; instead it alternates
        # flash-chain -> sanboot -> flash-chain across boots so the
        # just-flashed disk actually boots once before the next reflash.
        # The flip is driven by ``saw_flasher_boot``: armed when the box
        # fetches a /boot artifact (proof it booted the flasher),
        # consumed here on the following /pxe contact. Without this,
        # PXE-first firmware would reflash on every reboot forever.
        host = _request_host(request)
        policy = machine.get("boot_mode")
        ref = machine.get("bty_image_ref")

        # First decide the offer (template + summary + details).
        # This keeps the "what did we hand out" decision in one
        # place so the per-hit event log mirrors the actual render.
        rendered: str
        offer_kind: str
        offer_summary: str
        offer_details: dict[str, Any]

        if policy == "bty-tui":
            template = jinja.get_template("ipxe_tui.j2")
            rendered = template.render(mac=normalised, machine=machine, host=host)
            offer_kind = "bty-tui"
            offer_summary = f"{normalised} offered tui (operator picks via bty on tty1)"
            offer_details = {"offer": "bty-tui"}
        elif policy == "bty-inventory":
            # Inventory-then-sanboot, alternating like bty-flash-always
            # (same saw_flasher_boot bit). When the box has just booted
            # the live env (bit armed via GET /boot/...?mac=), serve a
            # sanboot of its disk and clear the bit; otherwise serve the
            # live-env chain so ``bty`` re-collects + posts inventory and
            # reboots (plan mode=inventory). Net: every power cycle
            # refreshes the inventory before booting the disk, so swapped
            # hardware is discovered.
            if machine.get("saw_flasher_boot"):
                drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
                template = jinja.get_template("ipxe_sanboot.j2")
                rendered = template.render(
                    mac=normalised, machine=machine, drive=drive, policy=policy
                )
                with _db.open_db(state_path) as conn:
                    conn.execute(
                        "UPDATE machines SET saw_flasher_boot = 0, updated_at = ? WHERE mac = ?",
                        (now, normalised),
                    )
                    conn.commit()
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
                if policy in ("bty-flash-always", "bty-flash-once") and machine.get(
                    "saw_flasher_boot"
                ):
                    # The box fetched a /boot artifact with ``?mac=`` since
                    # we served the flash chain -- proof it booted the
                    # flasher, flashed, and rebooted back. Boot the
                    # freshly-flashed disk via sanboot instead of
                    # reflashing. The bit handling is what makes the two
                    # modes differ (this is the "state" half of the
                    # mode/state model):
                    #   * bty-flash-always: CLEAR the bit, so the next real
                    #     netboot (no artifact fetch in between) flips back
                    #     to the flash chain -- the flash<->sanboot
                    #     alternation that stops a PXE-first reflash loop.
                    #   * bty-flash-once: KEEP the bit. This is terminal:
                    #     the box sanboots its disk from now on. The mode
                    #     STAYS bty-flash-once (no mutation to sanboot); it
                    #     re-arms only when the operator re-saves the
                    #     machine (which resets the bit).
                    drive = machine.get("sanboot_drive") or _models.DEFAULT_SANBOOT_DRIVE
                    template = jinja.get_template("ipxe_sanboot.j2")
                    rendered = template.render(
                        mac=normalised, machine=machine, drive=drive, policy=policy
                    )
                    if policy == "bty-flash-always":
                        with _db.open_db(state_path) as conn:
                            conn.execute(
                                "UPDATE machines SET saw_flasher_boot = 0, updated_at = ? "
                                "WHERE mac = ?",
                                (now, normalised),
                            )
                            conn.commit()
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
                # picked. Fall back to ipxe.j2 (exit to firmware) and
                # log a distinct event so the operator can tell this
                # case apart from "orphan ref / no bindable image".
                with _db.open_db(state_path) as conn:
                    _log_event(
                        conn,
                        kind="netboot.pxe.flash.no_target_disk",
                        summary=(
                            f"machine {normalised}: boot_mode={policy} but no "
                            "target_disk_serial picked; refusing flash chain"
                        ),
                        subject_kind="machine",
                        subject_id=normalised,
                        actor="pxe-client",
                        source_ip=client_ip,
                        details={
                            "bty_image_ref": ref,
                            "image_name": image_name,
                            "boot_mode": policy,
                        },
                    )
                    conn.commit()
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
                # (exit to firmware), but the operator sees a louder
                # event so the "boot_mode=bty-flash-always but ref is
                # dangling" case doesn't look like a normal hit.
                short = str(ref)[:12]
                with _db.open_db(state_path) as conn:
                    _log_event(
                        conn,
                        kind="netboot.pxe.flash.orphan_ref",
                        summary=f"machine {normalised} bound to ref={short}...: no catalog row",
                        subject_kind="machine",
                        subject_id=normalised,
                        actor="pxe-client",
                        source_ip=client_ip,
                        details={"bty_image_ref": ref},
                    )
                    conn.commit()
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

        # Audit every PXE hit. Cheap (one INSERT) and gives the
        # operator a full timeline of "client X showed up, server
        # offered Y". The events table is append-only with no
        # retention cap today; long-running per-job CI loops will
        # grow it indefinitely. If that becomes a problem, the
        # ``netboot.pxe.offered`` kind is a natural candidate for a
        # subject-id-keyed rolling-window prune ("keep the last 100
        # per MAC").
        with _db.open_db(state_path) as conn:
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
        "/pxe/{mac}/done",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def pxe_done(mac: str, request: Request) -> Response:
        # Open route: the live env hits this from the PXE-booted target,
        # which has no token. Trust model: bty-web is for trusted
        # networks (homelab / CI), not the open internet - same as the
        # other ``/pxe/*`` endpoints.
        #
        # Records ``last_flashed_at`` + ``updated_at``. It does NOT mutate
        # ``boot_mode``: the mode is the operator's intent and stays put.
        # The post-flash "boot the disk" behaviour comes from the
        # ``saw_flasher_boot`` bit instead -- armed when the box fetched
        # the flasher's /boot artifacts, it makes the next /pxe contact
        # sanboot the freshly-flashed disk. For bty-flash-once that's
        # terminal (the bit stays set, so it keeps booting the disk); for
        # bty-flash-always the /pxe handler clears it to re-arm the next
        # reflash. This is the mode/state split: mode = intent (here),
        # state = the bit. (Pre-mode/state, this flipped flash-once ->
        # sanboot, which lied about the operator's configured mode.)
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
        publish_state_changed()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/pxe/{mac}/plan")
    def pxe_plan(mac: str, request: Request) -> dict[str, Any]:
        """Return the per-MAC plan as JSON so ``bty --server X --mac
        Y`` can dispatch without operator input.

        Plan shapes (mode is the dispatch token):

        * ``{"mode": "flash", "image": URL, "target_disk_serial": S}``
          -- boot_mode in (bty-flash-always, bty-flash-once) with a bindable ref
          AND a target_disk_serial picked. ``bty`` runs the flash
          without prompts.
        * ``{"mode": "interactive", "catalog": URL}`` -- boot_mode
          ``tui``, OR a flash policy that can't be auto-resolved
          (no target serial, orphan ref). ``bty`` drops the operator
          into the wizard with the server's catalog pre-loaded.
        * ``{"mode": "exit"}`` -- boot_mode=sanboot (handled at the
          iPXE layer, so the box doesn't reach the live env) or any
          unrecognised policy. ``bty`` exits cleanly; the firmware /
          sanboot path handles boot.

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
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
            if row is None:
                # Auto-discovery: mirror /pxe/{mac}'s behaviour so a
                # ``bty`` invocation against an unknown MAC creates a
                # record (boot_mode=bty-inventory) instead of
                # 404-ing.
                conn.execute(
                    """
                    INSERT INTO machines
                        (mac, boot_mode,
                         discovered_at, last_seen_at, last_seen_ip,
                         created_at, updated_at)
                    VALUES (?, 'bty-inventory', ?, ?, ?, ?, ?)
                    """,
                    (normalised, now, now, client_ip, now, now),
                )
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
        publish_state_changed()

        host = _request_host(request)
        base = f"http://{host}"
        policy = machine.get("boot_mode")
        ref = machine.get("bty_image_ref")

        plan: dict[str, Any]
        offer_kind: str
        if policy in ("bty-flash-always", "bty-flash-once") and ref:
            target_disk_serial = machine.get("target_disk_serial")
            image_name = _flash_target_for_ref(str(ref))
            if image_name is not None and target_disk_serial:
                fmt = _flash_format_for_ref(str(ref))
                # The client detects image format from the URL name's
                # extension. An oras title ("nosi fedora-sysdev (x86_64,
                # rolling)") has none, so the flash gets rejected as
                # "format not recognised". When the catalog name carries
                # no usable extension, synthesise a filename from the
                # stored format so even older clients detect it; the
                # ``{ref}`` segment is what actually resolves the bytes,
                # so the name is free to change.
                if fmt and images.detect_format(Path(image_name)) is None:
                    url_name = f"image.{fmt}"
                else:
                    url_name = image_name
                image_name_encoded = urllib.parse.quote(url_name, safe="")
                plan = {
                    "mode": "flash",
                    "image": f"{base}/images/{ref}/{image_name_encoded}",
                    "target_disk_serial": str(target_disk_serial),
                    # Descriptive catalog name for display: the image URL's
                    # last segment may be a synthesised "image.<fmt>" (so
                    # the client can detect format), which is uninformative
                    # on the flash screen. ``name`` carries the real title.
                    "name": image_name,
                }
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
            # boot_mode=sanboot (or any other / missing) -- ``bty``
            # has nothing to do (sanboot is handled at the iPXE layer,
            # the box never chains into the live env); plan mode=exit
            # means "exit cleanly, let firmware / sanboot handle boot".
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
                details={"plan": plan, "boot_mode": policy, "offer_kind": offer_kind},
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
            cur = conn.execute(
                "UPDATE machines SET known_disks = ?, known_disks_at = ?, "
                "hw_lshw = COALESCE(?, hw_lshw), "
                "hw_lshw_at = CASE WHEN ? IS NOT NULL THEN ? ELSE hw_lshw_at END, "
                "updated_at = ? WHERE mac = ?",
                (disks_json, now, lshw_json, lshw_json, now, now, normalised),
            )
            if cur.rowcount == 0:
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

    # ---------- backups -----------------------------------------
    # Mirrors the /catalog/downloads + /catalog/hashes + /boot/releases
    # shape: GET lists the active jobs (queued + running + recent
    # terminal states, same as the other managers' raw list); POST
    # enqueues; DELETE cancels by backup_id. The workers page in the
    # UI filters to queued + running only -- terminal rows evict
    # from the UI on completion, and history lives in the events log.

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
    async def enqueue_backup(body: _models.BackupEnqueueRequest) -> dict[str, Any]:
        state = await backup_manager.enqueue(trigger=body.trigger)
        return state.to_dict()

    @app.delete("/workers/backups/{backup_id}", dependencies=[Depends(require_auth)])
    async def cancel_backup(backup_id: str) -> dict[str, Any]:
        state = await backup_manager.cancel(backup_id)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no active backup for id {backup_id!r}",
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

    @app.post("/events/{event_id}/ack", dependencies=[Depends(require_auth)])
    def acknowledge_event_endpoint(event_id: int) -> dict[str, Any]:
        """Mark one event acknowledged. Clears it from the dashboard
        Health Monitoring tripwire (which counts only unacknowledged
        failures) without deleting the audit row. 404 if no such id."""
        with _db.open_db(state_path) as conn:
            changed = _acknowledge_event(conn, event_id)
            conn.commit()
        if not changed:
            raise HTTPException(status_code=404, detail=f"no event with id {event_id}")
        return {"id": event_id, "acknowledged": True}

    def _arm_flasher_boot(raw_mac: str) -> None:
        """Mark that ``raw_mac`` fetched a live-env artifact -- proof it
        actually booted the live env, which is stronger evidence than
        the ``/pxe`` config GET (that only means "we told it to boot").
        One-shot state transition for the alternating policies: the next
        ``GET /pxe/{mac}`` consumes the bit and serves a sanboot of the
        local disk instead of re-running the live-env boot.
        ``bty-flash-always`` uses it to boot the just-flashed disk;
        ``bty-inventory`` to boot the disk after re-collecting
        inventory. The WHERE clause confines arming to those two
        policies so the bit's lifecycle can't leak into others (a
        typo'd or stale ``?mac=`` is a no-op)."""
        try:
            mac = _normalise_mac(raw_mac)
        except HTTPException:
            return  # malformed ?mac= -- ignore, just serve the file
        with _db.open_db(state_path) as conn:
            conn.execute(
                """
                UPDATE machines
                SET saw_flasher_boot = 1, updated_at = ?
                WHERE mac = ? AND boot_mode IN ('bty-flash-always', 'bty-inventory')
                """,
                (_now_iso(), mac),
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
            _arm_flasher_boot(raw_mac)
        return _serve_safe_file(resolved_boot_root, name)

    def _serve_image_by_key(key: str, request: Request) -> Response:
        """Resolve ``key`` (filename OR 64-hex ID) to bytes.

        Resolution order:

          1. Literal filename under ``image_root`` -- the bare
             ``GET /images/<name>`` form for scripts / curl-based
             operators.
          2. 64-hex ID through :func:`_resolve_image_for_key` -> a local
             file (image store, or an explicit-Download cache hit).
          3. 64-hex ref/sha of a REMOTE catalog entry not cached locally
             -> stream the source bytes straight through (no cache, no
             buffer-then-serve): GET pipes the chunks, HEAD returns the
             source's size. So a flashing client gets bytes immediately
             and a large image never times out the probe.
        """
        try:
            return _serve_safe_file(resolved_image_root, key)
        except HTTPException:
            pass
        if images.is_sha256_hex(key.lower()):
            resolved_path = _resolve_image_for_key(key)
            if resolved_path is not None:
                return FileResponse(resolved_path, media_type="application/octet-stream")
            src = _remote_src_for_key(key)
            if src is not None:
                return _stream_remote_image(src, request)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such image: {key}",
        )

    def _remote_src_for_key(key: str) -> str | None:
        """The remote (oras:// / http(s)://) src for a 64-hex key
        (bty_image_ref or disk_image_sha), or ``None`` if there's no
        catalog row or its src is local (file://)."""
        key_lower = key.lower()
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT src FROM catalog_entries WHERE bty_image_ref = ? OR disk_image_sha = ?",
                (key_lower, key_lower),
            ).fetchone()
        if row is None:
            return None
        src = str(row["src"])
        return src if src.startswith(("oras://", "http://", "https://")) else None

    def _stream_remote_image(src: str, request: Request) -> Response:
        """Proxy a remote image straight through to the client, no cache.

        HEAD resolves just the size (source HEAD / oras manifest); GET
        pipes the source bytes as they arrive. A source that can't be
        reached surfaces as 502 (the live env shows it on tty1) rather
        than hanging.
        """
        if request.method == "HEAD":
            from bty import flash as _flash

            try:
                info = _flash.probe_image_url(src)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"image source not reachable: {src} ({exc})",
                ) from exc
            headers = {"Content-Length": str(info.size_bytes)} if info.size_bytes else {}
            return Response(headers=headers, media_type="application/octet-stream")
        try:
            chunks, total = _catalog.stream_src(src)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"image source not reachable: {src} ({exc})",
            ) from exc
        headers = {"Content-Length": str(total)} if total else {}
        return StreamingResponse(chunks, media_type="application/octet-stream", headers=headers)

    def _flash_target_for_ref(ref: str) -> str | None:
        """Resolve a ``bty_image_ref`` to a display name so the iPXE
        flash template can build the ``/images/<ref>/<name>`` URL.

        Returns the entry's ``name`` (preserves format-by-extension
        on the live-env side) or ``None`` for an orphaned binding (no
        catalog row matches this ref). The URL uses the ref itself,
        not the content sha. The serve path returns a local file when
        present and otherwise stream-proxies the remote source, so the
        URL works whether or not the image has been downloaded yet.
        """
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT name FROM catalog_entries WHERE bty_image_ref = ?",
                (ref,),
            ).fetchone()
        if row is None:
            return None
        return str(row["name"])

    def _flash_format_for_ref(ref: str) -> str | None:
        """The catalog entry's stored ``format`` for a ref, or None.

        The flash plan passes this to the client: the image URL's name
        segment can be a descriptive title (e.g. an oras image's
        ``nosi fedora-sysdev (x86_64, rolling)``) with no file
        extension, so the client can't detect the format from the URL
        alone and would reject the flash as "format not recognised".
        """
        with _db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT format FROM catalog_entries WHERE bty_image_ref = ?",
                (ref,),
            ).fetchone()
        return str(row["format"]) if row and row["format"] else None

    def _resolve_image_for_key(key: str) -> Path | None:
        """Resolve a 64-hex key (bty_image_ref or disk_image_sha) to a
        local file path that already exists, or ``None``. Never fetches
        here: returns an image-store file or an explicit-Download cache
        hit if present; otherwise ``None`` and the serve path
        stream-proxies the remote source instead.

        Resolution order:

        1. ``key`` as ``bty_image_ref``: look up catalog_entries.
           - disk_image_sha known + cache file present -> cache path
           - src is file:// + file exists -> local (HashManager will
             populate disk_image_sha asynchronously)
           - else (remote src, not yet cached locally): ``None`` -> the
             serve path stream-proxies the source (no cache). An explicit
             Download is what caches a remote image to disk.
        2. ``key`` as raw ``disk_image_sha``: serves the sha-keyed
           URLs that the ``GET /images`` listing emits for entries
           whose content hash is known. Looks for the cache file
           first, then falls back to the catalog_entries row's
           ``file://`` src.
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
            sha: str | None = row["disk_image_sha"]
            src = str(row["src"])
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
            # Remote src (oras:// / http(s)://) with no local cache: NOT
            # served. The transparent serve-time cache-through was
            # dropped -- it download-then-served the whole image with no
            # dedup, so a large oras image thrashed (concurrent fetches
            # never completing) and the client's probe always timed out
            # before the bytes were ready. Remote images are now brought
            # local *deliberately* (the Downloads action, or dropping a
            # file into the image store) before they're flashable; until
            # then this resolves to a clean 404 the live env surfaces on
            # tty1, instead of a multi-minute hang.
            return None
        # (2) sha lookup -- serves the sha-keyed URLs emitted by the
        # ``GET /images`` listing for entries whose content hash is
        # known. Resolve via the catalog_entries row's src.
        if images.is_sha256_hex(key_lower):
            cached = catalog_cache_dir / key_lower
            if cached.is_file():
                return cached
            with _db.open_db(state_path) as conn:
                row = conn.execute(
                    "SELECT src FROM catalog_entries WHERE disk_image_sha = ?",
                    (key_lower,),
                ).fetchone()
            if row is not None:
                src = str(row["src"])
                if src.startswith("file://"):
                    rel = src[len("file://") :]
                    local = resolved_image_root / rel
                    if local.is_file():
                        return local
        return None

    @app.api_route(
        "/images/{key}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    def serve_image(key: str, request: Request) -> Response:
        """Serve image bytes by filename OR by SHA-256.

        Same trust model as /boot. The live env curls this to
        get the bytes that ``bty.image_url`` points at.

        HEAD is accepted alongside GET because
        ``bty.flash.probe_image_url`` HEADs the URL before
        streaming to learn Content-Length without downloading
        the bytes. Without HEAD support, the server returns
        405 Method Not Allowed; ``bty`` catches that as
        ``URLError`` and surfaces "image URL not reachable" --
        which obscures the actual cause (HEAD blocked).
        Starlette's FileResponse handles HEAD shape (200 +
        Content-Length, empty body) automatically.
        """
        return _serve_image_by_key(key, request)

    @app.api_route(
        "/images/{key}/{name:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    def serve_image_with_name(key: str, name: str, request: Request) -> Response:
        """``/images/<sha>/<filename>`` form. The ``key`` (SHA-256)
        binds the bytes; ``name`` is purely decorative -- it lets
        clients that derive image format from URL filename
        extension (``bty.flash.probe_image_url``) keep working
        when bty-web emits SHA-keyed URLs. The server ignores
        ``name`` for the actual lookup; it's there so
        ``Path(url.path).name`` returns ``foo.img.zst`` instead
        of a bare 64-hex SHA.

        HEAD support: see the sibling ``/images/{key}`` route
        for the rationale.
        """
        del name  # informational only; lookup is by ``key``
        return _serve_image_by_key(key, request)

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
                    -- Reset the one-shot alternation bit: an operator
                    -- reconfiguring a machine starts a fresh cycle, so a
                    -- stale arming (e.g. left over from a prior
                    -- bty-flash-always/bty-inventory boot) can't make the
                    -- next /pxe wrongly sanboot instead of flashing /
                    -- inventorying.
                    saw_flasher_boot   = 0,
                    updated_at         = excluded.updated_at
                """,
                (
                    normalised,
                    body.bty_image_ref,
                    body.hostname,
                    body.boot_mode,
                    body.sanboot_drive,
                    body.target_disk_serial,
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
                    "boot_mode": body.boot_mode,
                    "sanboot_drive": body.sanboot_drive,
                    "hostname": body.hostname,
                    "target_disk_serial": body.target_disk_serial,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (normalised,)).fetchone()
        assert row is not None
        publish_state_changed()
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
        Cached entries are rewritten to ``http://<this-server>/images/...``
        so they're reachable; dir-scan-only entries (file:// only, no
        sha + no upstream URL) are skipped. Any entry that would still
        leak a ``file://`` is dropped defensively below.

        Implemented as ``application/toml`` so a curl-then-eyeball
        round-trip shows a human-readable manifest, not a binary blob.
        Entries without a sha256 are skipped (the catalog schema
        requires one); a future cache-hashing pass over dir-scan
        files will surface them.
        """
        unified = _list_unified_images()
        origin = _request_origin(request)
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
            # ``_stream_upload`` already raises on write failure, so
            # FileNotFoundError shouldn't reach here -- suppressed
            # defensively for robustness against a transient unlink.
            with contextlib.suppress(FileNotFoundError):
                await hash_manager.enqueue(name)
            # Insert/refresh the ``catalog_entries`` row for this
            # file so the operator can bind a machine by its
            # ``bty_image_ref`` immediately, without waiting for a
            # bty-web restart's lifespan sweep. Idempotent
            # (``INSERT OR IGNORE`` + ``COALESCE`` UPDATE).
            _auto_import_dir_scan_rows()
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
        """Stream-upload a live-env artifact into the boot dir.

        Same shape as ``PUT /images/{name}`` - the live trio
        (vmlinuz / initrd / squashfs) goes here so the iPXE chain
        finds it via the open ``GET /boot/{name}`` route.
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
            )
            for row in rows
        )

    def _list_unified_images() -> list[images.UnifiedImage]:
        """Unified image listing: dir-scan files + operator-curated
        ``catalog_entries`` rows + content-cache, folded through the
        same ref-keyed + sha-keyed merge.

        The ``catalog_entries`` DB table is the authoritative
        catalog. ``catalog.toml`` is treated as an import seed
        (``_auto_import_manifest_rows`` at startup, and again after
        a UI upload / fetch-release reload), not a live overlay
        whose deletions get re-injected -- so operator removals via
        ``DELETE /catalog/entries`` stick across renders + restarts.
        Re-importing the catalog is an explicit operator action
        (``POST /catalog/import`` or the UI's catalog upload form).

        Recomputed per call so an operator who drops new files into
        BTY_IMAGE_ROOT (or whose catalog fetch just completed, or
        who added a URL via the UI) sees the change on the next
        page load without restarting bty-web.
        """
        db_entries = _load_db_catalog_entries()
        return images.merge_with_catalog(
            resolved_image_root,
            db_entries,
            catalog_cache_dir,
        )

    _ui.register_ui_routes(
        app,
        jinja=jinja,
        state_path=state_path,
        service_user=service_user,
        image_root=resolved_image_root,
        boot_root=resolved_boot_root,
        backups_root=resolved_backups_root,
        publish_state_changed=publish_state_changed,
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
        ``oras://``, the server runs ``bty.oras.resolve_ref`` at add
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
                if sha is None and entry.src.startswith("oras://"):
                    # Best-effort oras resolution: try to pin sha + size
                    # from the registry manifest. On failure (offline /
                    # registry unreachable / private registry needing
                    # auth) we still insert the entry, just without
                    # the sha+size pre-filled. The row is bindable via
                    # ``bty_image_ref`` even without sha, and the first
                    # cache-fetch will populate ``disk_image_sha`` then.
                    # Strict-fail mode would refuse offline imports
                    # which is operator-hostile for sealed environments.
                    try:
                        resolved = _oras.resolve_ref(entry.src)
                    except _oras.OrasError as exc:
                        errors.append(
                            {"name": entry.name, "error": f"oras (kept without sha): {exc}"}
                        )
                    else:
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
        """Re-read ``manifest_path`` and restart the DownloadManager.

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
        # Tear the old manager down first so its in-flight downloads
        # don't bleed into the new manager's queue with stale entry
        # references.
        if catalog_state.catalog is not None:
            await download_manager.stop()
        catalog_state.catalog = new_catalog
        _auto_import_manifest_rows(new_catalog)
        download_manager.start(new_catalog, catalog_cache_dir, state_path=state_path)
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
        # and reload the in-process catalog so the DownloadManager
        # binds to it -- without that step the "Fetch" buttons on
        # the resulting rows fall through to ``/catalog/downloads``
        # and get a 404 "no catalog manifest configured".
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
        try:
            with urllib.request.urlopen(catalog_url, timeout=30) as resp:
                # Bound the read at the catalog upload cap + 1 byte
                # so a release page that responds with a huge
                # unexpected body (HTML, a binary asset that
                # somehow got the catalog.toml URL pointed at it)
                # can't OOM the worker.
                content = resp.read(_CATALOG_UPLOAD_MAX_BYTES + 1)
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
        # the bytes to ``manifest_path`` + reload, so the
        # DownloadManager binds and the "Fetch" buttons on the
        # imported rows actually work. Without the write+reload,
        # ``POST /catalog/downloads`` 404s with "no catalog
        # configured" right after a successful import.
        _import_parsed_catalog(parsed, source=catalog_url, source_ip=None)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(content)
        await _reload_catalog_from_disk()
        return RedirectResponse("/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/catalog/downloads")
    async def list_downloads(_: str = Depends(require_auth)) -> dict[str, Any]:
        if catalog_state.catalog is None:
            return {"catalog": None, "downloads": []}
        states = await download_manager.list()
        return {
            "catalog": str(manifest_path),
            "cache_dir": str(catalog_cache_dir),
            "max_parallel": download_manager.max_parallel,
            "downloads": [s.to_dict() for s in states],
        }

    @app.post("/catalog/downloads", status_code=status.HTTP_202_ACCEPTED)
    async def enqueue_download(
        body: _models.CatalogEnqueueRequest,
        _: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if catalog_state.catalog is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no catalog configured",
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
        if catalog_state.catalog is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no catalog configured",
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
        if sha is None and catalog_state.catalog is not None:
            entry = catalog_state.catalog.by_name(name)
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


def _row_to_machine(row: sqlite3.Row) -> _models.Machine:
    """Decode a sqlite3.Row into a ``_models.Machine``.

    ``known_disks`` is stored as a JSON string in the column;
    deserialise it lazily here so callers don't have to juggle the
    text/list distinction. A None or unparseable column means "no
    inventory yet"; missing fields don't crash the model.
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
        hostname=row["hostname"],
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
    omit it. Default port mirrors the appliance's listen port.

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
    what to do with the resolved path (404 vs. open-for-write).
    """
    if not name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid name {name!r}: must be a bare filename "
            "(no '/', '\\', '..', or NUL bytes)",
        )
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
# anything useful". Operators can raise via ``BTY_MAX_UPLOAD_BYTES``
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
