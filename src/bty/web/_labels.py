"""Per-machine labels persistence (the ``machine_labels`` table).

Replaced the singular ``machines.hostname`` (RFC-1123-shaped) column
in v0.58.0. Labels are operator-curated tags; the boot chain never
reads them. The "set of strings per MAC" shape lives in its own table
so adding / removing a tag is one row instead of a JSON re-encode +
write of the whole machines row.

The :func:`set_labels` writer is set-semantic: it replaces a machine's
labels wholesale. Operators rarely add one label without touching the
others; the form posts the full set every time. The reader is also
deterministic (alphabetical) so the chip order on the page is stable.
"""

from __future__ import annotations

import sqlite3


def get_labels(conn: sqlite3.Connection, mac: str) -> list[str]:
    """Return the labels applied to ``mac``, alphabetically.

    ``None`` is never returned: a machine with no labels yields an
    empty list. Sorted at read time so the chip rendering is
    deterministic without a per-row sort in the template.
    """
    rows = conn.execute(
        "SELECT label FROM machine_labels WHERE mac = ? ORDER BY label",
        (mac,),
    ).fetchall()
    return [r[0] for r in rows]


def set_labels(conn: sqlite3.Connection, mac: str, labels: list[str]) -> None:
    """Replace ``mac``'s labels with ``labels`` (set semantics).

    Caller is responsible for validating each label's shape (the
    Pydantic ``MachineUpsert.labels`` constraint covers the JSON API
    + UI form paths). Duplicate entries in ``labels`` are deduped by
    the table's PRIMARY KEY (mac, label) -- an ``INSERT OR IGNORE``
    keeps the first one, drops the rest.
    """
    conn.execute("DELETE FROM machine_labels WHERE mac = ?", (mac,))
    for label in labels:
        conn.execute(
            "INSERT OR IGNORE INTO machine_labels (mac, label) VALUES (?, ?)",
            (mac, label),
        )


def delete_labels(conn: sqlite3.Connection, mac: str) -> None:
    """Drop every label for ``mac``. Called from the machine-delete
    paths to clean up after the ``machines`` row is removed -- sqlite
    isn't running with foreign-key enforcement, so the cascade is
    explicit.
    """
    conn.execute("DELETE FROM machine_labels WHERE mac = ?", (mac,))


def parse_form_value(raw: str) -> list[str]:
    """Parse the form-side comma-separated input into a list of
    trimmed, deduplicated labels (case-insensitive dedup; original
    casing of the first occurrence is preserved).

    Empty input -> empty list. Whitespace-only tokens are dropped
    (a stray trailing comma in ``"a, b, "`` would otherwise land an
    invalid empty label and 422 at validation time). The Pydantic
    pattern still rejects malformed tokens; this only handles the
    "what does the form encoding look like" bit.
    """
    seen_lower: set[str] = set()
    out: list[str] = []
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        out.append(cleaned)
    return out
