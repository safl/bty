"""Slim audit log of bty-web activity.

A single append-only ``events`` table in state.db captures the
"who did what when" timeline that the operator wants visible in
the UI: machines checking in, machines being flashed, image
uploads, catalog adds / deletes, settings changes, auth attempts.
Three rendering surfaces consume the same rows:

1. ``/ui/events`` -- top-level page with filter + pagination.
2. ``/ui/machines/{mac}`` -- the most recent events touching this
   MAC, embedded in the machine-detail card.
3. ``/ui/images`` -- the most recent events touching the image
   catalog (catalog adds / deletes, manifest imports).

Conventions:

- ``kind`` is a dotted lowercase namespace, e.g.
  ``machine.discovered``, ``machine.flashed``,
  ``catalog.entry.added``, ``netboot.artifacts.fetched``. Stable
  strings; the UI keys badge colours off them.
- ``subject_kind`` + ``subject_id`` together identify the entity
  the event is about. ``machine`` / mac, ``image`` / sha or name,
  ``catalog`` / src URL, ``boot`` / release-tag, ``settings`` /
  panel-name. Either may be ``None`` for global events.
- ``actor`` distinguishes operator-initiated changes from
  system-initiated ones (auto-discovery, hash completion).
  See :data:`KNOWN_ACTORS` for the catalogue of conventional
  values; ``record`` does not enforce the set so an unrecognised
  actor still flows through, it just won't appear in the
  /ui/events filter dropdown.
- ``details`` is an optional JSON blob with kind-specific extras
  (return code, error text, etc.) -- not currently surfaced in
  the UI table; the JSON ``GET /events`` API returns it so an
  operator can drill in via curl / scripting if needed.

Retention: append-only, no automatic trimming. The table is small
(a few KB / event) so years of homelab activity fit without
trouble. Operators with strict retention requirements run
``DELETE FROM events WHERE ts < ?`` themselves.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bty.web import _db


def normalize_ip(host: str | None) -> str | None:
    """Canonicalise a client IP string for storage / filtering.

    Starlette returns whatever the connection used: when bty-web
    binds dual-stack (``::``) and a v4 client connects, the host
    arrives as the v4-mapped-v6 form ``::ffff:192.168.1.5``.
    Storing both forms in ``events.source_ip`` and
    ``machines.last_seen_ip`` would split the operator's view of
    one client across two rows; the filter pivot on /ui/events
    would silently miss half the activity.

    Returns the bare v4 form for v4-mapped addresses, the
    compressed form for v6 (e.g. ``2001:db8::1``), and the
    input unchanged for anything ``ipaddress`` doesn't recognise
    (so unusual transports / unix socket paths still flow
    through).
    """
    if host is None or not host:
        return host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


# Catalogue of every ``kind`` value the rest of bty-web is allowed
# to pass to :func:`record`. Owned here so the callsites (in
# ``_app.py`` / ``_ui.py`` / the job managers under :mod:`._jobs`)
# and the ``/ui/events`` filter dropdown share one source. Adding
# a new event class is a two-step change: append a constant here,
# then use it at the callsite. :func:`record` no-ops the runtime
# check when a kind is not in this set -- the goal is
# centralisation, not enforcement; we don't want a typo in a logging
# call to crash a request flow.
KNOWN_EVENT_KINDS: tuple[str, ...] = (
    "machine.discovered",
    "machine.created",
    "machine.upserted",
    "machine.deleted",
    "machine.flashed",
    "machine.flash_failed",
    "machine.inventory",
    "netboot.pxe.offered",
    "netboot.pxe.plan",
    "netboot.flasher.armed",
    "pxe.client.orphan",
    "catalog.entry.added",
    "catalog.entry.add.failed",
    "catalog.entry.deleted",
    "catalog.entries.imported",
    "netboot.artifacts.fetched",
    "netboot.artifacts.fetch.requested",
    "netboot.artifacts.fetch.started",
    "netboot.artifacts.fetch.cancelled",
    "netboot.artifacts.fetch.failed",
    "settings.upstream.updated",
    "settings.backup.updated",
    "settings.display.updated",
    "settings.config.updated",
    "settings.config.failed",
    "backup.created",
    "backup.create.requested",
    "backup.create.started",
    "backup.create.cancelled",
    "backup.failed",
    "backup.pruned",
    "backup.deleted",
    "auth.login.succeeded",
    "auth.login.failed",
    "auth.logout",
    "system.schema.reset",
)

# Catalogue of ``subject_kind`` values. Powers the /ui/events
# subject-kind filter dropdown so adding a new subject (say,
# ``token`` or ``backup``) is a one-place change. Like
# ``KNOWN_EVENT_KINDS``, it's a soft catalogue: ``record`` does
# not enforce membership so a typo can't 500 a request.
KNOWN_SUBJECT_KINDS: tuple[str, ...] = (
    "machine",
    "catalog",
    "netboot",
    "settings",
    "auth",
    "backup",
)

# Catalogue of ``actor`` values the rest of bty-web emits. Powers
# the /ui/events actor filter dropdown. Like the other catalogues
# in this module it's a soft list -- callers may pass any string
# and ``record`` won't reject it; the catalogue exists so the UI
# has a fixed set of options to advertise.
KNOWN_ACTORS: tuple[str, ...] = (
    "operator",
    "system",
    "pxe-client",
)


@dataclass(frozen=True)
class Event:
    """One row of the events table, as returned by the listing API."""

    id: int
    ts: str  # ISO 8601 UTC
    kind: str
    subject_kind: str | None
    subject_id: str | None
    actor: str | None
    source_ip: str | None
    summary: str
    details: dict[str, Any] | None
    acknowledged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "actor": self.actor,
            "source_ip": self.source_ip,
            "summary": self.summary,
            "details": self.details,
            "acknowledged": self.acknowledged,
        }


def record(
    conn: sqlite3.Connection,
    *,
    kind: str,
    summary: str,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    actor: str | None = None,
    source_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Insert one event row. Returns the new row id.

    Caller owns the transaction: this does not ``commit`` so the
    record can be batched into the same transaction as the change
    that produced the event (e.g. machine upsert + event in one
    UPDATE / INSERT pair). ``open_db`` does NOT commit on exit (it
    only closes the connection), so a caller that wants the row
    persisted MUST call ``conn.commit()`` itself -- otherwise the
    INSERT is rolled back when the connection closes.

    ``source_ip`` is the IP that initiated / observed the event:
    the operator's request client host for operator events, the
    target's IP at check-in for ``pxe-client`` events. NULL when
    there is no meaningful source IP (e.g. CLI-driven events where
    the bty-web process self-initiates).
    """
    ts = datetime.now(UTC).isoformat()
    details_json = json.dumps(details) if details is not None else None
    cur = conn.execute(
        """
        INSERT INTO events
            (ts, kind, subject_kind, subject_id, actor, source_ip, summary, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, kind, subject_kind, subject_id, actor, source_ip, summary, details_json),
    )
    return int(cur.lastrowid or 0)


RECENT_EVENTS_LIMIT = 10
"""How many rows the embedded "Last N Events" card on each page
renders. Shared so the handler ``limit=`` and the Jinja card
title (rendered as ``Last {{ recent_events_limit }} Events``)
stay in sync; bump this in one place to change everywhere."""


def _q_predicate(q: str) -> tuple[str, list[Any]]:
    """SQL fragment + args matching the free-text events search.

    ``q`` is the operator's plain-text input; matched as a
    case-insensitive substring across the columns that are useful
    for filtering: ``kind``, ``subject_kind``, ``subject_id``,
    ``actor``, ``source_ip``, ``summary``. An empty / whitespace
    ``q`` returns an empty fragment + args so callers can splice
    unconditionally.
    """
    needle = q.strip()
    if not needle:
        return "", []
    like = f"%{needle.lower()}%"
    cols = ("kind", "subject_kind", "subject_id", "actor", "source_ip", "summary")
    clause = "(" + " OR ".join(f"LOWER(IFNULL({c}, '')) LIKE ?" for c in cols) + ")"
    return clause, [like] * len(cols)


def count_events(conn: sqlite3.Connection, *, q: str = "") -> int:
    """Number of events matching the free-text search (or all, when
    ``q`` is empty). Used by /ui/events for offset pagination."""
    clause, args = _q_predicate(q)
    sql = "SELECT COUNT(*) FROM events"
    if clause:
        sql += " WHERE " + clause
    row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def search_events(
    conn: sqlite3.Connection, *, q: str = "", offset: int = 0, limit: int = 50
) -> list[Event]:
    """Offset-paginated event listing with a free-text predicate.

    Powers the /ui/events page after the v0.57 simplification: a
    single search input replaced the dropdown filter set; the
    fields it covers match :func:`_q_predicate`. Newest first
    (``ORDER BY id DESC``) regardless of ``q``."""
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500
    if offset < 0:
        offset = 0
    clause, args = _q_predicate(q)
    sql = "SELECT * FROM events"
    if clause:
        sql += " WHERE " + clause
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    rows = conn.execute(sql, args).fetchall()
    return [_row_to_event(row) for row in rows]


def list_events(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    actor: str | None = None,
    source_ip: str | None = None,
    failed_only: bool = False,
    before_id: int | None = None,
    limit: int = 50,
) -> list[Event]:
    """Cursor-paginated event listing, newest first.

    ``before_id`` returns events with ``id < before_id`` so the UI
    can paginate by carrying the smallest-id-on-the-page through
    "Older" links. Without it, the most recent ``limit`` events
    are returned.

    ``source_ip`` filters to events recorded with that exact
    client IP. Powers the /ui/events "filter by IP" field so an
    operator can pull every change made from a given workstation.

    ``limit`` is clamped to ``[1, 500]`` to avoid pathological
    response sizes from a hand-edited URL; 50 is the UI default.
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    where: list[str] = []
    args: list[Any] = []
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    if subject_kind is not None:
        where.append("subject_kind = ?")
        args.append(subject_kind)
    if subject_id is not None:
        where.append("subject_id = ?")
        args.append(subject_id)
    if actor is not None:
        where.append("actor = ?")
        args.append(actor)
    if source_ip is not None:
        where.append("source_ip = ?")
        args.append(source_ip)
    if failed_only:
        # Every failure kind ends in ``.failed`` (dotted, since
        # v0.33.x normalised the earlier underscore form):
        # ``auth.login.failed``, ``catalog.entry.add.failed``,
        # ``netboot.artifacts.fetch.failed``, ``backup.failed``, etc.
        # ``%failed`` matches the suffix; a stricter check (``%.failed``)
        # would be marginally cleaner but ``failed`` as a full-token
        # suffix has no false positives in the current kind set.
        where.append("kind LIKE ?")
        args.append("%failed")
    if before_id is not None:
        where.append("id < ?")
        args.append(before_id)

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    rows = conn.execute(sql, args).fetchall()
    return [_row_to_event(row) for row in rows]


def _row_to_event(row: sqlite3.Row) -> Event:
    details_raw = row["details"]
    details: dict[str, Any] | None = None
    if details_raw:
        try:
            decoded = json.loads(details_raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            details = decoded
    # ``acknowledged`` is always present after ``init_db`` (the
    # additive migration backfills it), but guard the lookup so a row
    # selected before the migration ran still maps cleanly.
    ack = bool(_db.row_value(row, "acknowledged", False))
    return Event(
        id=row["id"],
        ts=row["ts"],
        kind=row["kind"],
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        actor=row["actor"],
        source_ip=row["source_ip"],
        summary=row["summary"],
        details=details,
        acknowledged=ack,
    )


def set_acknowledged(conn: sqlite3.Connection, event_id: int, value: bool) -> bool:
    """Set one event's acknowledged flag. ``value=True`` acknowledges,
    ``value=False`` clears it (un-acknowledges). Returns ``True`` iff a
    row matched. Caller owns the transaction (mirrors :func:`record`)."""
    cur = conn.execute(
        "UPDATE events SET acknowledged = ? WHERE id = ?",
        (1 if value else 0, event_id),
    )
    return cur.rowcount > 0


def acknowledge_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Mark one event acknowledged. Thin wrapper over
    :func:`set_acknowledged` kept for the JSON ``/events/{id}/ack``
    endpoint and existing callers."""
    return set_acknowledged(conn, event_id, True)


def count_unacknowledged_failures(conn: sqlite3.Connection) -> int:
    """Count failure events the operator has not acknowledged yet.

    Same ``kind LIKE '%failed'`` predicate as ``failed_only`` in
    :func:`list_events`, narrowed to ``acknowledged = 0``. Backs the
    dashboard Health Monitoring error tripwire: acknowledging a
    failure clears it from this count without deleting the row, so
    the operator can mark a known / resolved failure as seen and get
    the panel back to green.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind LIKE '%failed' AND acknowledged = 0"
    ).fetchone()
    return int(row[0])
