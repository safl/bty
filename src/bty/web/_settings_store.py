"""Operator-overridable settings: a thin key-value store over the
``settings`` table in state.db.

Most bty-web configuration is env-var / default driven and read-only
(surfaced on the Settings page so an operator can see where each magic
value comes from). A small set of values can be overridden here and
persisted across restarts without touching the systemd unit:

- :data:`KEY_NETBOOT_REPO` -- the GitHub ``owner/repo`` the netboot
  artifact fetch (vmlinuz / initrd / squashfs) pulls from. Default
  ``safl/bty``.
- :data:`KEY_CATALOG_REPO` -- the GitHub ``owner/repo`` the catalog
  fetch pulls ``catalog.toml`` from. Default ``safl/nosi``: bty
  consumes the upstream image-builder's auto-generated catalog
  rather than republishing a hand-maintained mirror.
- :data:`KEY_CATALOG_TAG` -- the release tag the "Fetch catalog"
  action targets (``latest`` by default).
- :data:`KEY_NETBOOT_TAG` -- the release tag the "Fetch artifacts"
  action targets (``latest`` by default). The two tags are
  independent so an operator can pin netboot artifacts to a known-
  good release while still pulling the moving catalog tip.

Resolution order is override (this table) -> environment variable ->
built-in default, so an unset key transparently falls back to the
existing behaviour. Only :data:`KEY_NETBOOT_REPO` has an env layer
(:data:`ENV_RELEASE_REPO` is ``BTY_BOOT_RELEASE_REPO``); the catalog
repo and the two tag keys resolve straight from override to their
built-in defaults.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

from bty.web._releases import DEFAULT_CATALOG_REPO, DEFAULT_NETBOOT_REPO, ENV_RELEASE_REPO

# ``ENV_RELEASE_REPO`` is imported from :mod:`bty.web._releases` so the
# env-var name has a single definition; ``KEY_NETBOOT_REPO`` falls back
# to it (via :func:`default_netboot_repo`) before the built-in default.
KEY_NETBOOT_REPO = "upstream.netboot_repo"
KEY_CATALOG_REPO = "upstream.catalog_repo"
KEY_CATALOG_TAG = "upstream.catalog_tag"
KEY_NETBOOT_TAG = "upstream.netboot_tag"

# Default tag for both catalog and netboot fetches. GitHub resolves
# ``latest`` to the most recent non-prerelease, non-draft tag.
DEFAULT_TAG = "latest"

# Optional withcache cache-host. When set, bty prefers it as the image
# *source* for artifacts it already holds (else serves the artifact as
# before). Resolves override -> env -> unset, so it can be configured via
# the systemd unit ($BTY_WITHCACHE_URL) without a DB write.
KEY_WITHCACHE_URL = "withcache.url"
ENV_WITHCACHE_URL = "BTY_WITHCACHE_URL"

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


def default_netboot_repo() -> str:
    """The netboot release repo from the environment, else the built-in
    default (ignores any DB override). bty's own CI publishes the
    ``vmlinuz`` / ``initrd`` / ``squashfs`` artifacts to this repo's
    releases; an operator forking bty points their fork here via
    ``$BTY_BOOT_RELEASE_REPO``."""
    return os.environ.get(ENV_RELEASE_REPO) or DEFAULT_NETBOOT_REPO


def default_catalog_repo() -> str:
    """The catalog repo's built-in default. bty consumes the upstream
    nosi project's auto-generated ``catalog.toml`` rather than
    republishing a mirror. No env-layer override: operators with a
    different upstream override via the Settings page (the resulting
    DB row beats this default)."""
    return DEFAULT_CATALOG_REPO


def resolve_netboot_repo(conn: sqlite3.Connection) -> str:
    """The effective netboot release repo: override -> env -> default."""
    return get(conn, KEY_NETBOOT_REPO) or default_netboot_repo()


def resolve_catalog_repo(conn: sqlite3.Connection) -> str:
    """The effective catalog repo: override -> default."""
    return get(conn, KEY_CATALOG_REPO) or default_catalog_repo()


def catalog_url_for(repo: str, tag: str) -> str:
    """The catalog.toml URL bty fetches for a given repo + tag.
    ``latest`` uses GitHub's redirect path (``releases/latest/download/``);
    everything else uses the explicit ``releases/download/<tag>/`` form."""
    if tag == "latest":
        return f"https://github.com/{repo}/releases/latest/download/catalog.toml"
    return f"https://github.com/{repo}/releases/download/{tag}/catalog.toml"


def resolve_catalog_tag(conn: sqlite3.Connection) -> str:
    """The effective catalog release tag to fetch: override ->
    :data:`DEFAULT_TAG` (``latest``)."""
    return get(conn, KEY_CATALOG_TAG) or DEFAULT_TAG


def resolve_catalog_url(conn: sqlite3.Connection) -> str:
    """The effective catalog URL, derived from the current catalog repo
    + tag. There is no separate override for the URL itself; operators
    tweak the catalog repo and/or the tag instead."""
    return catalog_url_for(resolve_catalog_repo(conn), resolve_catalog_tag(conn))


def resolve_netboot_tag(conn: sqlite3.Connection) -> str:
    """The effective netboot release tag to fetch: override ->
    :data:`DEFAULT_TAG` (``latest``)."""
    return get(conn, KEY_NETBOOT_TAG) or DEFAULT_TAG


def resolve_withcache_url(conn: sqlite3.Connection) -> str | None:
    """The withcache cache-host base URL, or ``None`` if unconfigured.

    Resolution: a DB override (set via Settings -> Upstream) wins;
    then ``[withcache] url`` from ``bty.toml`` (``cfg.withcache.url``);
    then the ``$BTY_WITHCACHE_URL`` env var directly; else ``None``
    (bty streams from origin URLs).

    Reading ``cfg.withcache.url`` is load-bearing: v0.42 moved the
    withcache URL into ``bty.toml`` and the generated compose / Quadlet
    no longer set ``$BTY_WITHCACHE_URL``. Before this, a stock container
    deploy resolved ``None`` here -- withcache was silently bypassed on
    the flash path (no HEAD, empty cache) even though bty.toml had the
    URL. The direct env read stays as the last fallback so a var set
    after the config was built (or before it's installed -- CLI paths /
    early startup) is still honoured."""
    override = get(conn, KEY_WITHCACHE_URL)
    if override:
        return override
    try:
        from bty.web._config import cfg as _cfg

        configured = (_cfg().withcache.url or "").strip()
    except RuntimeError:
        configured = ""
    if configured:
        return configured
    return (os.environ.get(ENV_WITHCACHE_URL) or "").strip() or None


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
