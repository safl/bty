"""Tests for ``bty.web._workflow.WorkflowRunner``.

Subprocess invocations of cijoe are mocked - a real cijoe binary
isn't installed in the dev env's ``[web]`` extras, and we don't
want the test suite to depend on the network either way. Each test
seeds an in-memory machine record, kicks off a runner with a
synchronous fake (no thread spawn) for the actual ``_run`` body,
and asserts the resulting DB state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bty.web import _db
from bty.web._workflow import WorkflowRunner


def _seed_machine(state_path: Path, mac: str = "aa:bb:cc:dd:ee:ff") -> None:
    """Insert a machine row so the runner's UPDATEs have a target."""
    with _db.open_db(state_path) as conn:
        conn.execute(
            """
            INSERT INTO machines
                (mac, provisioning_mode, boot_policy, created_at, updated_at)
            VALUES (?, 'cijoe-online', 'flash', ?, ?)
            """,
            (mac, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()


@pytest.fixture
def runner(tmp_path: Path):
    state = tmp_path / "state.db"
    _db.init_db(state)
    publishes: list[None] = []
    runner = WorkflowRunner(
        state_path=state,
        publish_machines_changed=lambda: publishes.append(None),
        workflows_dir=tmp_path / "workflows",
        ssh_key_path=tmp_path / "key",
        cijoe_bin="cijoe-fake",
    )
    return runner, state, publishes


# ---------- _run synchronous path ------------------------------------------


def test_workflow_runner_records_success(runner) -> None:
    runner_obj, state, publishes = runner
    _seed_machine(state)

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
    with patch("bty.web._workflow.subprocess.run", return_value=completed):
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT last_workflow_status, last_workflow_run_at, last_workflow_output_path "
            "FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_workflow_status"] == "success"
    assert row["last_workflow_run_at"] is not None
    assert row["last_workflow_output_path"]
    out_dir = Path(row["last_workflow_output_path"])
    # cijoe stdout/stderr captured as sidecars next to the run dir.
    assert (out_dir / "cijoe.stdout").read_text() == "ok\n"
    # Two SSE publishes: running, then success.
    assert len(publishes) == 2


def test_workflow_runner_records_failed_on_nonzero_exit(runner) -> None:
    runner_obj, state, _ = runner
    _seed_machine(state)

    completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom\n")
    with patch("bty.web._workflow.subprocess.run", return_value=completed):
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT last_workflow_status FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_workflow_status"] == "failed"


def test_workflow_runner_records_failed_on_timeout(runner) -> None:
    runner_obj, state, _ = runner
    _seed_machine(state)

    err = subprocess.TimeoutExpired(cmd=["cijoe"], timeout=10)
    with patch("bty.web._workflow.subprocess.run", side_effect=err):
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT last_workflow_status, last_workflow_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_workflow_status"] == "failed"
    assert (
        (Path(row["last_workflow_output_path"]) / "error.txt")
        .read_text()
        .startswith("cijoe timed out")
    )


def test_workflow_runner_records_failed_on_missing_binary(runner) -> None:
    runner_obj, state, _ = runner
    _seed_machine(state)

    err = FileNotFoundError("[Errno 2] No such file: 'cijoe-fake'")
    with patch("bty.web._workflow.subprocess.run", side_effect=err):
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT last_workflow_status, last_workflow_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    assert row["last_workflow_status"] == "failed"
    assert (
        "cijoe binary not found"
        in (Path(row["last_workflow_output_path"]) / "error.txt").read_text()
    )


# ---------- transport config ------------------------------------------------


def test_workflow_runner_renders_transport_config(runner) -> None:
    runner_obj, state, _ = runner
    _seed_machine(state)

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._workflow.subprocess.run", return_value=completed):
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    with _db.open_db(state) as conn:
        row = conn.execute(
            "SELECT last_workflow_output_path FROM machines WHERE mac = ?",
            ("aa:bb:cc:dd:ee:ff",),
        ).fetchone()
    config = (Path(row["last_workflow_output_path"]) / "transport.toml").read_text()
    assert 'hostname = "10.0.0.5"' in config
    assert "cijoe.transport.ssh" in config
    assert 'username = "root"' in config


# ---------- subprocess invocation -------------------------------------------


def test_workflow_runner_invokes_cijoe_with_workflow_and_config(runner) -> None:
    runner_obj, state, _ = runner
    _seed_machine(state)

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._workflow.subprocess.run", return_value=completed) as mock_run:
        runner_obj._run("aa:bb:cc:dd:ee:ff", "/path/to/wf.yaml", "10.0.0.5")

    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "cijoe-fake"
    assert cmd[1] == "/path/to/wf.yaml"
    assert "--config" in cmd
    assert "--monitor" in cmd
    # Subprocess runs with cwd set to the run dir so cijoe's
    # cijoe-output/ lands there.
    assert kwargs["cwd"]
