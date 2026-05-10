"""Tests for ``bty.web._events_log`` (slim audit log).

Covers the recording helper + the listing API + the cursor
pagination invariant (newer rows have larger ids; ``before_id``
returns rows with smaller ids).
"""

from __future__ import annotations

from pathlib import Path

from bty.web import _db, _events_log


def _open(state_path: Path):
    """Helper: ``_db.open_db`` is a contextmanager; this returns the
    opened conn for the test to use, plus a closer to call at
    teardown so the open isn't tied to the with-block scope."""
    cm = _db.open_db(state_path)
    conn = cm.__enter__()
    return conn, lambda: cm.__exit__(None, None, None)


def test_normalize_ip() -> None:
    """``normalize_ip`` collapses v4-mapped-v6 (the form Starlette
    returns when bty-web binds dual-stack and a v4 client connects)
    into the bare v4 form, leaves real v4 / v6 untouched, and
    passes through unrecognised inputs (e.g. unix socket paths).

    Without this normalisation, the same workstation behind a
    ``::ffff:`` mapping and through a v4-only socket would record
    under two different IPs and split the audit trail across them.
    """
    # v4-mapped v6 -> bare v4 (the bug case).
    assert _events_log.normalize_ip("::ffff:192.168.1.42") == "192.168.1.42"
    # Bare v4 stays bare v4.
    assert _events_log.normalize_ip("192.168.1.42") == "192.168.1.42"
    # Real v6 returns the compressed canonical form.
    assert _events_log.normalize_ip("2001:0db8:0000::1") == "2001:db8::1"
    assert _events_log.normalize_ip("::1") == "::1"
    # None / empty pass through unchanged.
    assert _events_log.normalize_ip(None) is None
    assert _events_log.normalize_ip("") == ""
    # Garbage / non-IP transports flow through unchanged so the
    # audit log doesn't drop unusual sources silently.
    assert _events_log.normalize_ip("not-an-ip") == "not-an-ip"


def test_record_returns_monotonic_id(tmp_path: Path) -> None:
    """The ``id`` column is AUTOINCREMENT, so successive records
    return strictly-increasing ids. Cursor pagination relies on
    this invariant."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        first = _events_log.record(
            conn,
            kind="machine.discovered",
            summary="aa:bb:cc:dd:ee:ff first /pxe contact",
            subject_kind="machine",
            subject_id="aa:bb:cc:dd:ee:ff",
            actor="pxe-client",
        )
        second = _events_log.record(
            conn,
            kind="machine.upserted",
            summary="aa:bb:cc:dd:ee:ff updated by operator",
            subject_kind="machine",
            subject_id="aa:bb:cc:dd:ee:ff",
            actor="operator",
        )
        conn.commit()
    finally:
        close()
    assert first >= 1
    assert second > first


def test_list_returns_newest_first(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(conn, kind="machine.discovered", summary="first")
        _events_log.record(conn, kind="machine.upserted", summary="second")
        _events_log.record(conn, kind="machine.deleted", summary="third")
        conn.commit()
        rows = _events_log.list_events(conn, limit=10)
    finally:
        close()
    assert [r.summary for r in rows] == ["third", "second", "first"]


def test_list_filters_by_kind(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(conn, kind="machine.discovered", summary="m1")
        _events_log.record(conn, kind="image.uploaded", summary="i1")
        _events_log.record(conn, kind="machine.flashed", summary="m2")
        conn.commit()
        rows = _events_log.list_events(conn, kind="image.uploaded")
    finally:
        close()
    assert [r.summary for r in rows] == ["i1"]


def test_list_filters_by_subject(tmp_path: Path) -> None:
    """``subject_kind=machine subject_id=<mac>`` powers the per-MAC
    embedded card on /ui/machines/{mac}."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        for mac in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"):
            _events_log.record(
                conn,
                kind="machine.discovered",
                summary=f"{mac} first contact",
                subject_kind="machine",
                subject_id=mac,
            )
        conn.commit()
        rows = _events_log.list_events(
            conn,
            subject_kind="machine",
            subject_id="aa:bb:cc:dd:ee:01",
        )
    finally:
        close()
    assert len(rows) == 1
    assert rows[0].subject_id == "aa:bb:cc:dd:ee:01"


