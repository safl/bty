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

from bty.web._sysconfig import Interface, list_interfaces


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
