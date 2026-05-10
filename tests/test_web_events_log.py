"""Tests for ``bty.web._events_log`` (slim audit-log v0.7.38).

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
