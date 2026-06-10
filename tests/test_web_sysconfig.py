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

from bty.web._sysconfig import (
    DaemonStatus,
    Interface,
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


# ---------- DaemonStatus / tftp_status ----------------------------------


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


def test_tftp_status_masked_state_passes_through() -> None:
    """``systemctl is-active`` returns 'inactive' for masked units
    (and 'masked' on some systemd versions). Either way the
    template should render it via the bg-secondary fallback."""
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="masked\n", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed):
        status = tftp_status()
    assert status.state == "masked"
    assert status.is_active is False


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


# ---------- tftp_probe ----------------------------------------------------


def test_tftp_probe_reports_file_present_on_data_reply() -> None:
    """A DATA opcode (3) means the server has the file -- both
    legs of the probe should report success."""
    import struct as _struct

    from bty.web._sysconfig import tftp_probe

    fake_data = _struct.pack("!HH", 3, 1) + b"x" * 510  # DATA, block 1, payload

    class FakeSock:
        def __init__(self) -> None:
            self.sent: list[tuple] = []

        def settimeout(self, _t: float) -> None:
            return None

        def sendto(self, pkt: bytes, addr: tuple) -> None:
            self.sent.append((pkt, addr))

        def recvfrom(self, _n: int) -> tuple[bytes, tuple]:
            return fake_data, ("127.0.0.1", 33000)

        def close(self) -> None:
            return None

    fake = FakeSock()
    with patch("bty.web._sysconfig.socket.socket", return_value=fake):
        result = tftp_probe(host="127.0.0.1")
    assert result.reachable is True
    assert result.file_present is True
    assert result.ok is True
    # RRQ packet structure: opcode 1, filename, NUL, "octet", NUL.
    sent_pkt = fake.sent[0][0]
    assert sent_pkt.startswith(b"\x00\x01")
    assert b"ipxe.efi\x00octet\x00" in sent_pkt


def test_tftp_probe_reports_missing_on_error_reply() -> None:
    """An ERROR opcode (5) means the server is up but the file
    isn't there -- reachable yes, file_present no."""
    import struct as _struct

    from bty.web._sysconfig import tftp_probe

    fake_data = _struct.pack("!HH", 5, 1) + b"File not found\x00"

    class FakeSock:
        def settimeout(self, _t: float) -> None:
            return None

        def sendto(self, _pkt: bytes, _addr: tuple) -> None:
            return None

        def recvfrom(self, _n: int) -> tuple[bytes, tuple]:
            return fake_data, ("127.0.0.1", 33000)

        def close(self) -> None:
            return None

    with patch("bty.web._sysconfig.socket.socket", return_value=FakeSock()):
        result = tftp_probe(host="127.0.0.1")
    assert result.reachable is True
    assert result.file_present is False
    assert result.ok is False
    assert "File not found" in result.detail


def test_tftp_probe_timeout_means_unreachable() -> None:
    """No reply within the timeout -> reachable=False, file_present
    irrelevant. The detail string includes the host:port + timeout
    so an operator can see exactly what bty-web tried."""

    from bty.web._sysconfig import tftp_probe

    class FakeSock:
        def settimeout(self, _t: float) -> None:
            return None

        def sendto(self, _pkt: bytes, _addr: tuple) -> None:
            return None

        def recvfrom(self, _n: int) -> tuple[bytes, tuple]:
            raise TimeoutError

        def close(self) -> None:
            return None

    with patch("bty.web._sysconfig.socket.socket", return_value=FakeSock()):
        result = tftp_probe(host="127.0.0.1", timeout_s=0.05)
    assert result.reachable is False
    assert result.file_present is False
    assert "127.0.0.1:69" in result.detail


def test_tftp_probe_oserror_is_unreachable_not_500() -> None:
    """Connection refused / no-route surfaces as reachable=False
    with the exception class + message in detail -- the UI never
    sees a 500 from a bad probe target."""
    from bty.web._sysconfig import tftp_probe

    class FakeSock:
        def settimeout(self, _t: float) -> None:
            return None

        def sendto(self, _pkt: bytes, _addr: tuple) -> None:
            raise ConnectionRefusedError("nope")

        def recvfrom(self, _n: int) -> tuple[bytes, tuple]:
            raise AssertionError("should not be reached")

        def close(self) -> None:
            return None

    with patch("bty.web._sysconfig.socket.socket", return_value=FakeSock()):
        result = tftp_probe(host="127.0.0.1")
    assert result.reachable is False
    assert "ConnectionRefusedError" in result.detail


# NOTE: probe-host resolution moved into Config
# (``effective_tftp_probe_host`` / ``advertised_host``); ``tftp_probe``
# now takes an explicit ``host`` and no longer reads an env var of its
# own. The resolution is covered in tests/test_web_config.py.


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
