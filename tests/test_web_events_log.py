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
        _events_log.record(conn, kind="catalog.entries.imported", summary="i1")
        _events_log.record(conn, kind="machine.flashed", summary="m2")
        conn.commit()
        rows = _events_log.list_events(conn, kind="catalog.entries.imported")
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


def test_list_filters_by_actor(tmp_path: Path) -> None:
    """``actor=<actor>`` returns only rows recorded with that actor,
    powering the /ui/events ``Actor`` dropdown for triage views
    like 'show me everything operators did'."""
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        _events_log.record(conn, kind="machine.upserted", summary="from operator", actor="operator")
        _events_log.record(conn, kind="machine.discovered", summary="from pxe", actor="pxe-client")
        _events_log.record(conn, kind="machine.flashed", summary="from system", actor="system")
        conn.commit()
        rows = _events_log.list_events(conn, actor="operator")
    finally:
        close()
    assert [r.summary for r in rows] == ["from operator"]


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
        _events_log.record(conn, kind="machine.flashed", summary="system, no IP")
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
            _events_log.record(conn, kind="machine.discovered", summary=f"e{i}")
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
            kind="machine.flashed",
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
    assert by_kind["machine.flashed"].source_ip is None


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
            kind="machine.discovered",
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
    assert by_kind["machine.discovered"].details == {"size_bytes": 12345, "name": "demo.qcow2"}
    assert by_kind["junk"].details is None  # malformed -> None, not crash


def test_acknowledge_event_and_unacked_failure_count(tmp_path: Path) -> None:
    """``acknowledge_event`` flips the flag on one row;
    ``count_unacknowledged_failures`` counts only ``%failed`` kinds
    with ``acknowledged = 0`` -- the predicate behind the dashboard
    Health Monitoring error tripwire. Acking a failure decrements the
    count without deleting the row; acking a non-failure leaves the
    count untouched.
    """
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn, close = _open(state)
    try:
        fail1 = _events_log.record(conn, kind="image.hash.failed", summary="boom 1")
        fail2 = _events_log.record(conn, kind="netboot.artifacts.fetch.failed", summary="boom 2")
        ok = _events_log.record(conn, kind="machine.flashed", summary="fine")
        conn.commit()
        # New events default to unacknowledged.
        assert all(not e.acknowledged for e in _events_log.list_events(conn))
        assert _events_log.count_unacknowledged_failures(conn) == 2
        # Acking one failure drops the tripwire count by one.
        assert _events_log.acknowledge_event(conn, fail1) is True
        conn.commit()
        assert _events_log.count_unacknowledged_failures(conn) == 1
        by_id = {e.id: e for e in _events_log.list_events(conn)}
        assert by_id[fail1].acknowledged is True
        assert by_id[fail2].acknowledged is False
        # Acking a non-failure event is a no-op for the tripwire.
        assert _events_log.acknowledge_event(conn, ok) is True
        conn.commit()
        assert _events_log.count_unacknowledged_failures(conn) == 1
        # Acking an unknown id changes nothing.
        assert _events_log.acknowledge_event(conn, 999999) is False
    finally:
        close()


def test_known_actors_covers_every_actor_emitted_by_the_codebase() -> None:
    """Every ``actor="..."`` literal in `_log_event` calls across
    `src/bty/` must be present in `KNOWN_ACTORS`.

    Powers the /ui/events ``actor`` filter dropdown -- an actor
    that's emitted but missing here is invisible to operator
    filtering. Completes the trio with KNOWN_EVENT_KINDS +
    KNOWN_SUBJECT_KINDS meta-tests so the three taxonomies all
    drift-check against the source tree.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src" / "bty"
    pattern = re.compile(r'actor="([a-z][a-z0-9_-]+)"')
    found: set[str] = set()
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        for match in pattern.findall(text):
            found.add(match)
    missing = sorted(found - set(_events_log.KNOWN_ACTORS))
    assert not missing, (
        "KNOWN_ACTORS is missing actors emitted by the codebase. "
        f"Add to _events_log.KNOWN_ACTORS: {missing}"
    )


def test_known_subject_kinds_covers_every_kind_emitted_by_the_codebase() -> None:
    """Same shape as the KNOWN_EVENT_KINDS check: every
    ``subject_kind="..."`` literal in `_log_event` calls across
    `src/bty/` must be present in `KNOWN_SUBJECT_KINDS`.

    Powers the /ui/events filter dropdown's "Subject kind" select.
    A subject kind that's emitted but missing here is invisible
    to the operator filtering by subject.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src" / "bty"
    pattern = re.compile(r'subject_kind="([a-z][a-z0-9_]+)"')
    found: set[str] = set()
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        for match in pattern.findall(text):
            found.add(match)
    missing = sorted(found - set(_events_log.KNOWN_SUBJECT_KINDS))
    assert not missing, (
        "KNOWN_SUBJECT_KINDS is missing subject kinds emitted by the codebase. "
        f"Add to _events_log.KNOWN_SUBJECT_KINDS: {missing}"
    )


