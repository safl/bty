"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db`` to match the appliance image's expectations).

Pre-1.0: the schema is whatever ``CREATE TABLE`` says here. There is
no migration apparatus -- breaking changes are landed by the operator
wiping ``state.db`` (the appliance is trivial to redeploy and machine
records are operator-typed). A proper migration framework will land
before the 1.0 tag.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

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
    """Resolve ``state.db`` location from ``$BTY_STATE_DIR`` or the default."""
    env = os.environ.get("BTY_STATE_DIR")
    base = Path(env) if env else DEFAULT_STATE_DIR
    return base / "state.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac                       TEXT PRIMARY KEY,
    -- Binding target: ``catalog_entries.bty_image_ref`` (sha256
    -- of canonicalised src), not the content sha. Lets operators
    -- bind rolling-tag oras refs and URL-only entries that have
    -- no pre-known content sha.
    bty_image_ref             TEXT,
    hostname                  TEXT,
    discovered_at             TEXT,    -- first /pxe/{mac} contact (NULL if PUT-created)
    last_seen_at              TEXT,    -- most recent /pxe/{mac} contact
    last_seen_ip              TEXT,    -- source IP of most recent /pxe contact
    boot_mode               TEXT NOT NULL DEFAULT 'ipxe-exit',
    -- iPXE BIOS drive the ``sanboot`` boot_mode boots (``0x80`` =
    -- first disk). NULL = use the default (``0x80``). Distinct from
    -- ``target_disk_serial``: iPXE picks local disks by BIOS drive
    -- number, not by the Linux serial the flash step matches.
    sanboot_drive             TEXT,
    last_flashed_at           TEXT,    -- updated by POST /pxe/{mac}/done
    -- One-shot state bit for the ``bty-flash-always`` loop-break.
    -- Armed (1) when the machine fetches a flash-chain artifact with
    -- ``?mac=`` (GET /boot/...?mac=X) -- positive proof it booted the
    -- flasher. Consumed (back to 0) by the next ``GET /pxe/{mac}``,
    -- which serves a one-shot sanboot of the just-flashed disk instead
    -- of reflashing; the next real netboot (no artifact fetch in
    -- between) flips back to the flash chain. Confined to
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

