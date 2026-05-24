"""Operator-overridable settings: a thin key-value store over the
``settings`` table in state.db.

Most bty-web configuration is env-var / default driven and read-only
(surfaced on the Settings page so an operator can see where each magic
value comes from). A small set of values can be overridden here and
persisted across restarts without touching the systemd unit:

- :data:`KEY_RELEASE_REPO` -- the GitHub ``owner/repo`` the netboot
  release fetch pulls artifacts from.
- :data:`KEY_CATALOG_URL` -- the full URL the "Fetch latest catalog"
  action downloads ``catalog.toml`` from.
- :data:`KEY_RELEASE_TAG` -- the release tag the "Fetch latest
  artifacts" action targets (``latest`` by default).

Resolution order is override (this table) -> environment variable ->
built-in default, so an unset key transparently falls back to the
existing behaviour. Not every key has an env layer:
:data:`KEY_RELEASE_REPO` reads :data:`ENV_RELEASE_REPO`, while
:data:`KEY_CATALOG_URL` and :data:`KEY_RELEASE_TAG` resolve straight
from override to default.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

from bty.web._releases import DEFAULT_REPO, ENV_RELEASE_REPO

# ``ENV_RELEASE_REPO`` is imported from :mod:`bty.web._releases` so the
# env-var name has a single definition; ``KEY_RELEASE_REPO`` falls back
# to it (via :func:`default_release_repo`) before the built-in default.
KEY_RELEASE_REPO = "upstream.release_repo"
KEY_CATALOG_URL = "upstream.catalog_url"
KEY_RELEASE_TAG = "upstream.release_tag"

DEFAULT_RELEASE_TAG = "latest"

# Scheduled-backup knobs. The Settings page exposes ``enabled`` +
# ``cadence`` + ``retention``; the scheduler loop reads them on each
# tick so a Settings change reflects within the next tick (no restart).
# ``last_run_at`` is written by the scheduler itself after a successful
# backup; it is NOT operator-editable from the UI -- the form only
# shows the value if present.
KEY_BACKUP_ENABLED = "backup.enabled"
KEY_BACKUP_CADENCE = "backup.cadence"
KEY_BACKUP_RETENTION = "backup.retention_count"
KEY_BACKUP_LAST_RUN_AT = "backup.last_run_at"

# ``manual`` keeps the scheduler quiescent -- the operator triggers
# every backup by hand from the Backup tab. The two non-manual values
# are anchor-from-last-run-at intervals (24h / 7d), NOT wall-clock
# times -- a wall-clock anchor would need timezone + locale handling
# we don't want to take on here.
BACKUP_CADENCES: tuple[str, ...] = ("daily", "weekly", "manual")
DEFAULT_BACKUP_CADENCE = "manual"
DEFAULT_BACKUP_RETENTION = 7


def get(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the stored override for ``key``, or ``None`` if unset."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value = row[0]
    return str(value) if value is not None else None


def set_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert ``key`` = ``value``. Caller owns the transaction."""
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, value, datetime.now(UTC).isoformat()),
    )


def clear(conn: sqlite3.Connection, key: str) -> None:
    """Remove any override for ``key`` (revert to env / default).
    Caller owns the transaction."""
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def default_release_repo() -> str:
    """The release repo from the environment, else the built-in default
    (ignores any DB override)."""
    return os.environ.get(ENV_RELEASE_REPO) or DEFAULT_REPO


def resolve_release_repo(conn: sqlite3.Connection) -> str:
    """The effective netboot release repo: override -> env -> default."""
    return get(conn, KEY_RELEASE_REPO) or default_release_repo()


def default_catalog_url(repo: str) -> str:
    """The catalog.toml URL bty fetches by default for ``repo``."""
    return f"https://github.com/{repo}/releases/latest/download/catalog.toml"


def resolve_catalog_url(conn: sqlite3.Connection) -> str:
    """The effective catalog URL: override -> URL built from the
    effective release repo."""
    return get(conn, KEY_CATALOG_URL) or default_catalog_url(resolve_release_repo(conn))


