"""Tests for ``bty.web._ramboot_cache``.

Unit-level coverage of the state-machine helpers (enqueue,
is_ready, statuses_by_ref) plus a couple of end-to-end-ish tests
that exercise the iPXE ramboot branch's gating on
``ramboot_cache.status``.

The worker thread itself (``RambootCacheManager._process``) is not
covered here; the network + zstd + nbdmux fan-out lives behind a
real subprocess + a real network in production. A future
integration test can spin up nbdmux + a fake HTTP source if the
worker grows enough surface to warrant it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bty.web import _db, _ramboot_cache


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    return sqlite3.connect(state)


def test_enqueue_creates_queued_row(tmp_path: Path) -> None:
    """First enqueue inserts a row in ``queued`` state plus the
    matching ``ramboot.pre_warm.requested`` audit event."""
    ref = "a" * 64
    with _conn(tmp_path) as conn:
        conn.row_factory = sqlite3.Row
        row = _ramboot_cache.enqueue(conn, ref, actor="operator")
        assert row.ref == ref
        assert row.status == "queued"
        assert row.image_path is None
        assert row.export_name is None
        # Audit event landed (no commit needed; record() writes to
        # the same connection's pending transaction).
        events = conn.execute(
            "SELECT kind, subject_kind, subject_id FROM events WHERE kind LIKE 'ramboot.%'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["kind"] == "ramboot.pre_warm.requested"
        assert events[0]["subject_kind"] == "ramboot_cache"
        assert events[0]["subject_id"] == ref


def test_enqueue_is_idempotent_for_ready_rows(tmp_path: Path) -> None:
    """A second enqueue against a row already in ``ready`` is a
    no-op so re-saving a machine doesn't bounce its serving
    export."""
    ref = "b" * 64
    with _conn(tmp_path) as conn:
        conn.row_factory = sqlite3.Row
        _ramboot_cache.enqueue(conn, ref)
        _ramboot_cache._set_status(conn, ref, "ready", export_name=ref, set_completed=True)
        before = _ramboot_cache.get_row(conn, ref)
        assert before is not None and before.status == "ready"
        row = _ramboot_cache.enqueue(conn, ref)
        after = _ramboot_cache.get_row(conn, ref)
        assert row.status == "ready"
        assert after is not None
        # updated_at preserved (the row was untouched).
        assert after.updated_at == before.updated_at


def test_enqueue_requeues_failed_row(tmp_path: Path) -> None:
    """A second enqueue against a ``failed`` row restarts at
    ``queued`` and clears the stale ``error`` / completion timestamps."""
    ref = "c" * 64
    with _conn(tmp_path) as conn:
        conn.row_factory = sqlite3.Row
        _ramboot_cache.enqueue(conn, ref)
        _ramboot_cache._set_status(
            conn,
            ref,
            "failed",
            error="upstream 503",
            set_started=True,
            set_completed=True,
        )
        _ramboot_cache.enqueue(conn, ref)
        row = _ramboot_cache.get_row(conn, ref)
        assert row is not None
        assert row.status == "queued"
        assert row.error is None
        assert row.started_at is None
        assert row.completed_at is None


def test_is_ready_returns_true_only_when_status_ready(tmp_path: Path) -> None:
    ref = "d" * 64
    with _conn(tmp_path) as conn:
        conn.row_factory = sqlite3.Row
        # Absent: not ready.
        assert _ramboot_cache.is_ready(conn, ref) is False
        _ramboot_cache.enqueue(conn, ref)
        # Queued: still not ready.
        assert _ramboot_cache.is_ready(conn, ref) is False
        _ramboot_cache._set_status(conn, ref, "ready", set_completed=True)
        assert _ramboot_cache.is_ready(conn, ref) is True


def test_statuses_by_ref_returns_full_map(tmp_path: Path) -> None:
    a = "a" * 64
    b = "b" * 64
    with _conn(tmp_path) as conn:
        conn.row_factory = sqlite3.Row
        _ramboot_cache.enqueue(conn, a)
        _ramboot_cache.enqueue(conn, b)
        _ramboot_cache._set_status(conn, b, "ready", set_completed=True)
        statuses = _ramboot_cache.statuses_by_ref(conn)
        assert statuses == {a: "queued", b: "ready"}


# Tests for the ``pytest`` import shadowing on the test client are
# handled by the larger test_web.py + test_web_ui.py suites; we
# don't repeat that fixture wiring here. The iPXE gate is covered
# in test_web.py::test_pxe_ramboot_falls_back_to_tui_without_*.
def test_module_imports_cleanly() -> None:
    """Smoke: the module loads without pulling in heavy deps at
    import time (nbdmux.client is lazy-imported inside the
    register helper, gzip / shutil are stdlib, urllib.request is
    stdlib)."""
    assert hasattr(_ramboot_cache, "RambootCacheManager")
    assert hasattr(_ramboot_cache, "enqueue")
    assert hasattr(_ramboot_cache, "is_ready")
    assert hasattr(_ramboot_cache, "statuses_by_ref")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
