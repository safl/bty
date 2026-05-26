"""Smoke tests for bty-state-init: argument parsing + validation
paths the operator hits before any destructive action. Tests run
the script via subprocess against fake devices so they don't
actually wipe anything."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
SCRIPT = _REPO / "bty-media/rootfs/server/usr/local/sbin/bty-state-init"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=10)


def test_help_flag_returns_usage_with_zero_exit() -> None:
    r = _run("--help")
    assert r.returncode == 0
    assert "Usage: bty-state-init" in r.stdout
    assert "BTY_IMAGE_STORE" in r.stdout


def test_no_args_returns_usage_with_nonzero_exit() -> None:
    r = _run()
    assert r.returncode == 2
    assert "DEVICE argument required" in r.stderr


def test_unknown_flag_returns_usage_with_nonzero_exit() -> None:
    r = _run("--not-a-flag")
    assert r.returncode == 2
    assert "unknown flag" in r.stderr


def test_non_block_device_path_refused() -> None:
    """Non-root invocation refuses before any destructive action;
    the script's `id -u` check fires before the block-device check.
    Tests can run as non-root + still observe the rails."""
    import os

    r = _run("/tmp/not-a-block-device")
    # As non-root the script aborts at the root check. As root it
    # would abort at the block-device check. Either path is
    # operator-actionable; assert one of the documented messages.
    assert r.returncode == 2
    if os.geteuid() == 0:
        assert "is not a block device" in r.stderr
    else:
        assert "must be run as root" in r.stderr
