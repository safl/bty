"""End-to-end tests for ``bty.web._release_mgr.ReleaseFetchManager``.

The manager wraps :func:`bty.web._releases.fetch_release` in an
asyncio worker pool. Pre-this-file the manager had no dedicated
tests; the 66% coverage on the module came from incidental
exercise via ``/ui/netboot`` integration tests, which don't reach
the failed / cancelled / backfill branches. v0.33.7 shipped a real
URL-construction bug in ``fetch_release`` that the manager would
have propagated unchanged -- this file pins manager-level shape so
a future regression is caught earlier.

The fetch_release function itself is monkeypatched out: this file
tests the MANAGER, not the network fetcher (that's
``test_web_releases.py``). Each test injects a fake that simulates
the verdict and observes how the manager translates it into state
transitions + audit events.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bty.web import _db, _events_log, _releases
from bty.web._release_mgr import (
    ReleaseArtifactState,
    ReleaseFetchManager,
    ReleaseFetchState,
)


def _run(coro: Any) -> Any:
    """Run an async coroutine in a fresh loop. Mirrors the pattern
    in :mod:`test_web_backup_manager`."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _init_state(tmp_path: Path) -> Path:
    """Initialise a state.db and return its path."""
    state_path = tmp_path / "state.db"
    _db.init_db(state_path)
    return state_path


# ---------- enqueue input validation ----------------------------------------


def test_enqueue_rejects_path_traversal_tag(tmp_path: Path) -> None:
    """``enqueue`` raises ValueError for tags that don't match the
    pinned regex. The HTTP layer's Pydantic model also enforces
    this, but a non-API caller (test, future internal use) must not
    be able to smuggle a slash through to the GitHub URL builder.
    """

    async def _drive() -> None:
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot")
        try:
            with pytest.raises(ValueError, match="invalid release tag"):
                await mgr.enqueue("../etc/passwd")
            with pytest.raises(ValueError, match="invalid release tag"):
                await mgr.enqueue("v1.2 with space")
            with pytest.raises(ValueError, match="invalid release tag"):
                await mgr.enqueue("")
        finally:
            await mgr.stop()

    _run(_drive())


def test_enqueue_before_start_raises(tmp_path: Path) -> None:
    """``enqueue`` without ``start`` is a programmer error -- the
    boot_root isn't bound yet so the fetcher would have nowhere to
    write. Surface clearly via RuntimeError rather than crashing
    later in the worker thread."""

    async def _drive() -> None:
        mgr = ReleaseFetchManager(max_parallel=1)
        with pytest.raises(RuntimeError, match="not started"):
            await mgr.enqueue("v1.0.0")

    _run(_drive())


# ---------- happy path + state transitions ----------------------------------


def test_enqueue_runs_to_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake ``fetch_release`` that signals all four artifacts via
    ``on_artifact_start`` then returns a FetchResult must produce
    state.status == "completed" with every per-artifact row also
    completed."""
    fake_base_url = "http://test/releases/download/v1.0.0"

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        on_start = kwargs.get("on_artifact_start")
        progress = kwargs.get("progress")
        # Walk the four artifacts in order, mimicking real fetch_release.
        for name in _releases.ALL_NAMES:
            if on_start is not None:
                on_start(name)
            if progress is not None:
                progress(100, 100)
        return _releases.FetchResult(
            base_url=fake_base_url,
            artifacts=_releases.ALL_NAMES,
            total_bytes=400,
        )

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            await mgr.enqueue("v1.0.0")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            states = await mgr.list()
            assert len(states) == 1
            terminal = states[0]
            assert terminal.status == "completed", terminal.error
            assert terminal.base_url == fake_base_url
            # Every per-artifact row also lands at "completed".
            for art in terminal.artifacts.values():
                assert art.status == "completed", (art.name, art.status)
        finally:
            await mgr.stop()

        # Terminal event lands in the audit log.
        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
        kinds = [e.kind for e in events]
        assert "netboot.artifacts.fetched" in kinds, kinds

    _run(_drive())


def test_enqueue_deduplicates_running_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two enqueues for the same tag while the first is queued /
    running return the SAME state object -- the worker doesn't run
    the fetch twice. Operator double-clicking "Fetch artifacts"
    shouldn't double-fetch."""
    # Block fetch_release on an event so the first enqueue stays
    # "running" while the second arrives.
    block = asyncio.Event()
    started = asyncio.Event()
    loop = asyncio.get_event_loop_policy().new_event_loop()

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        # signal-then-block: tell the test we've started, then
        # block until release.
        loop.call_soon_threadsafe(started.set)
        # Wait synchronously; this is in a worker thread.
        while not block.is_set():
            import time as _t

            _t.sleep(0.005)
        return _releases.FetchResult(
            base_url="http://test/x", artifacts=_releases.ALL_NAMES, total_bytes=0
        )

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot")
        try:
            first = await mgr.enqueue("v1.2.3")
            # Wait for the worker to start the fetch.
            await asyncio.wait_for(started.wait(), timeout=2.0)
            # Second enqueue while the first is running.
            second = await mgr.enqueue("v1.2.3")
            assert first is second, "dedup must return the same state instance"
            # Let the fetch finish.
            block.set()
            for _ in range(200):
                if first.status in ("completed", "failed"):
                    break
                await asyncio.sleep(0.01)
        finally:
            await mgr.stop()

    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_drive())
    finally:
        loop.close()