-- Operator-curated catalog entries.
--
-- ``bty_image_ref`` is the stable provenance identifier:
-- sha256(canonicalise_src(src)). Primary key. The value
-- ``machines.bty_image_ref`` references.
--
-- ``src`` is the operator-typed source (file://, http(s)://, or
-- oras://). UNIQUE -- two rows can't share a src. Different srcs
-- whose content happens to match end up as distinct entries with
-- potentially-equal ``disk_image_sha``.
--
-- ``disk_image_sha`` is the OBSERVED content hash. Populated:
--   - on first cache via fetch-to-cache (remote);
--   - on hash-by-HashManager for local file://;
--   - if pre-pinned in the source TOML manifest (import flow).
-- May stay NULL for an entry that has never been hashed/fetched.
CREATE TABLE IF NOT EXISTS catalog_entries (
    bty_image_ref  TEXT PRIMARY KEY,
    src            TEXT NOT NULL UNIQUE,
    disk_image_sha TEXT,
    name           TEXT NOT NULL,
    sha_url        TEXT,
    format         TEXT,
    size_bytes     INTEGER,
    description    TEXT,
    added_at       TEXT NOT NULL
);

-- Slim audit log of operator + machine activity.
-- Append-only, queryable. Backs the /ui/events page + per-subject
-- embedded lists on /ui/machines/{mac} and /ui/images.
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
"""


class StaleSchemaError(RuntimeError):
    """Raised when state.db exists but is missing required columns.
    Pre-1.0 has no migration apparatus; the fix is to wipe state.db.
    The error message names the missing columns + path so an
    operator can act without grepping the source."""


# Columns the current schema requires. Each entry is checked on
# every ``init_db`` call against an existing DB; a missing column
# raises :class:`StaleSchemaError` with an operator-actionable
# message ("rm state.db") instead of letting the first subsequent
# ``SELECT`` blow up with ``no such column``.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": ("source_ip",),
    # ``boot_mode`` (renamed from ``boot_policy``): listing it here forces
    # a clean state.db reset on an old DB that still has ``boot_policy``,
    # rather than a runtime "no such column" error.
    "machines": ("bty_image_ref", "known_disks", "target_disk_serial", "boot_mode"),
    "catalog_entries": ("bty_image_ref", "disk_image_sha"),
}


# Columns added after their table's initial release that carry a
# DEFAULT, so they can be backfilled in place with a plain
# ``ALTER TABLE ... ADD COLUMN`` rather than forcing a state.db wipe.
# This is the narrow exception to the "no migrations" rule: a
# defaulted column is non-destructive to add, and wiping the whole
# DB (losing machine inventory) just to gain an events flag would
# defeat bty-state-migrate's persist-across-reflash promise.
# Strictly-required columns with no sensible default still go through
# ``_REQUIRED_COLUMNS`` + the wipe path.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "events": {
        "acknowledged": "INTEGER NOT NULL DEFAULT 0",
    },
    "machines": {
        # Nullable (NULL = use the default sanboot drive, 0x80), so a
        # plain ADD COLUMN backfills existing rows with NULL.
        "sanboot_drive": "TEXT",
        # One-shot bty-flash-always loop-break bit; existing rows
        # backfill to 0 (not yet seen a post-flash artifact fetch).
        "saw_flasher_boot": "INTEGER NOT NULL DEFAULT 0",
        # Full lshw -json hardware blob + its timestamp; nullable, so a
        # plain ADD COLUMN backfills existing rows with NULL.
        "hw_lshw": "TEXT",
        "hw_lshw_at": "TEXT",
    },
}


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    """Add any missing defaulted columns from :data:`_ADDITIVE_COLUMNS`.

    Idempotent: runs after the ``CREATE TABLE IF NOT EXISTS`` pass so
    a fresh DB already has the column (the ALTER is skipped); an older
    DB gets the column added in place with its default backfilled.
    Table + column names are internal constants, never user input, so
    the f-string SQL carries no injection surface.
    """
    for table, cols in _ADDITIVE_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # fresh DB: SCHEMA already created it with the column
        existing = {r[1] for r in rows}
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _detect_stale_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Raise :class:`StaleSchemaError` if any expected column is
    missing on an existing table. Pre-1.0: the recovery is ``rm
    <state.db>`` and let bty-web recreate it.

    Tables that don't exist yet (fresh DB) are skipped; the
    ``CREATE TABLE IF NOT EXISTS`` in :data:`SCHEMA` will create
    them with the current shape.
    """
    for table, required in _REQUIRED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # table doesn't exist yet -- SCHEMA will create it
        existing = {r[1] for r in rows}
        missing = [c for c in required if c not in existing]
        if missing:
            raise StaleSchemaError(
                f"bty-web state.db at {path} is missing columns "
                f"{missing!r} on table {table!r}. Pre-1.0 has no "
                f"migrations apparatus -- delete the file "
                f"(``rm {path}``) and let bty-web recreate it on "
                f"next startup. Existing machine records will be "
                f"lost; auto-discovery will re-populate from "
                f"first PXE contact."
            )


def init_db(path: Path) -> None:
    """Create ``path`` (and its parent directory) if missing; apply the schema.

    Pre-1.0: no migrations. The schema is whatever :data:`SCHEMA`
    says. Idempotent for first-init / fresh-create; calling against
    an existing DB is a no-op for the tables already there. If an
    existing DB has tables with missing columns (a stale schema
    from an older bty-web), :class:`StaleSchemaError` is raised
    with operator-actionable recovery instructions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``with sqlite3.connect(...)`` is a *transaction* context, not a
    # *close* context -- it commits/rolls back but leaves the
    # connection open (closed only by refcount GC, fragile off
    # CPython). ``closing`` guarantees the fd is released here.
    with closing(sqlite3.connect(path)) as conn, conn:
        _detect_stale_schema(conn, path)
        conn.executescript(SCHEMA)
        _apply_additive_columns(conn)


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path``, ensure schema is applied, yield a Row-factory connection."""
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