def resolve_release_tag(conn: sqlite3.Connection) -> str:
    """The effective netboot release tag to fetch: override ->
    :data:`DEFAULT_RELEASE_TAG` (``latest``)."""
    return get(conn, KEY_RELEASE_TAG) or DEFAULT_RELEASE_TAG


# ----- Backup schedule resolvers ----------------------------------------
#
# Booleans / ints / cadence strings round-trip through the same text-
# valued settings table. Helpers below give callers typed reads;
# the Settings form writes via :func:`set_value` and clears via
# :func:`clear`. Resolvers are strict: a stored value that does not
# match the canonical form raises -- pre-1.0 wants a loud signal that
# state.db has been hand-edited into something the UI no longer
# understands, not a silent fallback that hides the divergence.


class SettingValueError(ValueError):
    """Raised when a stored settings value can't be parsed.

    The Settings form normalises every value to its canonical form
    before persisting (``"1"`` / ``"0"`` for booleans, one of
    :data:`BACKUP_CADENCES` for cadence, a positive int for retention).
    A resolver only fires this when something else -- typically a
    hand-edit of state.db or a stale row left over from an older
    schema -- put a non-canonical value into the row. Operators can
    clear the offending key via the Settings form (or
    ``sqlite3 state.db "DELETE FROM settings WHERE key=...;"``) to
    revert to the default.
    """


def resolve_backup_enabled(conn: sqlite3.Connection) -> bool:
    """Effective ``backup.enabled``. ``None`` (unset) -> ``False``;
    ``"1"`` -> ``True``; ``"0"`` -> ``False``; anything else raises
    :class:`SettingValueError`."""
    raw = get(conn, KEY_BACKUP_ENABLED)
    if raw is None:
        return False
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise SettingValueError(f"{KEY_BACKUP_ENABLED}={raw!r} is not canonical (expected '1' or '0')")


def resolve_backup_cadence(conn: sqlite3.Connection) -> str:
    """Effective ``backup.cadence``. ``None`` (unset) ->
    :data:`DEFAULT_BACKUP_CADENCE`; a value in :data:`BACKUP_CADENCES`
    is returned as-is; anything else raises
    :class:`SettingValueError`."""
    raw = get(conn, KEY_BACKUP_CADENCE)
    if raw is None:
        return DEFAULT_BACKUP_CADENCE
    if raw in BACKUP_CADENCES:
        return raw
    raise SettingValueError(
        f"{KEY_BACKUP_CADENCE}={raw!r} is not a known cadence "
        f"(expected one of {', '.join(BACKUP_CADENCES)})"
    )


def resolve_backup_retention(conn: sqlite3.Connection) -> int:
    """Effective ``backup.retention_count``. ``None`` (unset) ->
    :data:`DEFAULT_BACKUP_RETENTION`; a positive int (as decimal
    string) is returned as-is; anything else raises
    :class:`SettingValueError`."""
    raw = get(conn, KEY_BACKUP_RETENTION)
    if raw is None:
        return DEFAULT_BACKUP_RETENTION
    try:
        n = int(raw)
    except ValueError as exc:
        raise SettingValueError(f"{KEY_BACKUP_RETENTION}={raw!r} is not an integer") from exc
    if n < 1:
        raise SettingValueError(f"{KEY_BACKUP_RETENTION}={n} is out of range (must be >= 1)")
    return n


def get_backup_last_run_at(conn: sqlite3.Connection) -> str | None:
    """ISO-8601 timestamp of the most recent successful scheduled
    backup, or ``None`` if no scheduled backup has succeeded yet.
    Manual backups do NOT update this value -- the scheduler's
    cadence math anchors to its own run history only."""
    return get(conn, KEY_BACKUP_LAST_RUN_AT)


def set_backup_last_run_at(conn: sqlite3.Connection, ts: str) -> None:
    """Record an ISO-8601 timestamp for the scheduler's last successful
    run. Caller owns the transaction."""
    set_value(conn, KEY_BACKUP_LAST_RUN_AT, ts)
