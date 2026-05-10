"""Slim audit log of bty-web activity (v0.7.38).

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
  system-initiated ones (auto-discovery, hash completion). Free-
  form string; ``"operator"``, ``"system"``, ``"pxe-client"`` are
  the conventional values.
- ``details`` is an optional JSON blob with kind-specific extras
  (return code, error text, etc.) -- not surfaced in the table
  view but rendered as a collapsible <details> in the detail
  view.

Retention: append-only, no automatic trimming. The table is small
(a few KB / event) so years of homelab activity fit without
trouble. Operators with strict retention requirements run
``DELETE FROM events WHERE ts < ?`` themselves.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    """One row of the events table, as returned by the listing API."""

    id: int
    ts: str  # ISO 8601 UTC
    kind: str
    subject_kind: str | None
    subject_id: str | None
    actor: str | None
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
    details: dict[str, Any] | None = None,
) -> int:
    """Insert one event row. Returns the new row id.

    Caller owns the transaction: this does not ``commit`` so the
    record can be batched into the same transaction as the change
    that produced the event (e.g. machine upsert + event in one
    UPDATE / INSERT pair). If the caller doesn't manage their own
    transaction, the surrounding ``open_db`` ``conn.commit()`` at
    the end of the with-block flushes the row.
    """
    ts = datetime.now(UTC).isoformat()
    details_json = json.dumps(details) if details is not None else None
    cur = conn.execute(
        """
        INSERT INTO events
            (ts, kind, subject_kind, subject_id, actor, summary, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, kind, subject_kind, subject_id, actor, summary, details_json),
    )
    return int(cur.lastrowid or 0)


def list_events(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[Event]:
    """Cursor-paginated event listing, newest first.

    ``before_id`` returns events with ``id < before_id`` so the UI
    can paginate by carrying the smallest-id-on-the-page through
    "Older" links. Without it, the most recent ``limit`` events
    are returned.

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
        summary=row["summary"],
        details=details,
    )
