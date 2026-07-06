"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db``, the path the bty-web container and host
units expect).

Pre-1.0: the schema is whatever ``CREATE TABLE`` says here. There is
no migration apparatus. The DB carries the exact ``bty.__version__``
that created it in the ``bty_version`` table.

**Schema-mismatch behavior (v0.33.0+).** When ``init_db`` sees a
``state.db`` whose stored version disagrees with the running release
(or has data tables but no marker at all - a pre-versioning DB), it
**rotates** the old DB to ``state.db.<from>.<ts>.bak`` and creates a
fresh schema in its place. The old DB is preserved on disk for
forensics but the running server starts clean. A
``system.schema.reset`` event is recorded in the fresh DB so the
dashboard tripwire surfaces it; operators acknowledge from
``/ui/events``.

The earlier "refuse to start" + recovery-wizard approach (v0.31.x /
v0.32.x) was overengineered: ``state.db`` is regenerable
(bindings re-discover on next PXE contact, audit log is cosmetic,
settings are a tiny handful), and pre-1.0 explicitly says no
migration apparatus. Auto-rotation is the simplest correct
behavior. Image bytes are not on bty-web's disk anyway (v0.40
took bty-web out of the bytes plane; withcache caches blobs in
its own volume; oras streams from the registry), so a rotation
on the bty-web state.db cannot lose images.  Operators who want
hardware inventory preserved across upgrades use ``bty-web
export`` / ``bty-web import``.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import bty

