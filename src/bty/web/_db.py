"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand for now; a migration framework can
be added when the need arises.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db`` to match the appliance image's expectations).
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
    image_sha256              TEXT,    -- content-addressed image identity (M22)
    provisioning_mode         TEXT NOT NULL DEFAULT 'none',
    hostname                  TEXT,
    cijoe_workflow_ref        TEXT,
    last_known_good           TEXT,    -- JSON blob; NULL until first online cijoe
    discovered_at             TEXT,    -- first /pxe/{mac} contact (NULL if PUT-created)
    last_seen_at              TEXT,    -- most recent /pxe/{mac} contact
    last_seen_ip              TEXT,    -- source IP of most recent /pxe contact
    boot_policy               TEXT NOT NULL DEFAULT 'local',
    last_flashed_at           TEXT,    -- updated by POST /pxe/{mac}/done
    last_workflow_run_at      TEXT,    -- start of the most recent workflow run
    last_workflow_status      TEXT,    -- 'running' / 'success' / 'failed' / NULL
    last_workflow_output_path TEXT,    -- on-disk dir of the cijoe run
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

-- Operator-curated catalog entries (M23).
-- Lets the operator add image URLs via the bty-web UI form
-- (``image-url`` + optional ``sha-url``) without authoring a
-- catalog.toml file or dropping bytes into BTY_IMAGES. The URL
-- is the canonical key; ``sha256`` is optional metadata that
-- enables SHA-keyed machine binding when known. Without a sha,
-- the entry can still be flashed (URL streaming pipeline) but
-- cannot be bound to a machine (machines.image_sha256 binds by
-- content). Operators with vendors that don't publish sha256
-- manifests still get a managed catalog; operators with strict
-- integrity requirements provide ``sha_url`` and get the SHA.
CREATE TABLE IF NOT EXISTS catalog_entries (
    src          TEXT PRIMARY KEY,    -- the operator-typed image URL (canonical key)
    sha256       TEXT,                -- 64 lower-hex; NULL if no sha_url given
    name         TEXT NOT NULL,       -- display name (URL filename)
    sha_url      TEXT,                -- the operator-typed sha256-manifest URL (or NULL)
    format       TEXT,                -- detect_format(name); NULL if unrecognised
    size_bytes   INTEGER,             -- HEAD ``Content-Length`` at add time, or NULL
    description  TEXT,                -- free-form operator note
    added_at     TEXT NOT NULL
);
"""

# Columns that were added to ``machines`` after the original schema landed.
# ``init_db`` ALTERs the table to add them when an older DB is opened, so
# upgrades don't require operators to wipe ``state.db``. Each entry is
# ``(name, sqlite-decl)`` - the decl includes any DEFAULT clause needed
# for the migration to populate existing rows.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("discovered_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("last_seen_ip", "TEXT"),
    ("boot_policy", "TEXT NOT NULL DEFAULT 'local'"),
    ("last_flashed_at", "TEXT"),
    ("last_workflow_run_at", "TEXT"),
    ("last_workflow_status", "TEXT"),
    ("last_workflow_output_path", "TEXT"),
)


def init_db(path: Path) -> None:
    """Create ``path`` (and its parent directory) if missing; apply the schema.

    Also applies idempotent additive migrations: any column listed in
    :data:`_ADDED_COLUMNS` that does not yet exist gets ``ALTER TABLE``'d
    in. Safe to call repeatedly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
        for column, decl in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE machines ADD COLUMN {column} {decl}")
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