# ---------- failed branch ----------------------------------------------------


def test_enqueue_fetch_error_lands_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FetchError from fetch_release flips the state to failed
    with the error message preserved + a ``netboot.artifacts.fetch.failed``
    audit event."""

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        raise _releases.FetchError("simulated network error")

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            await mgr.enqueue("v1.2.3")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            states = await mgr.list()
            terminal = states[0]
            assert terminal.status == "failed"
            assert "simulated network error" in (terminal.error or "")
        finally:
            await mgr.stop()

        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
        kinds = [e.kind for e in events]
        assert "netboot.artifacts.fetch.failed" in kinds, kinds

    _run(_drive())


def test_enqueue_unexpected_exception_is_failed_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-FetchError exception (an unexpected programming bug
    surfaced as e.g. KeyError, AttributeError) is caught and turned
    into ``status=failed`` with a typed prefix -- the manager must
    not crash the worker thread on a surprise exception. The
    operator sees a recoverable failure rather than a wedged manager."""

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        raise KeyError("unexpected!")

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot")
        try:
            await mgr.enqueue("v1.2.3")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "failed"
            assert "KeyError" in (terminal.error or "")
        finally:
            await mgr.stop()

    _run(_drive())


# ---------- cancel branch ---------------------------------------------------


def test_enqueue_cancel_lands_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FetchCancelled from the fetcher (operator-pressed Cancel
    flipped the threading.Event between chunks) lands as
    ``status=cancelled`` + NO audit event (cancellation is normal
    control flow, not a failure to log)."""

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        raise _releases.FetchCancelled("cancelled mid-stream")

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            await mgr.enqueue("v1.2.3")
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "cancelled"
            assert terminal.error is None
        finally:
            await mgr.stop()

        with sqlite3.connect(state_path) as conn:
            conn.row_factory = sqlite3.Row
            events = _events_log.list_events(conn, subject_kind="netboot", limit=10)
        kinds = [e.kind for e in events]
        # Cancellation is operator-initiated -- not logged as a fetch event.
        assert "netboot.artifacts.fetch.failed" not in kinds
        assert "netboot.artifacts.fetched" not in kinds


# ---------- cancel-vs-IO-error race ----------------------------------------


def test_cancel_set_while_urllib_in_syscall_is_treated_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cancel flag fires mid-syscall (the operator presses
    Cancel between chunk boundaries while urllib happens to be
    inside its own read), the raised URLError reaches the manager
    BEFORE the next chunk-boundary cancel check translates it.

    The manager checks the cancel event in its except branch and
    treats the URLError as a cancellation in that case -- so the
    operator's Cancel click reliably ends with status=cancelled
    instead of a confusing status=failed-with-URLError."""

    def fake_fetch_release(boot_dir: Path, **kwargs: Any) -> _releases.FetchResult:
        cancel = kwargs.get("cancel")
        # Simulate: operator pressed Cancel, then urllib happened to
        # surface a URLError before the cancel check could translate
        # it into FetchCancelled. The manager looks at the cancel
        # event itself to disambiguate.
        if cancel is not None:
            # Reach into the threading.Event the manager stashed by
            # calling the cancel callable. The caller's _cancel is
            # already accessible via the closure.
            pass
        # Trigger via the state's cancel flag below; here we
        # unconditionally raise a generic FetchError.
        raise _releases.FetchError("URLError: read interrupted")

    monkeypatch.setattr(_releases, "fetch_release", fake_fetch_release)

    async def _drive() -> None:
        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot")
        try:
            state = await mgr.enqueue("v1.2.3")
            # Set the cancel BEFORE the worker thread runs the
            # fetcher. The manager's exception branch reads the flag
            # and re-classifies as cancelled.
            state._cancel.set()
            for _ in range(200):
                states = await mgr.list()
                if states and states[0].status in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.01)
            terminal = (await mgr.list())[0]
            assert terminal.status == "cancelled", (
                f"cancel-set + FetchError must be reported as cancelled, "
                f"got {terminal.status!r} (error={terminal.error!r})"
            )
            assert terminal.error is None
        finally:
            await mgr.stop()

    _run(_drive())


# ---------- backfill --------------------------------------------------------


