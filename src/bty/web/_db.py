"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db`` to match the appliance image's expectations).

Pre-1.0: the schema is whatever ``CREATE TABLE`` says here. There is
no migration apparatus. The DB carries the exact ``bty.__version__``
that created it in the ``bty_version`` table; bty-web refuses to
start if the running version doesn't match. Every release is
therefore breaking for state -- by design. The cross-release path
is ``bty-web export`` (slim bundle of images + cached files +
hardware inventory) then wipe and import on the new release. Plain
operator state (image bindings, boot policies, settings) is re-
typed on the new appliance, by design.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
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

-- Single-row marker recording the ``bty.__version__`` that created
-- this state.db. ``init_db`` refuses to start bty-web if the stored
-- version doesn't EXACTLY match the running code. Pre-1.0 policy:
-- no migration apparatus, no patch-release leniency, no schema-
-- version integer that operators have to track separately. Every
-- release that ships a wheel to PyPI is a release that wipes state
-- (or migrates via export/import). Operators who want to preserve
-- hardware inventory across an upgrade run ``bty-web export`` on
-- the running version, wipe state.db, install the new version, and
-- ``bty-web import`` on the new release -- the slim bundle format
-- is version-tolerant.
CREATE TABLE IF NOT EXISTS bty_version (
    version  TEXT NOT NULL PRIMARY KEY
);
"""


class VersionMismatchError(RuntimeError):
    """Raised when state.db's ``bty_version`` row doesn't match the
    running ``bty.__version__``, OR the DB has data tables but no
    ``bty_version`` row (pre-versioning DB from an older release).
    The recovery is ``rm state.db`` (and ``bty-web import`` afterwards
    if hardware inventory should be preserved). Pre-1.0: no migration
    apparatus."""


# DB state classifier. Returned by :func:`check_db` so callers
# (specifically ``bty.web._app.create_app``) can decide whether to
# build the full app or a recovery-mode app -- without raising.
# v0.32.0+: when bty-web boots against a mismatched / pre-versioning
# DB, it starts a minimal recovery UI on the same port instead of
# dying in the journal, so the operator gets a styled wizard in the
# browser. ``init_db`` keeps raising for callers that want the strict
# "refuse to proceed" semantics; ``check_db`` is the non-mutating
# probe the recovery flow uses.
class DbState:
    """Sentinel values returned by :func:`check_db`."""

    FRESH = "fresh"  # no tables at all; init_db would stamp + return
    OK = "ok"  # marker matches running bty.__version__
    PRE_VERSIONING = "pre_versioning"  # data tables exist, no marker
    MISMATCH = "mismatch"  # marker present but != running version


@dataclass(frozen=True)
class DbCheckResult:
    """Outcome of a non-mutating ``check_db`` probe.

    The recovery UI renders directly from these fields so the
    operator sees a faithful summary of what bty-web found.
    """

    state: str  # one of :class:`DbState` values
    stored_version: str | None  # ``bty_version`` row's value, or None
    running_version: str  # bty.__version__
    has_data_tables: bool  # True iff any data table other than sqlite_sequence/bty_version
    path: Path

    @property
    def needs_recovery(self) -> bool:
        """True iff bty-web should run in recovery mode instead of normal."""
        return self.state in (DbState.PRE_VERSIONING, DbState.MISMATCH)


def check_db(path: Path) -> DbCheckResult:
    """Non-mutating probe of ``path``'s shape vs the running bty version.

    Returns a :class:`DbCheckResult` describing what's on disk. Does
    NOT create / alter / stamp anything -- safe to call from a
    recovery-mode startup that needs to keep the operator's data
    readable until they decide what to do with it.

    A missing path returns ``DbState.FRESH`` (``init_db`` would
    create + stamp it). An unreadable / locked DB still returns a
    best-effort guess; downstream callers must handle a partial
    result rather than crash.
    """
    if not path.exists():
        return DbCheckResult(
            state=DbState.FRESH,
            stored_version=None,
            running_version=bty.__version__,
            has_data_tables=False,
            path=path,
        )
    # Open read-only to avoid creating the file or writing a journal
    # if the DB is fine. ``mode=ro`` is the URI form sqlite3 supports.
    uri = f"file:{path}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            has_data_tables = bool(tables - {"sqlite_sequence", "bty_version"})
            stored: str | None = None
            if "bty_version" in tables:
                row = conn.execute("SELECT version FROM bty_version LIMIT 1").fetchone()
                if row is not None:
                    stored = row[0]
    except sqlite3.Error:
        # Corrupt / locked / not a sqlite file -- treat as
        # pre-versioning so the recovery UI surfaces it.
        return DbCheckResult(
            state=DbState.PRE_VERSIONING,
            stored_version=None,
            running_version=bty.__version__,
            has_data_tables=True,
            path=path,
        )

    if not has_data_tables and stored is None:
        return DbCheckResult(
            state=DbState.FRESH,
            stored_version=None,
            running_version=bty.__version__,
            has_data_tables=False,
            path=path,
        )
    if stored is None:
        return DbCheckResult(
            state=DbState.PRE_VERSIONING,
            stored_version=None,
            running_version=bty.__version__,
            has_data_tables=has_data_tables,
            path=path,
        )
    if stored != bty.__version__:
        return DbCheckResult(
            state=DbState.MISMATCH,
            stored_version=stored,
            running_version=bty.__version__,
            has_data_tables=has_data_tables,
            path=path,
        )
    return DbCheckResult(
        state=DbState.OK,
        stored_version=stored,
        running_version=bty.__version__,
        has_data_tables=has_data_tables,
        path=path,
    )


def init_db(path: Path) -> None:
    """Create ``path`` (and its parent directory) if missing; apply
    the schema; verify the ``bty_version`` marker matches.

    Pre-1.0: no migrations. The DB carries a ``bty_version`` row with
    the ``bty.__version__`` that created it; bty-web REFUSES to start
    if the running version doesn't match exactly. The recovery is
    documented in the :class:`VersionMismatchError` message and in
    operations.md. Every release that bumps ``__version__`` is a
    release that requires a state.db wipe (or export+wipe+import).

    Fresh DB (no tables at all) gets the current version stamped on
    init. A DB with data tables but no ``bty_version`` row is a pre-
    versioning install -- refuse with a clear message.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``with sqlite3.connect(...)`` is a *transaction* context, not a
    # *close* context -- it commits/rolls back but leaves the
    # connection open (closed only by refcount GC, fragile off
    # CPython). ``closing`` guarantees the fd is released here.
    with closing(sqlite3.connect(path)) as conn, conn:
        # Pre-SCHEMA pass: catch a pre-versioning DB BEFORE doing
        # anything that mutates the DB. The earlier version of this
        # check ran ``executescript(SCHEMA)`` first and only then
        # decided to raise -- but ``sqlite3.executescript`` issues an
        # implicit COMMIT, so the ``CREATE TABLE IF NOT EXISTS
        # bty_version`` inside SCHEMA committed regardless of what
        # came after. On the next systemd restart, ``bty_version``
        # existed (empty), ``had_marker_before_schema`` flipped True,
        # and the refuse condition silently inverted to "fresh DB, go
        # stamp the marker." The franken-state slipped through. We
        # now decide BEFORE running SCHEMA: if data tables exist and
        # the marker table doesn't, refuse without touching anything.
        existing_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        has_data_tables = bool(existing_tables - {"sqlite_sequence", "bty_version"})
        marker_table_existed = "bty_version" in existing_tables

        if has_data_tables and not marker_table_existed:
            # Pre-versioning DB. Refuse before SCHEMA runs so the next
            # invocation (after systemd's Restart=on-failure retry)
            # hits the same condition rather than seeing a half-
            # created marker table.
            raise VersionMismatchError(
                f"bty-web: state.db at {path} has data tables but no "
                f"bty_version table -- this is a pre-versioning DB from "
                f"an older bty release. Pre-1.0 policy: no migration "
                f"apparatus.\n\n"
                f"Recovery (loses operator state -- bindings, settings, "
                f"audit log):\n"
                f"  sudo systemctl stop bty-web\n"
                f"  sudo rm {path}\n"
                f"  sudo systemctl start bty-web\n\n"
                f"To preserve hardware inventory across the wipe, run "
                f"``bty-web export`` on the OLD version BEFORE wiping, "
                f"then ``bty-web import`` on the new release. See "
                f"operations.md."
            )

        if marker_table_existed:
            # Check the stored version BEFORE SCHEMA in the same
            # mutation-free way -- a mismatched marker means we refuse
            # to touch the DB at all, leaving any export attempt on the
            # OLD release reading consistent state.
            stored_row = conn.execute("SELECT version FROM bty_version LIMIT 1").fetchone()
            if stored_row is not None and stored_row[0] != bty.__version__:
                raise VersionMismatchError(
                    f"bty-web: state.db at {path} carries bty_version "
                    f"{stored_row[0]!r}, but the running code is "
                    f"{bty.__version__!r}. Pre-1.0 policy: no migration "
                    f"apparatus -- every release wipes state.\n\n"
                    f"Recovery (loses operator state):\n"
                    f"  sudo systemctl stop bty-web\n"
                    f"  sudo rm {path}\n"
                    f"  sudo systemctl start bty-web\n\n"
                    f"To preserve hardware inventory (MAC + lshw + "
                    f"known_disks) across the wipe, run ``bty-web export`` "
                    f"on the OLD version BEFORE upgrading, then "
                    f"``bty-web import`` on the new release. See "
                    f"operations.md."
                )

        # All clear: either a fresh DB (no tables at all) or an
        # already-versioned DB that matches the running code. Apply
        # the schema + stamp the marker if it's not stamped yet.
        conn.executescript(SCHEMA)

        stored_row = conn.execute("SELECT version FROM bty_version LIMIT 1").fetchone()
        if stored_row is None:
            conn.execute("INSERT INTO bty_version (version) VALUES (?)", (bty.__version__,))
        # Same-version case: idempotent no-op.


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path``, ensure schema is applied, yield a Row-factory connection.

    ``timeout=5.0`` on the connect call bounds the wait if another
    process holds the write lock (sqlite's default is 5s already,
    but the value is implicit; making it explicit makes the
    contract auditable + protects against a future stdlib default
    drift). On a single-bty-web appliance the WAL writer never
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
