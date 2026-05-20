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

# Which boot_policy a ``bty-flash-once`` machine settles into once its
# flash completes (``POST /pxe/{mac}/done``): ``local`` (emit iPXE
# ``exit`` and let the firmware boot order pick the disk) or ``sanboot``
# (have iPXE boot the local disk itself). Override -> env -> default.
KEY_FLASH_SETTLE_POLICY = "flash.settle_policy"
ENV_FLASH_SETTLE_POLICY = "BTY_FLASH_SETTLE_POLICY"
DEFAULT_FLASH_SETTLE_POLICY = "local"
# The policies a bty-flash-once machine may settle into. Anything else
# (typo in the env var / DB) resolves back to the default.
FLASH_SETTLE_POLICIES = ("local", "sanboot")


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


def default_flash_settle_policy() -> str:
    """The bty-flash-once settle policy from the environment, else the
    built-in default (ignores any DB override). Falls back to the
    default if the env value isn't a recognised policy."""
    value = os.environ.get(ENV_FLASH_SETTLE_POLICY)
    if value in FLASH_SETTLE_POLICIES:
        return value
    return DEFAULT_FLASH_SETTLE_POLICY


def resolve_flash_settle_policy(conn: sqlite3.Connection) -> str:
    """The boot_policy a ``bty-flash-once`` machine settles into on flash
    completion: override -> env -> default. An unrecognised override
    (typo / stale value) resolves back to the env / built-in default so
    a bad setting can never wedge a machine into an invalid policy."""
    override = get(conn, KEY_FLASH_SETTLE_POLICY)
    if override in FLASH_SETTLE_POLICIES:
        return override
    return default_flash_settle_policy()