DEFAULT_STATE_DIR = Path("/var/lib/bty")


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read ``key`` from a sqlite3.Row, returning ``default`` when the
    column is absent (an additive column on a row from a pre-migration
    or partial SELECT).

    Membership must go through ``.keys()``: ``key in row`` checks the
    Row's *values*, not its column names -- which is also why call
    sites otherwise need ``# noqa: SIM118``.
    """
    return row[key] if key in row.keys() else default  # noqa: SIM118


def default_state_path() -> Path:
    """Resolve ``state.db`` location from the active config
    (``cfg.paths.state_dir`` -- ``[paths] state_dir`` in bty.toml or
    ``BTY_PATHS_STATE_DIR`` env override; default
    :data:`DEFAULT_STATE_DIR`).

    Pre-bty.web-init callers (the rare paths that need a state path
    BEFORE main() has installed the active config) fall back to the
    env-var override or the built-in default. This keeps existing
    test scaffolds + the inventory-export CLI working without
    needing to first set up an active config."""
    try:
        from bty.web._config import cfg as _cfg

        return _cfg().state_db
    except RuntimeError:
        # No active config yet (very early startup, or a CLI subcommand
        # that bypasses the FastAPI app). Fall back to the env var.
        env = os.environ.get("BTY_PATHS_STATE_DIR")
        base = Path(env) if env else DEFAULT_STATE_DIR
        return base / "state.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac                       TEXT PRIMARY KEY,
    -- Binding target: ``WithcacheCatalog`` entry's
    -- ``bty_image_ref`` (sha256 of canonicalised src), not the
    -- content sha. Lets operators bind rolling-tag oras refs and
    -- URL-only entries that have no pre-known content sha.
    bty_image_ref             TEXT,
    discovered_at             TEXT,    -- first /pxe/{mac} contact (NULL if PUT-created)
    last_seen_at              TEXT,    -- most recent /pxe/{mac} contact
    last_seen_ip              TEXT,    -- source IP of most recent /pxe contact
    boot_mode               TEXT NOT NULL DEFAULT 'ipxe-exit',
    -- iPXE BIOS drive the ``ipxe-exit`` boot_mode boots on legacy
    -- BIOS (``0x80`` = first disk). NULL = use the default
    -- (``0x80``). Distinct from ``target_disk_serial``: iPXE picks
    -- local disks by BIOS drive number, not by the Linux serial the
    -- flash step matches. The ``sanboot`` in the column name is the
    -- underlying iPXE verb (still emitted by ``ipxe_sanboot.j2`` on
    -- BIOS); the boot-mode value was renamed to ``ipxe-exit`` in
    -- v0.25.0.
    sanboot_drive             TEXT,
    last_flashed_at           TEXT,    -- updated by POST /pxe/{mac}/done
    -- One-shot state bit for the ``bty-flash-always`` loop-break.
    -- Armed (1) when the machine fetches a flash-chain artifact with
    -- ``?mac=`` (GET /boot/...?mac=X) -- positive proof it booted the
    -- flasher. Consumed (back to 0) by the next ``GET /pxe/{mac}``,
    -- which serves a one-shot ``ipxe-exit`` chain of the just-flashed
    -- disk instead of reflashing; the next real netboot (no artifact
    -- fetch in between) flips back to the flash chain. Confined to
    -- bty-flash-always machines (only that policy arms it).
    saw_flasher_boot          INTEGER NOT NULL DEFAULT 0,
    -- Per-machine disk inventory, posted by ``bty`` on startup via
    -- POST /pxe/{mac}/inventory. JSON array of dicts:
    -- ``[{"path": "/dev/sda", "size": "...", "model": "...",
    --     "serial": "...", "tran": "sata", ...}, ...]``.
    -- The operator picks one of these by serial number; the chosen
    -- serial is what the live env consumes at flash time.
    known_disks               TEXT,    -- JSON array; NULL until first inventory
    known_disks_at            TEXT,    -- ISO timestamp of last inventory post
    -- Full ``lshw -json`` hardware tree (CPU / RAM / NICs+MACs /
    -- peripherals), posted alongside the disk inventory. Supplementary
    -- to known_disks; surfaced on the Machine view + a raw download
    -- (GET /machines/{mac}/lshw.json). NULL until a live-env boot
    -- posts it. The flasher never reads it.
    hw_lshw                   TEXT,    -- JSON blob; NULL until first inventory with lshw
    hw_lshw_at                TEXT,    -- ISO timestamp of last lshw post
    -- Operator-selected target disk SERIAL. Serial (vs path) is the
    -- durable identifier: ``/dev/sda`` can flip to ``/dev/nvme0n1``
    -- across kernel versions / udev rules, but the disk's serial
    -- number is fixed. ``bty`` in auto-flash mode matches the plan's
    -- target_disk_serial on this value; refuses to flash if the
    -- serial isn't found among the current disks (so a swapped-out
    -- drive doesn't get mis-flashed against a stale operator
    -- decision).
    target_disk_serial        TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

-- Operator-applied labels per machine. Plural by design: a box can
-- carry several tags at once ("rack-3", "noisy", "gmktec-g10"), so
-- filtering by any one of them surfaces every box that wears it.
-- Cosmetic; the boot chain never reads them. Replaced the singular
-- ``machines.hostname`` (RFC-1123-shaped) in v0.58.0.
--
-- Composite primary key prevents the same label being applied twice
-- to a MAC. Sqlite doesn't enforce foreign keys by default (no
-- ``PRAGMA foreign_keys=ON`` here), so the ``DELETE /machines/<mac>``
-- path is responsible for cleaning up label rows -- the assertion is
-- in tests so a future refactor that forgets it can't ship.
CREATE TABLE IF NOT EXISTS machine_labels (
    mac    TEXT NOT NULL,
    label  TEXT NOT NULL,
    PRIMARY KEY (mac, label)
);
CREATE INDEX IF NOT EXISTS machine_labels_label_idx ON machine_labels(label);

-- Slim audit log of operator + machine activity.
-- Append-only, queryable. Backs the /ui/events page + per-subject
-- embedded lists on /ui/machines/{mac}.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,        -- ISO 8601 UTC
    kind          TEXT NOT NULL,        -- dotted namespace
    subject_kind  TEXT,                 -- 'machine' / 'image' / 'catalog' / NULL
    subject_id    TEXT,                 -- mac / sha / src / NULL
    actor         TEXT,                 -- 'operator' / 'system' / 'pxe-client' / NULL
    -- IP that initiated / observed the event. For operator events,
    -- the request's client host (operator's browser / curl). For
    -- pxe-client events, the target's IP at check-in. NULL for
    -- events with no meaningful source IP (e.g. CLI-driven events
    -- where the bty-web process self-initiates).
    source_ip     TEXT,
    summary       TEXT NOT NULL,
    details       TEXT,                 -- JSON blob with kind-specific extras
    -- Operator tripwire-clear flag. 0 = unacknowledged; 1 =
    -- acknowledged. Unacknowledged failures count toward the
    -- dashboard Health Monitoring error tripwire; acknowledging one
    -- clears it from the count without deleting the row. Defaulted
    -- so the additive migration below can backfill it on DBs created
    -- before this column existed.
    acknowledged  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts_idx       ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_idx     ON events(kind);
CREATE INDEX IF NOT EXISTS events_subject_idx  ON events(subject_kind, subject_id);

-- Operator-overridable settings, a small key-value store. Most config
-- stays env-var / default driven (read-only on the Settings page); a
-- handful of values (upstream catalog URL, netboot release repo) can be
-- overridden here so they survive across restarts without editing the
-- unit file. A missing key means "no override": the resolver falls back
-- to the env var, then the built-in default.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL        -- ISO 8601 UTC of the last write
);

-- Single-row marker recording the ``bty.__version__`` that created
-- this state.db. On version mismatch ``init_db`` rotates the existing
-- DB to ``state.db.<from>.<ts>.bak`` and creates a fresh one.
CREATE TABLE IF NOT EXISTS bty_version (
    version  TEXT NOT NULL PRIMARY KEY
);

"""


