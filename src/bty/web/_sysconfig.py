"""System-config helpers backing the ``/ui/settings`` page.

Each function shells out to a privileged helper script under
``/usr/local/sbin/`` via ``sudo -n``. The sudoers entry shipped on
the bty server image (``/etc/sudoers.d/bty-web``) allows the ``bty``
service user to invoke exactly two helpers without a password -
nothing else.

The helpers do the actual writes; this module is the trust boundary
on the bty-web side: it validates inputs and turns subprocess
failures into :class:`SysConfigError` so the UI can show a clean
message instead of leaking subprocess details.

Listing interfaces and reading the active PXE config are
unprivileged operations done directly here.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PXE_ACTIVE_PATH = Path("/etc/dnsmasq.d/bty-pxe-active.conf")
SYSNET_PATH = Path("/sys/class/net")
ACTIVATE_PXE_HELPER = "/usr/local/sbin/bty-web-activate-pxe"

# Per the helper's own validation; mirrored here for early rejection.
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Interface:
    name: str
    operstate: str  # "up" / "down" / "unknown"


@dataclass(frozen=True)
class PxeConfig:
    interface: str
    subnet: str


class SysConfigError(Exception):
    """Helper failed; the message is safe to surface to the UI."""


def list_interfaces(sysnet: Path = SYSNET_PATH) -> list[Interface]:
    """Return non-loopback network interfaces with their operstate.

    Reads ``/sys/class/net/<iface>/operstate`` directly - no
    subprocess, no privileges. Returns an empty list on hosts where
    ``/sys/class/net`` doesn't exist (containers, tests).
    """
    if not sysnet.is_dir():
        return []
    out: list[Interface] = []
    for entry in sorted(sysnet.iterdir()):
        if entry.name == "lo":
            continue
        operstate_path = entry / "operstate"
        operstate = operstate_path.read_text().strip() if operstate_path.is_file() else "unknown"
        out.append(Interface(name=entry.name, operstate=operstate))
    return out


def pxe_active(active_path: Path = PXE_ACTIVE_PATH) -> PxeConfig | None:
    """Parse the active PXE config; ``None`` if the file is absent."""
    if not active_path.is_file():
        return None
    text = active_path.read_text()
    iface_match = re.search(r"^interface=(.+)$", text, re.MULTILINE)
    subnet_match = re.search(r"^dhcp-range=([^,]+),proxy", text, re.MULTILINE)
    if iface_match and subnet_match:
        return PxeConfig(
            interface=iface_match.group(1).strip(), subnet=subnet_match.group(1).strip()
        )
    return None


def activate_pxe(
    interface: str,
    subnet: str,
    mode: str = "proxy",
    range_lo: str | None = None,
    range_hi: str | None = None,
    netmask: str | None = None,
) -> None:
    """Validate inputs and invoke the PXE-activation helper.

    ``mode`` is ``proxy`` (default - other DHCP server on segment)
    or ``full`` (bty-server is the only DHCP server, must hand out
    IPs as well as PXE info). Full-DHCP mode requires ``range_lo``,
    ``range_hi``, and ``netmask``.
    """
    if mode not in {"proxy", "full"}:
        raise SysConfigError(f"invalid mode: {mode!r} (expected 'proxy' or 'full')")
    interface = interface.strip()
    subnet = subnet.strip()
    if not _INTERFACE_RE.fullmatch(interface):
        raise SysConfigError(f"invalid interface name: {interface!r}")
    try:
        # Accept either ``192.168.1.0`` or ``192.168.1.0/24``; the
        # helper takes the bare network address, so split CIDR off
        # if present.
        cidr_or_addr = subnet
        if "/" in subnet:
            net = ipaddress.IPv4Network(cidr_or_addr, strict=False)
            subnet_arg = str(net.network_address)
        else:
            ipaddress.IPv4Address(cidr_or_addr)
            subnet_arg = cidr_or_addr
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise SysConfigError(f"invalid subnet: {subnet!r}") from exc

    extra: list[str] = []
    if mode == "full":
        if not (range_lo and range_hi and netmask):
            raise SysConfigError("full mode requires range_lo, range_hi, and netmask")
        for label, value in (
            ("range_lo", range_lo),
            ("range_hi", range_hi),
            ("netmask", netmask),
        ):
            try:
                ipaddress.IPv4Address(value.strip())
            except (ipaddress.AddressValueError, ValueError) as exc:
                raise SysConfigError(f"invalid {label}: {value!r}") from exc
            extra.append(value.strip())

    cmd = ["sudo", "-n", ACTIVATE_PXE_HELPER, mode, interface, subnet_arg, *extra]
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise SysConfigError(
            f"activate-pxe helper exited {exc.returncode}: {(exc.stderr or '').strip()}"
        ) from exc
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise SysConfigError(f"activate-pxe helper failed: {exc}") from exc
