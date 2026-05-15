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
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PXE_ACTIVE_PATH = Path("/etc/default/bty-pxe-proxy")
SYSNET_PATH = Path("/sys/class/net")
ACTIVATE_PXE_HELPER = "/usr/local/sbin/bty-web-activate-pxe"
DEACTIVATE_PXE_HELPER = "/usr/local/sbin/bty-web-deactivate-pxe"

# Per the helper's own validation; mirrored here for early rejection.
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Interface:
    name: str
    operstate: str  # "up" / "down" / "unknown"
    # First IPv4 address + prefix on the interface, or ``None`` when
    # the interface has no v4 address. Pre-populates the PXE-activate
    # form's subnet/netmask fields so the operator doesn't have to
    # type the segment by hand.
    ipv4: str | None = None
    prefix: int | None = None

    @property
    def subnet(self) -> str | None:
        """Network address (e.g. ``192.168.1.0`` for ``192.168.1.42/24``)."""
        if self.ipv4 is None or self.prefix is None:
            return None
        try:
            return str(
                ipaddress.IPv4Network(f"{self.ipv4}/{self.prefix}", strict=False).network_address
            )
        except (ipaddress.AddressValueError, ValueError):
            return None

    @property
    def netmask(self) -> str | None:
        """Dotted-quad netmask derived from the prefix (e.g. ``255.255.255.0``)."""
        if self.prefix is None:
            return None
        try:
            return str(ipaddress.IPv4Network(f"0.0.0.0/{self.prefix}").netmask)
        except (ipaddress.AddressValueError, ValueError):
            return None


@dataclass(frozen=True)
class PxeConfig:
    interface: str
    subnet: str


@dataclass(frozen=True)
class PxeState:
    """Combined view of the active PXE config + NIC presence.

    ``config`` is the parsed active config (``None`` when PXE is
    deactivated). ``iface_present`` is True iff ``config.interface``
    appears in :func:`list_interfaces` -- false means dnsmasq is
    bound to a NIC that has since gone away (USB ethernet adapter
    unplugged, systemd predictable-name churn across reboot).
    """

    config: PxeConfig | None
    iface_present: bool


class SysConfigError(Exception):
    """Helper failed; the message is safe to surface to the UI."""


def pxe_state() -> PxeState:
    """Read the active PXE config + check the bound NIC still exists.

    Two callsites (the /ui/dashboard initial render and the SSE
    counts-refresh fragment in ``_app.py``) need exactly this pair,
    and they MUST agree -- one source of truth keeps the dashboard
    tile coherent across refresh paths.
    """
    config = pxe_active()
    if config is None:
        return PxeState(config=None, iface_present=False)
    iface_present = any(i.name == config.interface for i in list_interfaces())
    return PxeState(config=config, iface_present=iface_present)


def list_interfaces(sysnet: Path = SYSNET_PATH) -> list[Interface]:
    """Return non-loopback network interfaces with operstate + first IPv4.

    Reads ``/sys/class/net/<iface>/operstate`` for the up/down/unknown
    state, and shells out to ``ip -j addr show <iface>`` (iproute2 ships
    with every Debian) to capture the primary IPv4 address + prefix.
    Returns an empty list on hosts where ``/sys/class/net`` doesn't
    exist (containers, tests). Failures to read addresses are not
    fatal - the interface is still listed, just without the IP.
    """
    if not sysnet.is_dir():
        return []
    out: list[Interface] = []
    for entry in sorted(sysnet.iterdir()):
        if entry.name == "lo":
            continue
        operstate_path = entry / "operstate"
        operstate = operstate_path.read_text().strip() if operstate_path.is_file() else "unknown"
        ipv4, prefix = _first_ipv4(entry.name)
        out.append(Interface(name=entry.name, operstate=operstate, ipv4=ipv4, prefix=prefix))
    return out


def _first_ipv4(iface: str) -> tuple[str | None, int | None]:
    """Return ``(address, prefix)`` for the interface's first IPv4 address.

    Uses ``ip -j addr show <iface>`` and picks the first ``inet`` entry.
    ``(None, None)`` when the tool is missing, the interface has no
    addresses, or the JSON shape doesn't match expectations - the UI
    treats those as "no info" and lets the operator type values.
    """
    try:
        result = subprocess.run(
            ["ip", "-j", "addr", "show", iface],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None, None
    if not payload:
        return None, None
    for addr in payload[0].get("addr_info", []):
        if addr.get("family") != "inet":
            continue
        local = addr.get("local")
        prefixlen = addr.get("prefixlen")
        if isinstance(local, str) and isinstance(prefixlen, int):
            return local, prefixlen
    return None, None


def pxe_active(active_path: Path = PXE_ACTIVE_PATH) -> PxeConfig | None:
    """Parse the active PXE config; ``None`` if the file is absent.

    The active file is an ``EnvironmentFile``-shaped key=value blob
    written by ``bty-web-activate-pxe`` and read by
    ``bty-pxe-proxy.service``::

        BTY_PXE_INTERFACE=enp90s0
        BTY_PXE_SUBNET=192.168.1.0
    """
    if not active_path.is_file():
        return None
    text = active_path.read_text()
    iface_match = re.search(r"^BTY_PXE_INTERFACE=(.+)$", text, re.MULTILINE)
    subnet_match = re.search(r"^BTY_PXE_SUBNET=(.+)$", text, re.MULTILINE)
    if iface_match and subnet_match:
        return PxeConfig(
            interface=iface_match.group(1).strip(),
            subnet=subnet_match.group(1).strip(),
        )
    return None


def activate_pxe(interface: str, subnet: str) -> None:
    """Validate inputs and invoke the PXE-activation helper.

    Proxy-DHCP only: bty assumes there is already a DHCP server on
    the segment handing out IPs. We deliberately do NOT support
    full DHCP from this UI - the blast radius of a misconfigured
    bty handing out IPs (rogue DHCP that conflicts with the real
    LAN router) is high and the actual operator demand is low.
    Run a dedicated DHCP daemon next to bty if you need one.
    """
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


def deactivate_pxe() -> None:
    """Invoke the PXE-deactivation helper.

    Idempotent on the helper side: a missing
    ``/etc/dnsmasq.d/bty-pxe-active.conf`` is reported as
    "already inactive", which the operator can ignore.
    Restarts ``dnsmasq.service`` so the change takes effect.
    """
    try:
        subprocess.run(
            ["sudo", "-n", DEACTIVATE_PXE_HELPER],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise SysConfigError(
            f"deactivate-pxe helper exited {exc.returncode}: {(exc.stderr or '').strip()}"
        ) from exc
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise SysConfigError(f"deactivate-pxe helper failed: {exc}") from exc
