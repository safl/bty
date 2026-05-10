"""Slim audit log of bty-web activity.

A single append-only ``events`` table in state.db captures the
"who did what when" timeline that the operator wants visible in
the UI: machines checking in, configuration changes, image
uploads, task lifecycles, etc. Three rendering surfaces consume
the same rows:

1. ``/ui/events`` -- top-level page with filter + pagination.
2. ``/ui/machines/{mac}`` -- the most recent events touching this
   MAC, embedded in the machine-detail card.
3. ``/ui/images`` -- the most recent events touching the image
   catalog (uploads, catalog adds / deletes, hash completions).

Conventions:

- ``kind`` is a dotted lowercase namespace, e.g.
  ``machine.discovered``, ``machine.flashed``, ``image.uploaded``,
  ``catalog.entry.added``. Stable strings; the UI keys badge
  colours off them.
- ``subject_kind`` + ``subject_id`` together identify the entity
  the event is about. ``machine`` / mac, ``image`` / sha or name,
  ``catalog`` / src URL, ``boot`` / release-tag, ``settings`` /
  panel-name. Either may be ``None`` for global events
  (``settings.pxe.activated``).
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
# ``_app.py`` / ``_task.py`` / ``_ui.py``) and the ``/ui/events``
# filter dropdown share one source. Adding a new event class is a
# two-step change: append a constant here, then use it at the
# callsite. :func:`record` no-ops the runtime check when a kind is
# not in this set -- the goal is centralisation, not enforcement;
# we don't want a typo in a logging call to crash a request flow.
KNOWN_EVENT_KINDS: tuple[str, ...] = (
    "machine.discovered",
    "machine.created",
    "machine.upserted",
    "machine.deleted",
    "machine.flashed",
    "machine.task.running",
    "machine.task.completed",
    "machine.task.cancelled",
    "machine.task.failed",
    "image.uploaded",
    "image.upload_failed",
    "image.hashed",
    "image.hash_failed",
    "catalog.entry.added",
    "catalog.entry.add_failed",
    "catalog.entry.deleted",
    "boot.release.fetched",
    "boot.release.fetch_failed",
    "settings.pxe.activated",
    "settings.pxe.activate_failed",
    "auth.login.succeeded",
    "auth.login.failed",
    "auth.logout",
)

# Catalogue of ``subject_kind`` values. Powers the /ui/events
# subject-kind filter dropdown so adding a new subject (say,
# ``token`` or ``backup``) is a one-place change. Like
# ``KNOWN_EVENT_KINDS``, it's a soft catalogue: ``record`` does
# not enforce membership so a typo can't 500 a request.
KNOWN_SUBJECT_KINDS: tuple[str, ...] = (
    "machine",
    "image",
    "catalog",
    "boot",
    "settings",
    "auth",
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
    UPDATE / INSERT pair). If the caller doesn't manage their own
    transaction, the surrounding ``open_db`` ``conn.commit()`` at
    the end of the with-block flushes the row.

    ``source_ip`` is the IP that initiated / observed the event:
    the operator's request client host for operator events, the
    target's IP at check-in for ``pxe-client`` events, and the
    target's ``last_seen_ip`` for task-runner system events.
    NULL when there is no meaningful source IP (e.g. CLI-driven
    events where the bty-web process self-initiates).
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
        # Failure kinds end either in ``.failed`` (e.g.
        # ``machine.task.failed``) or ``_failed`` (e.g.
        # ``image.hash_failed``). LIKE matches both via the
        # ``%failed`` suffix; a stricter check would need an
        # OR of two LIKE clauses but the simpler form is
        # sufficient here -- ``failed`` is rare enough as a
        # full-token suffix that false positives are unlikely.
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
    )
