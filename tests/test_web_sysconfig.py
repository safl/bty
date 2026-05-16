"""Tests for ``bty.web._sysconfig``.

Subprocess invocations of the privileged helpers are mocked - the
helpers run as root and we don't want the test suite shelling out
to sudo. Filesystem helpers (``list_interfaces``, ``pxe_active``)
are exercised against tmp paths so they don't depend on the host's
``/sys/class/net``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bty.web._sysconfig import (
    DaemonEvent,
    DaemonStatus,
    Interface,
    PxeConfig,
    SysConfigError,
    activate_pxe,
    control_daemon,
    daemon_status,
    daemon_statuses,
    list_interfaces,
    pxe_active,
    recent_daemon_events,
)

# ---------- list_interfaces -------------------------------------------------


def test_list_interfaces_skips_loopback_and_returns_operstate(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    for name in ("lo", "eth0", "ens18"):
        d = sysnet / name
        d.mkdir(parents=True)
        (d / "operstate").write_text("up\n" if name != "ens18" else "down\n")

    # ``ip -j addr show`` is invoked once per interface; mock it so
    # the test doesn't depend on host networking.
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
    (sysnet / "eth0").mkdir(parents=True)  # no operstate file
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=fake):
        out = list_interfaces(sysnet=sysnet)
    assert out == [Interface(name="eth0", operstate="unknown")]


def test_list_interfaces_captures_first_ipv4(tmp_path: Path) -> None:
    """``ip -j addr show <iface>`` JSON gets parsed into ``ipv4`` +
    ``prefix`` so the UI can pre-fill the PXE-activate subnet field."""
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
    # Derived fields exposed via @property for the template.
    assert out[0].subnet == "192.168.1.0"
    assert out[0].netmask == "255.255.255.0"


def test_list_interfaces_no_ip_yields_none_fields(tmp_path: Path) -> None:
    """Interfaces with no v4 address still get listed, but with
    ``ipv4=None`` / ``prefix=None`` so the form leaves the field
    blank and the operator types it themselves."""
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
    """``ip`` not on PATH (containers, minimal images) -> address
    fields just stay None instead of failing the whole listing."""
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


# ---------- pxe_active -----------------------------------------------------


def test_pxe_active_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert pxe_active(active_path=tmp_path / "nope.conf") is None


def test_pxe_active_parses_interface_and_subnet(tmp_path: Path) -> None:
    """``pxe_active`` parses the EnvironmentFile bty-web-activate-pxe
    writes for bty-pxe-proxy.service. Format is shell-style
    ``KEY=VALUE``; we look at BTY_PXE_INTERFACE + BTY_PXE_SUBNET."""
    p = tmp_path / "bty-pxe-proxy"
    p.write_text(
        "# Generated by bty-web-activate-pxe.\nBTY_PXE_INTERFACE=eth0\nBTY_PXE_SUBNET=192.168.1.0\n"
    )
    cfg = pxe_active(active_path=p)
    assert cfg == PxeConfig(interface="eth0", subnet="192.168.1.0")


def test_pxe_active_returns_none_on_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bty-pxe-proxy"
    p.write_text("# only comments\n")
    assert pxe_active(active_path=p) is None


# ---------- activate_pxe ---------------------------------------------------


def test_activate_pxe_passes_validated_args_to_helper() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        activate_pxe("eth0", "192.168.1.0/24")
    args, _ = mock_run.call_args
    cmd = args[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("/bty-web-activate-pxe")
    # interface + canonicalised network address.
    assert cmd[3:] == ["eth0", "192.168.1.0"]


def test_activate_pxe_accepts_bare_network_address() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        activate_pxe("eth0", "10.0.0.0")
    cmd = mock_run.call_args[0][0]
    assert cmd[3:] == ["eth0", "10.0.0.0"]


def test_activate_pxe_rejects_bad_interface_name() -> None:
    with pytest.raises(SysConfigError, match="invalid interface"):
        activate_pxe("eth0; rm -rf /", "192.168.1.0")


def test_activate_pxe_rejects_bad_subnet() -> None:
    with pytest.raises(SysConfigError, match="invalid subnet"):
        activate_pxe("eth0", "not-an-ip")


def test_activate_pxe_helper_failure_wraps_as_sysconfig_error() -> None:
    err = subprocess.CalledProcessError(returncode=2, cmd=["sudo"], stderr="bad subnet\n")
    with (
        patch("bty.web._sysconfig.subprocess.run", side_effect=err),
        pytest.raises(SysConfigError, match="exited 2"),
    ):
        activate_pxe("eth0", "192.168.1.0")


# ---------- daemon_status / daemon_statuses --------------------------------


def test_daemon_status_returns_systemctl_state_verbatim() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        status = daemon_status("bty-pxe-proxy")
    assert status == DaemonStatus(unit="bty-pxe-proxy", state="active")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["systemctl", "is-active", "bty-pxe-proxy.service"]


def test_daemon_status_handles_inactive_nonzero_exit() -> None:
    # ``systemctl is-active`` exits non-zero on inactive / failed,
    # but stdout still carries the state name and we keep it.
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="inactive\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        status = daemon_status("bty-tftp")
    assert status.state == "inactive"


def test_daemon_status_returns_unknown_when_systemctl_missing() -> None:
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=FileNotFoundError("systemctl"),
    ):
        status = daemon_status("bty-tftp")
    assert status.state == "unknown"


def test_daemon_status_rejects_unknown_unit() -> None:
    with pytest.raises(SysConfigError, match="unknown unit"):
        daemon_status("sshd")  # not in PXE_DAEMON_UNITS


def test_daemon_statuses_covers_both_pxe_daemons() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        results = daemon_statuses()
    units = [s.unit for s in results]
    assert units == ["bty-pxe-proxy", "bty-tftp"]


# ---------- control_daemon -------------------------------------------------


def test_control_daemon_shells_helper_via_sudo() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        control_daemon("bty-pxe-proxy", "restart")
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("/bty-web-pxe-daemon")
    assert cmd[3:] == ["restart", "bty-pxe-proxy"]


def test_control_daemon_rejects_unknown_unit() -> None:
    with pytest.raises(SysConfigError, match="unknown unit"):
        control_daemon("sshd", "restart")


def test_control_daemon_rejects_unknown_action() -> None:
    with pytest.raises(SysConfigError, match="unknown action"):
        control_daemon("bty-pxe-proxy", "enable")  # enable not in allowlist


def test_control_daemon_helper_failure_wraps_as_sysconfig_error() -> None:
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["sudo"], stderr="Failed to restart bty-pxe-proxy.service\n"
    )
    with (
        patch("bty.web._sysconfig.subprocess.run", side_effect=err),
        pytest.raises(SysConfigError, match="exited 1"),
    ):
        control_daemon("bty-pxe-proxy", "restart")


# ---------- recent_daemon_events -------------------------------------------


def _journal_line(*, unit: str, ts_us: int, message: str) -> str:
    """Compose one journalctl --output=json line."""
    import json as _json

    return _json.dumps(
        {
            "_SYSTEMD_UNIT": f"{unit}.service",
            "__REALTIME_TIMESTAMP": str(ts_us),
            "MESSAGE": message,
        }
    )


def test_recent_daemon_events_parses_structured_lines() -> None:
    lines = "\n".join(
        [
            _journal_line(
                unit="bty-pxe-proxy",
                ts_us=1_700_000_000_000_000,
                message='{"evt":"dhcp.offer","mac":"aa:bb","arch":7,"bootfile":"ipxe.efi"}',
            ),
            _journal_line(
                unit="bty-tftp",
                ts_us=1_700_000_001_000_000,
                message='{"evt":"tftp.complete","peer":"192.168.1.50:1234","bytes":1024}',
            ),
        ]
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=lines, stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        out = recent_daemon_events()
    # journalctl invocation: both units passed via -u, JSON output.
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["journalctl", "--output=json", "--no-pager"]
    assert "-u" in cmd and "bty-pxe-proxy.service" in cmd
    assert "bty-tftp.service" in cmd
    # Parsed events kept in journal order (oldest first), ``evt``
    # stripped from fields.
    assert len(out) == 2
    assert out[0] == DaemonEvent(
        unit="bty-pxe-proxy",
        ts_us=1_700_000_000_000_000,
        event="dhcp.offer",
        fields={"mac": "aa:bb", "arch": 7, "bootfile": "ipxe.efi"},
    )
    assert out[1].event == "tftp.complete"


def test_recent_daemon_events_skips_plain_log_lines() -> None:
    """Daemons emit a plain-text startup banner alongside structured
    events. Those land in journald but aren't JSON; the parser must
    skip them silently rather than blowing up."""
    lines = "\n".join(
        [
            _journal_line(
                unit="bty-pxe-proxy",
                ts_us=1_700_000_000_000_000,
                message="bty-pxe-proxy: listening on enp90s0 (UDP 67)",
            ),
            _journal_line(
                unit="bty-pxe-proxy",
                ts_us=1_700_000_000_500_000,
                message='{"evt":"dhcp.discover","mac":"aa:bb","arch":7}',
            ),
        ]
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=lines, stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        out = recent_daemon_events()
    # Only the structured line came through.
    assert len(out) == 1
    assert out[0].event == "dhcp.discover"


def test_recent_daemon_events_skips_dict_without_evt_key() -> None:
    """JSON-but-not-an-event messages (e.g. ``log.info(json.dumps({...}))``
    elsewhere) must not be treated as events."""
    lines = _journal_line(
        unit="bty-pxe-proxy",
        ts_us=1_700_000_000_000_000,
        message='{"hello": "world"}',
    )
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=lines, stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        assert recent_daemon_events() == []


def test_recent_daemon_events_returns_empty_when_journalctl_missing() -> None:
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=FileNotFoundError("journalctl"),
    ):
        assert recent_daemon_events() == []


def test_recent_daemon_events_returns_empty_on_timeout() -> None:
    with patch(
        "bty.web._sysconfig.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["journalctl"], timeout=5),
    ):
        assert recent_daemon_events() == []


def test_daemon_event_hms_and_iso_utc_properties() -> None:
    # 1700000000 == 2023-11-14 22:13:20 UTC.
    e = DaemonEvent(
        unit="bty-pxe-proxy",
        ts_us=1_700_000_000_000_000,
        event="dhcp.offer",
        fields={},
    )
    assert e.hms_utc == "22:13:20"
    assert e.iso_utc.startswith("2023-11-14T22:13:20")
