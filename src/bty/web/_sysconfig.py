"""System-config helpers backing the ``/ui/settings`` page.

Each function shells out to a privileged helper script under
``/usr/local/sbin/`` via ``sudo -n``. The sudoers entry shipped on
the bty server image (``/etc/sudoers.d/bty-web``) allows the ``bty``
service user to invoke exactly two helpers without a password —
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
ROTATE_HELPER = "/usr/local/sbin/bty-web-rotate-token"
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

    Reads ``/sys/class/net/<iface>/operstate`` directly — no
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


def rotate_token() -> str:
    """Invoke the rotation helper; return the new token on success.

    Does NOT restart bty-web — by design, the operator copies the
    new token from the UI before triggering a restart so they can
    log back in afterwards.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", ROTATE_HELPER],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        raise SysConfigError(
            f"rotate-token helper exited {exc.returncode}: {(exc.stderr or '').strip()}"
        ) from exc
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise SysConfigError(f"rotate-token helper failed: {exc}") from exc
    new_token = result.stdout.strip()
    if not new_token:
        raise SysConfigError("rotate-token helper produced an empty token")
    return new_token


def activate_pxe(interface: str, subnet: str) -> None:
    """Validate inputs and invoke the PXE-activation helper."""
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
    try:
        subprocess.run(
            ["sudo", "-n", ACTIVATE_PXE_HELPER, interface, subnet_arg],
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