def test_known_event_kinds_covers_every_kind_emitted_by_the_codebase() -> None:
    """Every ``kind="..."`` literal used in `_log_event` calls
    across `src/bty/` must be present in `KNOWN_EVENT_KINDS`.

    ``KNOWN_EVENT_KINDS`` powers the /ui/events filter dropdown;
    a kind that's emitted but not in the catalogue still records
    fine (the check at write time is a no-op) but is invisible
    in the dropdown -- the operator can't filter to it without
    knowing the exact string. This test guards the
    catalogue against drift by grepping the source tree.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src" / "bty"
    pattern = re.compile(r'kind="([a-z][a-z0-9._]+)"')
    found: set[str] = set()
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        for match in pattern.findall(text):
            # ``kind=`` strings used in template / status badges
            # (success / danger / url) shouldn't be mistaken for
            # event kinds. Filter to dotted namespaces, which is
            # the convention.
            if "." in match:
                found.add(match)
    missing = sorted(found - set(_events_log.KNOWN_EVENT_KINDS))
    assert not missing, (
        "KNOWN_EVENT_KINDS is missing kinds emitted by the codebase. "
        f"Add to _events_log.KNOWN_EVENT_KINDS: {missing}"
    )


# --------------------------------------------------------------------------
# Reverse-direction drift checks: every entry in the KNOWN_* catalogues
# must actually be emitted somewhere. Catches stale entries left behind
# by feature removal (round 16 caught ``bty-web`` lingering in
# KNOWN_ACTORS after the actor was retired).
# --------------------------------------------------------------------------


def _broad_attr_scan(attr: str) -> set[str]:
    """Collect every double-quoted string on lines containing
    ``<attr>=``. Wider than ``re.findall(r'<attr>="..."')`` because
    that misses the conditional-expression form
    ``kind="A" if cond else "B"`` (the regex only catches the first
    literal). Reverse drift checks need to count BOTH branches as
    emitted, so we lean wide and filter by the catalogue.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src" / "bty"
    found: set[str] = set()
    needle = attr + "="
    for path in src_root.rglob("*.py"):
        for line in path.read_text().splitlines():
            if needle not in line:
                continue
            for match in re.findall(r'"([a-z][a-z0-9._-]*)"', line):
                found.add(match)
    return found


def test_known_event_kinds_has_no_unused_entries() -> None:
    """Every entry in ``KNOWN_EVENT_KINDS`` must correspond to a
    real emit call somewhere in ``src/bty/``. A stale entry would
    surface in the /ui/events kind dropdown but nothing in the
    audit log would ever match it -- operator confusion + a hint
    that a feature was retired without a doc / code pass.
    """
    emitted = _broad_attr_scan("kind")
    unused = sorted(set(_events_log.KNOWN_EVENT_KINDS) - emitted)
    assert not unused, (
        "KNOWN_EVENT_KINDS contains entries that no _log_event call emits. "
        f"Drop from _events_log.KNOWN_EVENT_KINDS: {unused}"
    )


def test_known_subject_kinds_has_no_unused_entries() -> None:
    """Reverse direction of the existing forward check. Same drift
    class -- a subject kind retired in code but not the catalogue
    would appear in the filter dropdown with no matches.
    """
    emitted = _broad_attr_scan("subject_kind")
    unused = sorted(set(_events_log.KNOWN_SUBJECT_KINDS) - emitted)
    assert not unused, (
        "KNOWN_SUBJECT_KINDS contains entries that no _log_event call emits. "
        f"Drop from _events_log.KNOWN_SUBJECT_KINDS: {unused}"
    )


def test_known_actors_has_no_unused_entries() -> None:
    """Reverse direction. The auth events emit ``actor=service_user``
    (a dynamic value -- the OS account name, not a string literal),
    so this test allows those to escape the scan; only literal
    ``actor="..."`` strings count for emission. KNOWN_ACTORS must
    enumerate only literals that bty-web actually emits.
    """
    emitted = _broad_attr_scan("actor")
    unused = sorted(set(_events_log.KNOWN_ACTORS) - emitted)
    assert not unused, (
        "KNOWN_ACTORS contains entries that no _log_event call emits. "
        f"Drop from _events_log.KNOWN_ACTORS: {unused}"
    )
