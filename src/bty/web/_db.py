"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` — no SQLAlchemy or SQLModel dep. The schema
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
    mac                 TEXT PRIMARY KEY,
    image               TEXT,
    provisioning_mode   TEXT NOT NULL DEFAULT 'none',
    hostname            TEXT,
    cijoe_workflow_ref  TEXT,
    last_known_good     TEXT,        -- JSON blob; NULL until first online cijoe
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""


def init_db(path: Path) -> None:
    """Create ``path`` (and its parent directory) if missing; apply the schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
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
