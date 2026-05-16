"""Tests for ``bty.web._sysconfig``.

What used to be tested (proxy-DHCP activate/deactivate, daemon-
control, journald event reader) is gone with v0.18's
architectural pivot to operator-router-owned DHCP. The remaining
surface is :func:`list_interfaces`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bty.web._sysconfig import (
    DaemonStatus,
    Interface,
    SysConfigError,
    control_tftp,
    list_interfaces,
    tftp_status,
)


def test_list_interfaces_skips_loopback_and_returns_operstate(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    for name in ("lo", "eth0", "ens18"):
        d = sysnet / name
        d.mkdir(parents=True)
        (d / "operstate").write_text("up\n" if name != "ens18" else "down\n")

    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=fake):
        out = list_interfaces(sysnet=sysnet)
    names = [i.name for i in out]
    assert names == ["ens18", "eth0"]  # sorted, no lo
    states = {i.name: i.operstate for i in out}
    assert states == {"ens18": "down", "eth0": "up"}


def test_list_interfaces_handles_missing_sysnet(tmp_path: Path) -> None:
    assert list_interfaces(sysnet=tmp_path / "no-such-dir") == []


def test_list_interfaces_handles_missing_operstate_file(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    (sysnet / "eth0").mkdir(parents=True)
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=fake):
        out = list_interfaces(sysnet=sysnet)
    assert out == [Interface(name="eth0", operstate="unknown")]


def test_list_interfaces_captures_first_ipv4(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    (sysnet / "eth0").mkdir(parents=True)
    (sysnet / "eth0" / "operstate").write_text("up\n")

    ip_json = (
        '[{"ifname":"eth0","addr_info":['
        '{"family":"inet6","local":"fe80::1","prefixlen":64},'
        '{"family":"inet","local":"192.168.1.42","prefixlen":24}'
        "]}]"
    )
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=ip_json, stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=fake):
        out = list_interfaces(sysnet=sysnet)
    assert len(out) == 1
    assert out[0].ipv4 == "192.168.1.42"
    assert out[0].prefix == 24
    assert out[0].subnet == "192.168.1.0"
    assert out[0].netmask == "255.255.255.0"


def test_list_interfaces_no_ip_yields_none_fields(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    (sysnet / "eth0").mkdir(parents=True)
    (sysnet / "eth0" / "operstate").write_text("down\n")
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{"ifname":"eth0","addr_info":[]}]',
        stderr="",
    )
    with patch("bty.web._sysconfig.subprocess.run", return_value=fake):
        out = list_interfaces(sysnet=sysnet)
    assert out[0].ipv4 is None
    assert out[0].subnet is None
    assert out[0].netmask is None


def test_list_interfaces_tolerates_missing_ip_tool(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    (sysnet / "eth0").mkdir(parents=True)
    (sysnet / "eth0" / "operstate").write_text("up\n")
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=FileNotFoundError("ip"),
    ):
        out = list_interfaces(sysnet=sysnet)
    assert out[0].name == "eth0"
    assert out[0].ipv4 is None


# ---------- DaemonStatus / tftp_status / control_tftp ---------------------


def test_daemon_status_is_active_true_only_for_active() -> None:
    assert DaemonStatus(state="active").is_active is True
    assert DaemonStatus(state="inactive").is_active is False
    assert DaemonStatus(state="failed").is_active is False
    assert DaemonStatus(state="unknown").is_active is False


def test_tftp_status_returns_active_when_systemctl_says_so() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        status = tftp_status()
    assert status == DaemonStatus(state="active")
    # systemctl invocation pinned: is-active + the tftp unit name.
    cmd = mock_run.call_args[0][0]
    assert cmd == ["systemctl", "is-active", "dnsmasq.service"]


def test_tftp_status_handles_inactive_nonzero_exit() -> None:
    """``systemctl is-active`` exits non-zero on inactive/failed
    but stdout still carries the state name. We keep the state
    rather than treating non-zero as 'unknown'."""
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="inactive\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        assert tftp_status().state == "inactive"


def test_tftp_status_returns_unknown_when_systemctl_missing() -> None:
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=FileNotFoundError("systemctl"),
    ):
        assert tftp_status().state == "unknown"


def test_tftp_status_returns_unknown_on_timeout() -> None:
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["systemctl"], timeout=5),
    ):
        assert tftp_status().state == "unknown"


def test_control_tftp_shells_helper_via_sudo() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        control_tftp("restart")
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("/bty-web-tftp")
    assert cmd[3:] == ["restart"]


def test_control_tftp_rejects_unknown_action() -> None:
    with pytest.raises(SysConfigError, match="unknown action"):
        control_tftp("enable")  # not in allowlist


def test_control_tftp_helper_failure_wraps_as_sysconfig_error() -> None:
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["sudo"], stderr="Failed to restart dnsmasq.service\n"
    )
    with (
        patch("bty.web._sysconfig.subprocess.run", side_effect=err),
        pytest.raises(SysConfigError, match="exited 1"),
    ):
        control_tftp("restart")


def test_control_tftp_helper_timeout_wraps_as_sysconfig_error() -> None:
    """If systemctl wedges and the 30s timeout fires, the user
    should get a SysConfigError flash, not a raw subprocess
    exception bubbling to the UI."""
    with (
        patch(
            "bty.web._sysconfig.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["sudo"], timeout=30),
        ),
        pytest.raises(SysConfigError, match="tftp helper failed"),
    ):
        control_tftp("restart")


def test_control_tftp_empty_action_gets_friendly_error() -> None:
    """Form field arriving as an empty string surfaces a clean
    error rather than the generic 'unknown action: \\'\\'' path."""
    with pytest.raises(SysConfigError, match="no action specified"):
        control_tftp("")


def test_tftp_status_masked_state_passes_through() -> None:
    """``systemctl is-active`` returns 'inactive' for masked units
    (and 'masked' on some systemd versions). Either way the
    template should render it via the bg-secondary fallback."""
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="masked\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        status = tftp_status()
    assert status.state == "masked"
    assert status.is_active is False


def test_tftp_controllable_requires_helper_and_sudo() -> None:
    """The UI hides Start/Stop/Restart buttons when sudo or the
    helper isn't installed (Docker container case). On a clean
    test host neither path exists; on the appliance both do."""
    from bty.web._sysconfig import tftp_controllable

    # No expectation on truthiness -- depends on the test host.
    # Just verify it returns a bool and doesn't crash.
    assert isinstance(tftp_controllable(), bool)


def test_tftp_status_falls_back_to_pgrep_when_systemctl_missing() -> None:
    """Inside the bty-web Docker container there's no systemd /
    no systemctl on PATH. ``tftp_status`` should fall back to
    ``pgrep -x dnsmasq`` so the UI badge still reflects whether
    the daemon is alive."""

    def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd[0] == "systemctl":
            raise FileNotFoundError("systemctl")
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"42\n")
        raise AssertionError(f"unexpected: {cmd}")

    with patch("bty.web._sysconfig.subprocess.run", side_effect=fake_run):
        status = tftp_status()
    assert status.state == "active"


def test_tftp_status_pgrep_missing_returns_inactive() -> None:
    """pgrep returns 1 when no matching process. UI shows
    inactive badge (grey)."""

    def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd[0] == "systemctl":
            raise FileNotFoundError("systemctl")
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=b"")
        raise AssertionError(f"unexpected: {cmd}")

    with patch("bty.web._sysconfig.subprocess.run", side_effect=fake_run):
        status = tftp_status()
    assert status.state == "inactive"
