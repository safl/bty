"""Operator-overridable settings: a thin key-value store over the
``settings`` table in state.db.

Most bty-web configuration is env-var / default driven and read-only
(surfaced on the Settings page so an operator can see where each magic
value comes from). A small set of values can be overridden here and
persisted across restarts without touching the systemd unit:

- :data:`KEY_NETBOOT_REPO` -- the GitHub ``owner/repo`` the netboot
  artifact fetch (vmlinuz / initrd / squashfs) pulls from. Default
  ``safl/bty``.
- :data:`KEY_NETBOOT_TAG` -- the release tag the "Fetch artifacts"
  action targets (``latest`` by default).
- :data:`KEY_CATALOG_URL` -- the URL the "Fetch catalog" action
  pulls ``catalog.toml`` bytes from. Default
  :data:`DEFAULT_CATALOG_URL` (the latest nosi release's
  ``catalog.toml`` asset). A single URL is the natural shape here;
  unlike netboot (which fetches several assets per release), the
  catalog is one file. An operator pointing at a fork's catalog
  just edits the URL.

Resolution order is override (this table) -> environment variable ->
built-in default. Only :data:`KEY_NETBOOT_REPO` has an env layer
(:data:`ENV_RELEASE_REPO` is ``BTY_BOOT_RELEASE_REPO``); the netboot
tag and the catalog URL resolve straight from override to default.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bty.web._releases import DEFAULT_NETBOOT_REPO, ENV_RELEASE_REPO

# ``ENV_RELEASE_REPO`` is imported from :mod:`bty.web._releases` so the
# env-var name has a single definition; ``KEY_NETBOOT_REPO`` falls back
# to it (via :func:`default_netboot_repo`) before the built-in default.
KEY_NETBOOT_REPO = "upstream.netboot_repo"
KEY_NETBOOT_TAG = "upstream.netboot_tag"
KEY_CATALOG_URL = "upstream.catalog_url"

# Default tag for the netboot artifact fetch. GitHub resolves
# ``latest`` to the most recent non-prerelease, non-draft tag.
DEFAULT_TAG = "latest"

# Default URL the "Fetch catalog" button pulls bytes from.
#
# Points at nosi's ``/releases/latest/`` so a fresh ``bty-lab init``
# picks up whatever nosi release is current at fetch time, instead
# of an ISO-week tag baked into the bty version that drifts the
# moment a new bty release isn't cut.
#
# Byte-stability for production -- "two operators on the same bty
# version see the same catalog content" -- is now provided by
# withcache (since v0.59.0). Once an operator's withcache has the
# catalog's referenced images cached, evicting cache entries is
# how they choose to roll forward; until they do, every fetch
# resolves to the same cached blob regardless of what ``/latest/``
# rolled to upstream. Operators who want a hard pin (truly
# reproducible across cache evictions, or for production deploys
# where rolling under the operator's feet would surprise) paste a
# week-tagged URL into Settings -> Catalog.
DEFAULT_CATALOG_URL = "https://github.com/safl/nosi/releases/latest/download/catalog.toml"

# Optional withcache cache-host. When set, bty prefers it as the image
# *source* for artifacts it already holds (else serves the artifact as
# before). Resolves override -> env -> unset, so it can be configured via
# the systemd unit ($BTY_WITHCACHE_URL) without a DB write.
KEY_WITHCACHE_URL = "withcache.url"
ENV_WITHCACHE_URL = "BTY_WITHCACHE_URL"

# Optional nbdmux daemon. When set, bty's ``boot_mode=ramboot`` uses
# it as the NBD-export multiplexer that serves catalog images over
# the network for in-place ramboot. The value is the HTTP control
# plane URL (e.g. ``http://nbdmux:8082``); bty derives the NBD
# endpoint from the same host on port 10809. Resolves override ->
# env -> unset, same shape as ``KEY_WITHCACHE_URL``.
KEY_NBDMUX_URL = "nbdmux.url"
ENV_NBDMUX_URL = "BTY_NBDMUX_URL"

# Default tmpfs size for the in-target ramboot overlay. Operators
# override per-bind from the machine-edit form. Value is a string
# accepted by Linux's ``mount -t tmpfs -o size=...``. Conservative
# default keeps a CI / preview workload from filling host RAM by
# accident; long-running boots may need a bigger value.
KEY_RAMBOOT_OVERLAY_SIZE = "ramboot.overlay_size"
ENV_RAMBOOT_OVERLAY_SIZE = "BTY_RAMBOOT_OVERLAY_SIZE"
DEFAULT_RAMBOOT_OVERLAY_SIZE = "10G"

# Display timezone for ALL operator-facing timestamps rendered by the
# bty-web UI. bty stores timestamps as UTC; the renderer normalises to
# the configured zone before formatting. Resolves override -> env ->
# UTC. The override is an IANA zone name (``Europe/Copenhagen``,
# ``America/New_York``, ``UTC``); an invalid name raises
# :class:`SettingValueError` at resolve time and the Settings form
# rejects it before persisting.
KEY_DISPLAY_TZ = "display.timezone"
ENV_DISPLAY_TZ = "BTY_DISPLAY_TZ"

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


def resolve_netboot_repo(conn: sqlite3.Connection) -> str:
    """The effective netboot release repo: override -> env -> default."""
    return get(conn, KEY_NETBOOT_REPO) or default_netboot_repo()


def resolve_catalog_url(conn: sqlite3.Connection) -> str:
    """The effective catalog URL: override -> :data:`DEFAULT_CATALOG_URL`.
    The single URL is what the "Fetch catalog" button GETs; pointing at
    a fork is just a Settings-page edit, no repo + tag composition."""
    return get(conn, KEY_CATALOG_URL) or DEFAULT_CATALOG_URL


def resolve_netboot_tag(conn: sqlite3.Connection) -> str:
    """The effective netboot release tag to fetch: override ->
    :data:`DEFAULT_TAG` (``latest``)."""
    return get(conn, KEY_NETBOOT_TAG) or DEFAULT_TAG


def resolve_display_timezone(conn: sqlite3.Connection) -> ZoneInfo:
    """The effective timezone used to render operator-facing timestamps
    in the bty-web UI: override -> ``$BTY_DISPLAY_TZ`` -> UTC.

    The override is an IANA zone name. The Settings form validates
    before persisting (via :class:`ZoneInfo`), so a bad value here
    means an out-of-band write to state.db or a stale row from an
    older schema. Pre-1.0 wants a loud failure on that, not a silent
    UTC fallback that hides the divergence; this raises
    :class:`SettingValueError` so the renderer surfaces the bug
    to the operator.
    """
    raw = get(conn, KEY_DISPLAY_TZ) or (os.environ.get(ENV_DISPLAY_TZ) or "").strip()
    if not raw:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise SettingValueError(
            f"{KEY_DISPLAY_TZ}={raw!r} is not a known IANA timezone "
            f"(expected e.g. 'UTC', 'Europe/Copenhagen', 'America/New_York')"
        ) from exc


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


def resolve_nbdmux_url(conn: sqlite3.Connection) -> str | None:
    """The nbdmux daemon's HTTP control plane base URL, or ``None``
    if unconfigured. Same resolution shape as
    :func:`resolve_withcache_url`: override (Settings -> Bytes path)
    wins, then ``[nbdmux] url`` from ``bty.toml``, then
    ``$BTY_NBDMUX_URL`` env, else ``None`` (ramboot is unavailable).

    Returning ``None`` is not an error; it means an operator who
    binds ``boot_mode=ramboot`` will see a clear "nbdmux not
    configured" rejection from the form rather than a half-working
    flow that fails at boot time.
    """
    override = get(conn, KEY_NBDMUX_URL)
    if override:
        return override
    try:
        from bty.web._config import cfg as _cfg

        configured = (_cfg().nbdmux.url or "").strip()
    except (RuntimeError, AttributeError):
        # AttributeError covers older bty.toml configs that predate
        # the [nbdmux] section: cfg().nbdmux is missing entirely.
        # Fall through to env / unset rather than raising.
        configured = ""
    if configured:
        return configured
    return (os.environ.get(ENV_NBDMUX_URL) or "").strip() or None


def resolve_ramboot_overlay_size(conn: sqlite3.Connection) -> str:
    """The default tmpfs size for the ramboot overlay
    (``size=<value>`` on the mount command, units mount understands:
    ``10G``, ``8192M``, etc.). Resolves override -> env ->
    :data:`DEFAULT_RAMBOOT_OVERLAY_SIZE`.

    The mount layer validates the units at boot time; this resolver
    just round-trips the string. A bad value surfaces in the
    initramfs panic message rather than at bty-web render time, so
    operators see "tmpfs failed: invalid size" on the serial console
    rather than a Settings-form rejection. Acceptable since
    overlay size is a small, well-trodden setting and Linux's
    error message is descriptive.
    """
    raw = (
        get(conn, KEY_RAMBOOT_OVERLAY_SIZE)
        or (os.environ.get(ENV_RAMBOOT_OVERLAY_SIZE) or "").strip()
    )
    return raw or DEFAULT_RAMBOOT_OVERLAY_SIZE


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