def test_list_filters_by_source_ip(tmp_path: Path) -> None:
    """``source_ip=<ip>`` returns only rows recorded with that IP, so
    the /ui/events filter pivot lands on a clean slice."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(
            conn, kind="machine.upserted", summary="from .42", source_ip="192.168.1.42"
        )
        _events_log.record(
            conn, kind="machine.upserted", summary="from .55", source_ip="192.168.1.55"
        )
        _events_log.record(conn, kind="machine.task.completed", summary="system, no IP")
        conn.commit()
        rows = _events_log.list_events(conn, source_ip="192.168.1.42")
    finally:
        close()
    assert [r.summary for r in rows] == ["from .42"]


def test_list_cursor_pagination(tmp_path: Path) -> None:
    """``before_id`` returns rows older than the cursor, newest
    first. The "Older" link on /ui/events plumbs the smallest-id
    on the current page through this param."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        ids = [
            _events_log.record(conn, kind="machine.discovered", summary=f"e{i}") for i in range(5)
        ]
        conn.commit()
        # First page: limit=2 -> the two newest.
        page1 = _events_log.list_events(conn, limit=2)
        assert [e.id for e in page1] == [ids[4], ids[3]]
        # Second page: before_id = smallest on page 1.
        page2 = _events_log.list_events(conn, limit=2, before_id=page1[-1].id)
        assert [e.id for e in page2] == [ids[2], ids[1]]
        # Third (partial) page.
        page3 = _events_log.list_events(conn, limit=2, before_id=page2[-1].id)
        assert [e.id for e in page3] == [ids[0]]
    finally:
        close()


def test_list_clamps_limit(tmp_path: Path) -> None:
    """Hand-edited ``?limit=99999`` URL clamps to 500 server-side."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        # Below floor.
        rows = _events_log.list_events(conn, limit=-5)
        assert rows == []  # still works (clamped to 1)
        # Above cap: insert 600 rows, ask for 1000, get 500.
        for i in range(600):
            _events_log.record(conn, kind="image.hashed", summary=f"e{i}")
        conn.commit()
        rows = _events_log.list_events(conn, limit=1000)
    finally:
        close()
    assert len(rows) == 500


def test_source_ip_round_trip(tmp_path: Path) -> None:
    """``source_ip`` is persisted on write and surfaced on read, so the
    /ui/events table can show which IP made the change. ``None`` is
    valid (system-initiated events with no meaningful source).
    """
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(
            conn,
            kind="machine.discovered",
            summary="from PXE client",
            actor="pxe-client",
            source_ip="192.168.1.42",
        )
        _events_log.record(
            conn,
            kind="machine.upserted",
            summary="from operator browser",
            actor="operator",
            source_ip="10.0.0.5",
        )
        _events_log.record(
            conn,
            kind="machine.task.completed",
            summary="system event with no source",
            actor="system",
            # source_ip omitted -> NULL
        )
        conn.commit()
        rows = _events_log.list_events(conn, limit=10)
    finally:
        close()
    by_kind = {r.kind: r for r in rows}
    assert by_kind["machine.discovered"].source_ip == "192.168.1.42"
    assert by_kind["machine.upserted"].source_ip == "10.0.0.5"
    assert by_kind["machine.task.completed"].source_ip is None


def test_details_round_trip(tmp_path: Path) -> None:
    """``details`` is JSON-encoded on write and decoded on read; a
    malformed JSON blob (manually corrupted state.db) decodes to
    ``None`` rather than raising."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(
            conn,
            kind="image.uploaded",
            summary="upload",
            details={"size_bytes": 12345, "name": "demo.qcow2"},
        )
        # Manually insert a row with malformed JSON details.
        conn.execute(
            "INSERT INTO events (ts, kind, summary, details) VALUES (?, ?, ?, ?)",
            ("2026-05-10T00:00:00+00:00", "junk", "junk", "not-json{{"),
        )
        conn.commit()
        rows = _events_log.list_events(conn, limit=10)
    finally:
        close()
    by_kind = {r.kind: r for r in rows}
    assert by_kind["image.uploaded"].details == {"size_bytes": 12345, "name": "demo.qcow2"}
    assert by_kind["junk"].details is None  # malformed -> None, not crash