def test_backfill_restores_completed_state_from_events(tmp_path: Path) -> None:
    """A bty-web restart loses ``_states``; backfill_from_events
    repopulates it from the audit log so /ui/netboot still shows
    "Last fetched X at Y" across restarts."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="netboot.artifacts.fetched",
                summary="boot release 'v1.0.0' fetched",
                subject_kind="netboot",
                subject_id="v1.0.0",
                actor="system",
                details={
                    "tag": "v1.0.0",
                    "base_url": "http://github.com/.../v1.0.0",
                    "total_bytes": 12345,
                },
            )
            conn.commit()

        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].tag == "v1.0.0"
            assert states[0].status == "completed"
            assert states[0].base_url == "http://github.com/.../v1.0.0"
            assert states[0].bytes_total == 12345
        finally:
            await mgr.stop()

    _run(_drive())


def test_backfill_restores_failed_state_from_events(tmp_path: Path) -> None:
    """Failed fetches surface too, with the error preserved so the
    operator can see what went wrong without re-running."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="netboot.artifacts.fetch.failed",
                summary="boot release 'v0.99.0' fetch failed",
                subject_kind="netboot",
                subject_id="v0.99.0",
                actor="operator",
                details={"tag": "v0.99.0", "error": "HTTP 404 from upstream"},
            )
            conn.commit()

        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].tag == "v0.99.0"
            assert states[0].status == "failed"
            assert states[0].error == "HTTP 404 from upstream"
        finally:
            await mgr.stop()

    _run(_drive())


def test_backfill_dedups_to_latest_event_per_tag(tmp_path: Path) -> None:
    """If the same tag has multiple terminal events (operator
    retried after a failure), the NEWEST event wins -- the operator
    sees the latest verdict on /ui/netboot, not a stale failure."""

    async def _drive() -> None:
        state_path = _init_state(tmp_path)
        with _db.open_db(state_path) as conn:
            _events_log.record(
                conn,
                kind="netboot.artifacts.fetch.failed",
                summary="first attempt failed",
                subject_kind="netboot",
                subject_id="v1.0.0",
                actor="operator",
                details={"tag": "v1.0.0", "error": "transient failure"},
            )
            _events_log.record(
                conn,
                kind="netboot.artifacts.fetched",
                summary="retry succeeded",
                subject_kind="netboot",
                subject_id="v1.0.0",
                actor="system",
                details={"tag": "v1.0.0", "base_url": "http://...", "total_bytes": 999},
            )
            conn.commit()

        mgr = ReleaseFetchManager(max_parallel=1)
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            # The retry-success wins over the prior fail-row.
            assert states[0].status == "completed"
            assert states[0].error is None
        finally:
            await mgr.stop()

    _run(_drive())


def test_backfill_soft_fails_on_corrupt_db(tmp_path: Path) -> None:
    """``_backfill_from_events`` swallows any exception so a corrupt
    DB / missing table / etc. doesn't block bty-web startup. The
    backfill is a UX nicety, not a correctness requirement."""

    async def _drive() -> None:
        # state_path that points at a non-DB file (corrupt).
        state_path = tmp_path / "not-a-db"
        state_path.write_bytes(b"garbage")

        mgr = ReleaseFetchManager(max_parallel=1)
        # Must not raise.
        mgr.start(tmp_path / "boot", state_path=state_path)
        try:
            states = await mgr.list()
            assert states == []
        finally:
            await mgr.stop()

    _run(_drive())


# ---------- ReleaseArtifactState shape -------------------------------------


def test_release_artifact_state_to_dict_round_trip() -> None:
    """``to_dict`` is what the API surfaces to the workers UI. Pin
    the field set so a refactor can't silently drop fields the
    /ui/downloads progress bar relies on."""
    art = ReleaseArtifactState(
        name="bty-netboot-x86_64-v1.0.0.vmlinuz",
        status="running",
        bytes_done=42,
        bytes_total=100,
    )
    d = art.to_dict()
    assert d == {
        "name": "bty-netboot-x86_64-v1.0.0.vmlinuz",
        "status": "running",
        "bytes_done": 42,
        "bytes_total": 100,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


def test_release_fetch_state_to_dict_includes_artifacts() -> None:
    """The parent ReleaseFetchState.to_dict carries the artifacts
    sub-array so a single workers-API hit returns the per-file
    rows the UI needs without a second roundtrip."""
    state = ReleaseFetchState(tag="v1.0.0", status="running")
    state.artifacts["a"] = ReleaseArtifactState(name="a")
    state.artifacts["b"] = ReleaseArtifactState(name="b")
    d = state.to_dict()
    assert d["tag"] == "v1.0.0"
    assert d["status"] == "running"
    assert {a["name"] for a in d["artifacts"]} == {"a", "b"}


# ---------- safety: empty fixture path is callable -------------------------


@pytest.fixture
def _unused_iterator() -> Iterator[None]:
    """Placeholder for parity with sibling test files that yield
    HTTP servers / temp state. This file's tests use monkeypatch
    + tmp_path directly so no fixture is needed; the import-time
    presence of this stub keeps the file pattern consistent."""
    yield None
