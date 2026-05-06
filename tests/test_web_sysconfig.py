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
    Interface,
    PxeConfig,
    SysConfigError,
    activate_pxe,
    list_interfaces,
    pxe_active,
)

# ---------- list_interfaces -------------------------------------------------


def test_list_interfaces_skips_loopback_and_returns_operstate(tmp_path: Path) -> None:
    sysnet = tmp_path / "net"
    for name in ("lo", "eth0", "ens18"):
        d = sysnet / name
        d.mkdir(parents=True)
        (d / "operstate").write_text("up\n" if name != "ens18" else "down\n")

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
    out = list_interfaces(sysnet=sysnet)
    assert out == [Interface(name="eth0", operstate="unknown")]


# ---------- pxe_active -----------------------------------------------------


def test_pxe_active_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert pxe_active(active_path=tmp_path / "nope.conf") is None


def test_pxe_active_parses_interface_and_subnet(tmp_path: Path) -> None:
    p = tmp_path / "active.conf"
    p.write_text(
        "bind-interfaces\n"
        "interface=eth0\n"
        "dhcp-range=192.168.1.0,proxy\n"
        "dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe\n"
    )
    cfg = pxe_active(active_path=p)
    assert cfg == PxeConfig(interface="eth0", subnet="192.168.1.0")


def test_pxe_active_returns_none_on_malformed(tmp_path: Path) -> None:
    p = tmp_path / "active.conf"
    p.write_text("# only comments\n")
    assert pxe_active(active_path=p) is None


# ---------- activate_pxe ---------------------------------------------------


def test_activate_pxe_proxy_mode_passes_validated_args_to_helper() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        activate_pxe("eth0", "192.168.1.0/24")
    args, _ = mock_run.call_args
    cmd = args[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("/bty-web-activate-pxe")
    # mode + interface + canonicalised network address; no extras.
    assert cmd[3:] == ["proxy", "eth0", "192.168.1.0"]


def test_activate_pxe_full_mode_passes_range_and_netmask() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        activate_pxe(
            "eth0",
            "192.168.99.0",
            mode="full",
            range_lo="192.168.99.50",
            range_hi="192.168.99.150",
            netmask="255.255.255.0",
        )
    cmd = mock_run.call_args[0][0]
    assert cmd[3:] == [
        "full",
        "eth0",
        "192.168.99.0",
        "192.168.99.50",
        "192.168.99.150",
        "255.255.255.0",
    ]


def test_activate_pxe_full_mode_requires_range_and_netmask() -> None:
    with pytest.raises(SysConfigError, match="full mode requires"):
        activate_pxe("eth0", "10.0.0.0", mode="full")


def test_activate_pxe_rejects_unknown_mode() -> None:
    with pytest.raises(SysConfigError, match="invalid mode"):
        activate_pxe("eth0", "10.0.0.0", mode="weird")


def test_activate_pxe_accepts_bare_network_address() -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("bty.web._sysconfig.subprocess.run", return_value=completed) as mock_run:
        activate_pxe("eth0", "10.0.0.0")
    cmd = mock_run.call_args[0][0]
    assert cmd[3:] == ["proxy", "eth0", "10.0.0.0"]


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
