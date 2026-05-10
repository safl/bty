"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand for now; a migration framework can
be added when the need arises.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db`` to match the appliance image's expectations).

Pre-1.0: the schema is whatever ``CREATE TABLE`` says here. There is
no migration apparatus -- breaking changes during the pre-1.0 stretch
are landed by the operator wiping ``state.db`` (the appliance is
trivial to redeploy and machine records are operator-typed). The
post-1.0 cadence will pick up a proper migration framework before the
first stable tag.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/bty")


def default_state_path() -> Path:
    """Resolve ``state.db`` location from ``$BTY_STATE_DIR`` or the default."""
    env = os.environ.get("BTY_STATE_DIR")
    base = Path(env) if env else DEFAULT_STATE_DIR
    return base / "state.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac                       TEXT PRIMARY KEY,
    image_sha256              TEXT,    -- content-addressed image identity
    provisioning_mode         TEXT NOT NULL DEFAULT 'none',
    hostname                  TEXT,
    cijoe_task_ref            TEXT,
    discovered_at             TEXT,    -- first /pxe/{mac} contact (NULL if PUT-created)
    last_seen_at              TEXT,    -- most recent /pxe/{mac} contact
    last_seen_ip              TEXT,    -- source IP of most recent /pxe contact
    boot_policy               TEXT NOT NULL DEFAULT 'local',
    last_flashed_at           TEXT,    -- updated by POST /pxe/{mac}/done
    last_task_run_at          TEXT,    -- start of the most recent task run
    last_task_status          TEXT,    -- one of bty.web._models.TASK_STATUSES, or NULL
    last_task_output_path     TEXT,    -- on-disk dir of the cijoe run
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

-- Operator-curated catalog entries.
-- Operator pastes ``image_url`` (+ optional ``sha_url``) via the
-- bty-web UI; the row drives the catalog table on /ui/images.
-- Without a sha, the entry is flashable via the URL streaming
-- pipeline but not bindable to a machine (machines.image_sha256
-- binds by content).
CREATE TABLE IF NOT EXISTS catalog_entries (
    src          TEXT PRIMARY KEY,
    sha256       TEXT,
    name         TEXT NOT NULL,
    sha_url      TEXT,
    format       TEXT,
    size_bytes   INTEGER,
    description  TEXT,
    added_at     TEXT NOT NULL
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
    -- pxe-client events, the target's IP at check-in. For
    -- task-runner system events, the target's last_seen_ip we
    -- SSHed to. NULL for events with no meaningful source IP.
    source_ip     TEXT,
    summary       TEXT NOT NULL,
    details       TEXT                  -- JSON blob with kind-specific extras
);
CREATE INDEX IF NOT EXISTS events_ts_idx       ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_idx     ON events(kind);
CREATE INDEX IF NOT EXISTS events_subject_idx  ON events(subject_kind, subject_id);
"""


class StaleSchemaError(RuntimeError):
    """Raised when state.db exists but is missing columns added in
    newer bty-web versions. Pre-1.0 has no migrations apparatus;
    the fix is to wipe state.db. The error message names the
    missing columns + path so an operator can act without
    grepping the source."""


# Columns that were added to existing tables after the initial
# schema landed. Each entry is checked on every ``init_db`` call;
# a missing column raises :class:`StaleSchemaError` with an
# operator-actionable message instead of letting the first
# subsequent ``SELECT`` blow up with ``no such column``.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "machines": ("last_task_status", "last_task_run_at", "last_task_output_path"),
    "events": ("source_ip",),
}


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
    with sqlite3.connect(path) as conn:
        _detect_stale_schema(conn, path)
        conn.executescript(SCHEMA)
        conn.commit()


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