def _bak_path(state_path: Path, from_version: str) -> Path:
    """Build the rotation target for ``state.db``.

    Format: ``state.db.<sanitised-from>.<UTC-iso-compact>.bak``. The
    timestamp prevents collisions when a single bty-web instance bounces
    through multiple releases. The version goes through a
    [^0-9A-Za-z.-]-stripping pass so a hypothetical bad-actor version
    string can't smuggle a path separator into the filename.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^0-9A-Za-z.-]", "_", from_version) or "unknown"
    return state_path.with_name(f"{state_path.name}.{safe}.{ts}.bak")


def _rotate_to_bak(state_path: Path, from_version: str) -> Path:
    """Move ``state.db`` to a versioned ``.bak`` and drop sidecars.

    sqlite3 WAL sidecars (``-journal`` / ``-wal`` / ``-shm``) refer
    to the main DB by filename; renaming the main file alone orphans
    them. Unlink them after rotation so a future ``state.db`` (about
    to be created by the caller) doesn't pick up stale pages from
    the previous DB's WAL.

    Same-second collisions get a numeric suffix; unlikely on a
    typical upgrade cadence but cheap to handle.
    """
    target = _bak_path(state_path, from_version)
    counter = 1
    while target.exists():
        target = state_path.with_name(f"{_bak_path(state_path, from_version).stem}.{counter}.bak")
        counter += 1
    state_path.rename(target)
    for suffix in ("-journal", "-wal", "-shm"):
        (state_path.parent / f"{state_path.name}{suffix}").unlink(missing_ok=True)
    return target


def init_db(path: Path) -> None:
    """Create ``path`` (and its parent directory) if missing; apply
    the schema; stamp the ``bty_version`` marker.

    On schema mismatch (stored marker != running version, or data
    tables exist without a marker -- pre-versioning DB), the old
    ``state.db`` is rotated to ``state.db.<from>.<ts>.bak`` and a
    fresh DB is created in its place. The rotation is recorded as a
    ``system.schema.reset`` event in the fresh DB so the dashboard
    tripwire surfaces it.

    Pre-1.0 contract (see module docstring): no migration apparatus,
    no schema-version integer, no operator intervention on upgrade.
    Image bytes are not on bty-web's disk in v0.40+ (withcache holds
    cached blobs in its own volume; oras streams from the registry),
    so a rotation here cannot lose them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    rotated_from: str | None = None
    rotated_to: Path | None = None

    if path.exists():
        # Probe the existing DB in a separate connection. We need to
        # know "fresh / matches / mismatches / pre-versioning" BEFORE
        # touching the file, because rotating only makes sense if we
        # actually find a stale schema.
        with closing(sqlite3.connect(path)) as probe:
            tables = {
                r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            has_data = bool(tables - {"sqlite_sequence", "bty_version"})
            stored: str | None = None
            if "bty_version" in tables:
                row = probe.execute("SELECT version FROM bty_version LIMIT 1").fetchone()
                if row is not None:
                    stored = row[0]

        if has_data and stored is None:
            rotated_from = "pre-versioning"
        elif stored is not None and stored != bty.__version__:
            rotated_from = stored

        if rotated_from is not None:
            rotated_to = _rotate_to_bak(path, rotated_from)

    # Path is now either non-existent (first boot, or just rotated)
    # or an in-place same-version DB (idempotent re-init). Apply the
    # schema + stamp the marker if it's not stamped yet.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA)

        stored_row = conn.execute("SELECT version FROM bty_version LIMIT 1").fetchone()
        if stored_row is None:
            conn.execute("INSERT INTO bty_version (version) VALUES (?)", (bty.__version__,))

        if rotated_from is not None and rotated_to is not None:
            # Lazy import: ``_events_log`` imports ``_db`` at module
            # load (circular if imported eagerly here).
            from . import _events_log

            _events_log.record(
                conn,
                kind="system.schema.reset",
                actor="system",
                summary=(
                    f"state.db rotated on upgrade ({rotated_from} -> {bty.__version__}). "
                    f"Machine bindings + audit log reset; image bytes "
                    f"(withcache volume / oras registry) are untouched."
                ),
                details={
                    "from_version": rotated_from,
                    "to_version": bty.__version__,
                    "archived_at": str(rotated_to),
                },
            )


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path``, ensure schema is applied, yield a Row-factory connection.

    ``timeout=5.0`` on the connect call bounds the wait if another
    process holds the write lock (sqlite's default is 5s already,
    but the value is implicit; making it explicit makes the
    contract auditable + protects against a future stdlib default
    drift). On a single-bty-web instance the WAL writer never
    contends with anything else, but our lifespan teardown waits
    on connection close -- without a bounded timeout, a wedged
    writer could starve systemd's shutdown sequence.
    """
    init_db(path)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
