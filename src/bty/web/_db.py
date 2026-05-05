"""SQLite-backed persistence for bty-web.

Uses stdlib :mod:`sqlite3` - no SQLAlchemy or SQLModel dep. The schema
is small enough to evolve by hand for now; a migration framework can
be added when the need arises.

State lives at ``$BTY_STATE_DIR/state.db`` (default
``/var/lib/bty/state.db`` to match the appliance image's expectations).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/bty")
DEFAULT_SESSION_TTL = timedelta(days=30)


def default_state_path() -> Path:
    """Resolve ``state.db`` location from ``$BTY_STATE_DIR`` or the default."""
    env = os.environ.get("BTY_STATE_DIR")
    base = Path(env) if env else DEFAULT_STATE_DIR
    return base / "state.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac                       TEXT PRIMARY KEY,
    image                     TEXT,
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

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,    -- sha256 hex of the bearer; plaintext never persisted
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,       -- ISO; expired rows ignored on lookup
    last_used_at TEXT,
    label        TEXT                 -- optional UA / device hint set at login
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


# ---------- session helpers -------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def issue_session(
    conn: sqlite3.Connection,
    *,
    ttl: timedelta = DEFAULT_SESSION_TTL,
    label: str | None = None,
) -> tuple[str, datetime]:
    """Generate a new opaque bearer token, persist its hash, return ``(token, expires_at)``.

    Plaintext is returned to the caller exactly once - nothing in the
    DB can recover it, only verify a presented value via
    :func:`find_active_session`.
    """
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + ttl
    conn.execute(
        "INSERT INTO sessions(token_hash, created_at, expires_at, last_used_at, label) "
        "VALUES (?, ?, ?, NULL, ?)",
        (_hash_token(token), now.isoformat(), expires.isoformat(), label),
    )
    conn.commit()
    return token, expires


def find_active_session(conn: sqlite3.Connection, token: str) -> bool:
    """Return True if ``token`` matches an unexpired session row.

    Updates ``last_used_at`` on the matched row (best-effort; not
    fatal if the connection is busy).
    """
    h = _hash_token(token)
    now = _now().isoformat()
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (h, now),
    ).fetchone()
    if row is None:
        return False
    try:
        conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE token_hash = ?",
            (now, h),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Concurrent locker - the session is still valid; skip the
        # last_used_at touch rather than fail auth.
        pass
    return True


def revoke_session(conn: sqlite3.Connection, token: str) -> bool:
    """Delete the row matching ``token``; return True if a row was deleted."""
    cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()
    return cur.rowcount > 0


def revoke_all_sessions(conn: sqlite3.Connection) -> int:
    """Delete every session row; return the number deleted."""
    cur = conn.execute("DELETE FROM sessions")
    conn.commit()
    return cur.rowcount


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    """Delete rows whose ``expires_at`` is in the past; return the count."""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now().isoformat(),))
    conn.commit()
    return cur.rowcount


def has_active_sessions(conn: sqlite3.Connection) -> bool:
    """True if any unexpired session row exists."""
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE expires_at > ? LIMIT 1",
        (_now().isoformat(),),
    ).fetchone()
    return row is not None
