"""Tests for ``bty.web._task.TaskManager``.

Subprocess invocations of cijoe are mocked - a real cijoe binary
isn't installed in the dev env's ``[web]`` extras, and we don't
want the test suite to depend on the network either way. Each test
seeds an in-memory machine record, drives ``_run`` synchronously
(no thread spawn) on a constructed :class:`TaskState`, and asserts
the resulting state-dict + DB shape.

Status vocabulary mirrors the other manager-driven jobs (hashes,
downloads, release-fetches): ``running`` / ``completed`` /
``cancelled`` / ``failed``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bty.web import _db
from bty.web._task import TaskManager, TaskState


def _seed_machine(state_path: Path, mac: str = "aa:bb:cc:dd:ee:ff") -> None:
    """Insert a machine row so the runner's UPDATEs have a target."""
    with _db.open_db(state_path) as conn:
        conn.execute(
            """
            INSERT INTO machines
                (mac, provisioning_mode, boot_policy, created_at, updated_at)
            VALUES (?, 'cijoe-task', 'flash', ?, ?)
            """,
            (mac, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()


@pytest.fixture
def runner(tmp_path: Path):
    state = tmp_path / "state.db"
    _db.init_db(state)
    publishes: list[None] = []
    runner = TaskManager(
        state_path=state,
        publish_machines_changed=lambda: publishes.append(None),
        tasks_dir=tmp_path / "tasks",
        ssh_key_path=tmp_path / "key",
        cijoe_bin="cijoe-fake",
    )
    return runner, state, publishes


def _fake_proc(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    """Build a Popen-shape mock that ``_run`` can call ``communicate`` on."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _seed_state(mac: str = "aa:bb:cc:dd:ee:ff") -> TaskState:
    return TaskState(
        mac=mac,
        task_ref="/path/to/wf.yaml",
        target_ip="10.0.0.5",
    )


# ---------- _run synchronous path ------------------------------------------


def test_task_manager_records_completed(runner) -> None:
    runner_obj, state_db, publishes = runner
    _seed_machine(state_db)

    proc = _fake_proc(0, stdout="ok\n")
    with patch("bty.web._task.subprocess.Popen", return_value=proc):
        runner_obj._run(_seed_state())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status, last_task_run_at, last_task_output_path "
            "FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "completed"
    assert row["last_task_run_at"] is not None
    assert row["last_task_output_path"]
    out_dir = Path(row["last_task_output_path"])
    assert (out_dir / "cijoe.stdout").read_text() == "ok\n"
    # Two SSE publishes: running, then completed.
    assert len(publishes) == 2


def test_task_manager_records_failed_on_nonzero_exit(runner) -> None:
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    proc = _fake_proc(2, stderr="boom\n")
    with patch("bty.web._task.subprocess.Popen", return_value=proc):
        runner_obj._run(_seed_state())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "failed"


def test_task_manager_records_failed_on_timeout(runner) -> None:
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = -15  # SIGTERM after our terminate()
    # Two communicate calls: first raises TimeoutExpired (the
    # main wait), second returns the buffered output (the post-
    # terminate cleanup wait).
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd=["cijoe"], timeout=10),
        ("partial-stdout\n", "partial-stderr\n"),
    ]
    with patch("bty.web._task.subprocess.Popen", return_value=proc):
        runner_obj._run(_seed_state())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status, last_task_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "failed"
    assert (
        (Path(row["last_task_output_path"]) / "error.txt").read_text().startswith("cijoe timed out")
    )


def test_task_manager_records_failed_on_missing_binary(runner) -> None:
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    err = FileNotFoundError("[Errno 2] No such file: 'cijoe-fake'")
    with patch("bty.web._task.subprocess.Popen", side_effect=err):
        runner_obj._run(_seed_state())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status, last_task_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "failed"
    assert (
        "cijoe binary not found" in (Path(row["last_task_output_path"]) / "error.txt").read_text()
    )


# ---------- transport config ------------------------------------------------


def test_task_manager_renders_transport_config(runner) -> None:
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    proc = _fake_proc(0)
    with patch("bty.web._task.subprocess.Popen", return_value=proc):
        runner_obj._run(_seed_state())

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    config = (Path(row["last_task_output_path"]) / "transport.toml").read_text()
    assert 'hostname = "10.0.0.5"' in config
    assert "cijoe.transport.ssh" in config
    assert 'username = "root"' in config


# ---------- subprocess invocation -------------------------------------------


def test_task_manager_invokes_cijoe_with_task_and_config(runner) -> None:
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    proc = _fake_proc(0)
    with patch("bty.web._task.subprocess.Popen", return_value=proc) as mock_popen:
        runner_obj._run(_seed_state())

    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == "cijoe-fake"
    assert cmd[1] == "/path/to/wf.yaml"
    assert "--config" in cmd
    assert "--monitor" in cmd
    # Subprocess runs with cwd set to the run dir so cijoe's
    # cijoe-output/ lands there.
    assert kwargs["cwd"]


def test_task_manager_layers_user_config_when_present(tmp_path: Path) -> None:
    """v0.7.39: bty-web always provides the SSH transport config,
    but the operator can drop a ``cijoe-user-config.toml`` next to
    state.db with their own task-specific knobs / additional named
    transports. cijoe accepts ``--config`` repeatedly; bty-web
    layers them as ``[user-config, transport.toml]`` so the SSH
    transport in ``transport.toml`` (loaded LAST) overrides any
    redefinition the operator might have put in their config.

    This test pins the ordering: user-config first, transport
    last.
    """
    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    _seed_machine(state_db)

    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text("[cijoe.workflow]\nfail_fast = false\n")

    runner_obj = TaskManager(
        state_path=state_db,
        publish_machines_changed=lambda: None,
        tasks_dir=tmp_path / "tasks",
        ssh_key_path=tmp_path / "key",
        cijoe_bin="cijoe-fake",
        user_config_path=user_cfg,
    )

    proc = _fake_proc(0)
    with patch("bty.web._task.subprocess.Popen", return_value=proc) as mock_popen:
        runner_obj._run(_seed_state())

    cmd = mock_popen.call_args.args[0]
    # Two ``--config`` flags should appear, in this order: user
    # config first, transport TOML last.
    config_idxs = [i for i, tok in enumerate(cmd) if tok == "--config"]
    assert len(config_idxs) == 2, f"expected 2 --config flags, got cmd={cmd}"
    user_pos = cmd.index(str(user_cfg))
    # The transport.toml is generated under ``run_dir / transport.toml``;
    # its absolute path appears AFTER the user config in the cmd.
    transport_pos = next(i for i, tok in enumerate(cmd) if tok.endswith("transport.toml"))
    assert user_pos < transport_pos


def test_task_manager_skips_user_config_when_missing(tmp_path: Path) -> None:
    """If ``user_config_path`` points at a missing file (operator
    hasn't dropped one), the runner just passes its own transport
    config. No spurious ``--config`` flag, no error."""
    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    _seed_machine(state_db)

    runner_obj = TaskManager(
        state_path=state_db,
        publish_machines_changed=lambda: None,
        tasks_dir=tmp_path / "tasks",
        ssh_key_path=tmp_path / "key",
        cijoe_bin="cijoe-fake",
        user_config_path=tmp_path / "does-not-exist.toml",
    )

    proc = _fake_proc(0)
    with patch("bty.web._task.subprocess.Popen", return_value=proc) as mock_popen:
        runner_obj._run(_seed_state())

    cmd = mock_popen.call_args.args[0]
    config_idxs = [i for i, tok in enumerate(cmd) if tok == "--config"]
    assert len(config_idxs) == 1, f"expected exactly 1 --config flag, got cmd={cmd}"


# ---------- cancellation ----------------------------------------------------


def test_kick_off_refuses_non_ip_target(runner) -> None:
    """``kick_off`` validates ``target_ip`` is a real IPv4/v6
    address before spawning the worker thread. TOML-injection
    defence at the boundary."""
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    with patch("bty.web._task.subprocess.Popen") as mock_popen:
        for bad in (
            'host"; injected="x',
            "10.0.0.1\n[evil]\nx = 1",
            "not-an-ip",
            "",
        ):
            result = runner_obj.kick_off(
                mac="aa:bb:cc:dd:ee:ff",
                task_ref="/path/to/wf.yaml",
                target_ip=bad,
            )
            assert result is None, f"non-IP {bad!r} should have been refused"
    assert not mock_popen.called


def test_kick_off_idempotent_for_running_mac(runner) -> None:
    """A second ``kick_off`` for a mac whose task is already in
    flight returns the existing state and does NOT spawn a new
    worker thread. Per-MAC parallelism is 1 -- protects against
    a flapping target rapid-firing /pxe/{mac}/done and queueing
    redundant runs."""
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    # Pre-populate _states with a running task for this mac, so
    # the second kick_off sees an existing in-flight job.
    pre = TaskState(
        mac="aa:bb:cc:dd:ee:ff",
        task_ref="/wf.yaml",
        target_ip="10.0.0.5",
        status="running",
    )
    with runner_obj._lock:
        runner_obj._states["aa:bb:cc:dd:ee:ff"] = pre

    with patch("bty.web._task.subprocess.Popen") as mock_popen:
        result = runner_obj.kick_off(
            mac="aa:bb:cc:dd:ee:ff",
            task_ref="/different.yaml",
            target_ip="10.0.0.5",
        )
    assert result is pre
    # No subprocess spawned: kick_off returned early.
    assert not mock_popen.called


def test_cancel_unknown_mac_returns_none(runner) -> None:
    runner_obj, _, _ = runner
    assert runner_obj.cancel("aa:bb:cc:dd:ee:ff") is None


def test_cancel_running_terminates_subprocess(runner) -> None:
    """``cancel`` flips the threading.Event AND calls
    ``proc.terminate()`` so the cijoe subprocess actually stops.
    Without the terminate call, the worker would block on
    ``proc.communicate(timeout=...)`` until the cijoe binary
    exits on its own (potentially the full 30-min timeout)."""
    runner_obj, _, _ = runner

    pre = TaskState(
        mac="aa:bb:cc:dd:ee:ff",
        task_ref="/wf.yaml",
        target_ip="10.0.0.5",
        status="running",
    )
    fake_proc = MagicMock()
    pre._proc = fake_proc
    with runner_obj._lock:
        runner_obj._states["aa:bb:cc:dd:ee:ff"] = pre

    result = runner_obj.cancel("aa:bb:cc:dd:ee:ff")
    assert result is pre
    assert pre._cancel.is_set()
    fake_proc.terminate.assert_called_once()


def test_cancel_already_finished_is_noop(runner) -> None:
    """Cancelling a ``completed`` / ``cancelled`` / ``failed`` task
    returns the existing state without mutation, so the API can
    treat DELETE as idempotent."""
    runner_obj, _, _ = runner
    pre = TaskState(
        mac="aa:bb:cc:dd:ee:ff",
        task_ref="/wf.yaml",
        target_ip="10.0.0.5",
        status="completed",
    )
    with runner_obj._lock:
        runner_obj._states["aa:bb:cc:dd:ee:ff"] = pre

    result = runner_obj.cancel("aa:bb:cc:dd:ee:ff")
    assert result is pre
    assert not pre._cancel.is_set()  # not flipped


def test_run_records_cancelled_when_event_set(runner) -> None:
    """After ``cancel()`` flips the event and terminates the
    subprocess, the worker thread's ``_run`` sees a non-zero
    return code AND a set cancel event. It must record the
    result as ``cancelled`` (not ``failed``); otherwise the UI
    shows a misleading red badge for what was an operator-
    initiated stop."""
    runner_obj, state_db, _ = runner
    _seed_machine(state_db)

    state = _seed_state()
    state._cancel.set()  # simulate operator cancel before run
    proc = _fake_proc(-15)  # rc from SIGTERM
    with patch("bty.web._task.subprocess.Popen", return_value=proc):
        runner_obj._run(state)

    assert state.status == "cancelled"
    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "cancelled"


def test_list_returns_snapshot(runner) -> None:
    runner_obj, _, _ = runner
    pre = TaskState(
        mac="aa:bb:cc:dd:ee:ff",
        task_ref="/wf.yaml",
        target_ip="10.0.0.5",
        status="running",
    )
    with runner_obj._lock:
        runner_obj._states["aa:bb:cc:dd:ee:ff"] = pre

    snapshot = runner_obj.list()
    assert len(snapshot) == 1
    assert snapshot[0].mac == "aa:bb:cc:dd:ee:ff"


# ---------- start sweeps stale running rows ---------------------------------


def test_start_sweeps_stale_running_rows(runner) -> None:
    """``TaskManager.start()`` is called once at bty-web lifespan
    startup and rewrites any ``last_task_status='running'`` row
    in state.db to ``failed`` (the in-flight task died with the
    previous bty-web process). Without this, the UI shows a
    perma-running badge that never resolves."""
    runner_obj, state_db, _ = runner
    with _db.open_db(state_db) as conn:
        conn.execute(
            """
            INSERT INTO machines
                (mac, provisioning_mode, last_task_status,
                 boot_policy, created_at, updated_at)
            VALUES ('aa:bb:cc:dd:ee:ff', 'cijoe-task', 'running',
                    'flash', ?, ?)
            """,
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()

    runner_obj.start()

    with _db.open_db(state_db) as conn:
        row = conn.execute(
            "SELECT last_task_status FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_task_status"] == "failed"
