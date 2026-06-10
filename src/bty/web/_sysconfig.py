"""System-config helpers for the ``/ui/netboot`` page (DHCP / PXE +
TFTP daemon sub-sections).

* :func:`list_interfaces` -- enumerate non-loopback network
  interfaces with their IPv4 / operstate. Used to suggest the
  bty host's own IP to the operator in the router-config
  cheatsheet (the DHCP / PXE card on the Settings page).
* :func:`tftp_status` -- report ``systemctl is-active
  dnsmasq.service`` (or a ``pgrep dnsmasq`` fallback) as a pure
  observability signal on ``/ui/netboot``. The UI no longer
  start/stop/restarts the daemon: that's a host- or container-
  lifecycle concern (systemd / Podman / Quadlet), not an
  operator click target. The container deploy serves TFTP from
  a separate sidecar; in that env this reports ``inactive`` and
  the UI's accompanying text explains why.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

SYSNET_PATH = Path("/sys/class/net")

# The systemd unit that owns the TFTP root on a host/systemd
# install. ``tftp_status`` queries this one; the container
# deploy's sidecar runs outside our visibility.
TFTP_UNIT = "dnsmasq.service"

# The TFTP probe target host is resolved from config
# (``Config.effective_tftp_probe_host``) and passed in by the caller --
# this module no longer reads an env var of its own, so there is one
# source of truth. The default port 69 is the IANA TFTP port and isn't
# expected to vary in homelab setups.
DEFAULT_TFTP_PROBE_PORT = 69

# Default filename to probe for. ``ipxe.efi`` is the UEFI iPXE
# bootfile bty's tftp sidecar always ships; if the operator's TFTP
# server doesn't have it, every UEFI PXE-Boot client on the LAN
# 404s on the first DHCP -> TFTP step.
DEFAULT_TFTP_PROBE_FILENAME = "ipxe.efi"


@dataclass(frozen=True)
class DaemonStatus:
    """systemctl-derived state for the TFTP-serving daemon.

    ``state`` is the literal output of ``systemctl is-active`` --
    typically one of ``active`` / ``inactive`` / ``failed`` /
    ``activating`` / ``deactivating``. ``unknown`` is used when
    the command itself fails (systemctl missing on a test host,
    unit doesn't exist at all). The string is surfaced raw in
    the UI as a badge label and CSS class.
    """

    state: str  # "active" | "inactive" | "failed" | "unknown" | ...

    @property
    def is_active(self) -> bool:
        """``True`` when systemd reports the unit as ``active``.
        Used by the template to disable the Start button while the
        daemon is up + the Stop button while it's down."""
        return self.state == "active"


def tftp_status() -> DaemonStatus:
    """Return the ``dnsmasq.service`` state.

    Uses ``systemctl is-active`` which does NOT require root --
    it just reads the unit state from dbus. When systemctl is
    missing (container deployments without systemd, test hosts),
    falls back to a ``pgrep -x dnsmasq`` check so the badge still
    reflects whether the TFTP daemon is actually running.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", TFTP_UNIT],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return DaemonStatus(state=(result.stdout or "").strip() or "unknown")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # No systemd around -- try pgrep, for a non-systemd host that
    # runs dnsmasq as a bare process. ``pgrep -x dnsmasq`` exits 0
    # when there's a process named exactly ``dnsmasq``, 1 otherwise.
    # In the container deploy bty-web serves no TFTP (the sidecar
    # does), so this reports ``inactive`` there -- expected.
    try:
        rc = subprocess.run(
            ["pgrep", "-x", "dnsmasq"],
            capture_output=True,
            check=False,
            timeout=5,
        ).returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return DaemonStatus(state="unknown")
    return DaemonStatus(state="active" if rc == 0 else "inactive")


@dataclass(frozen=True)
class TftpProbeResult:
    """Outcome of a single TFTP probe.

    The operator's question is "is the TFTP server reachable AND
    does it have the bootfile clients ask for?" -- two independent
    failure modes, each with its own remediation. The shape carries
    both so the UI can show either "no route to host" vs "host
    answered but file missing" without a second probe round-trip.

    * ``host`` / ``port`` / ``filename`` -- what was probed (echoed
      back so the UI doesn't have to recompute it).
    * ``reachable`` -- a TFTP RRQ for ``filename`` got SOMETHING
      back from the server (a DATA packet OR an ERROR packet); if
      False, the server didn't respond inside ``timeout_s``.
    * ``file_present`` -- the server answered with a DATA opcode (3)
      rather than ERROR (5). Only meaningful when ``reachable`` is
      True.
    * ``detail`` -- short human-readable explanation. The TFTP
      ERROR packet's text (if any) lands here when the file is
      missing; a socket-level error message lands here on
      unreachable.
    """

    host: str
    port: int
    filename: str
    reachable: bool
    file_present: bool
    detail: str

    @property
    def ok(self) -> bool:
        """True iff both legs of the probe succeeded."""
        return self.reachable and self.file_present


def tftp_probe(
    host: str,
    port: int = DEFAULT_TFTP_PROBE_PORT,
    filename: str = DEFAULT_TFTP_PROBE_FILENAME,
    timeout_s: float = 1.5,
) -> TftpProbeResult:
    """Send a TFTP RRQ for ``filename`` and report what came back.

    Implements RFC 1350 just far enough to round-trip one request:

    * Opcode 1 (RRQ) + filename + NUL + ``"octet"`` + NUL goes out
      as a single UDP datagram.
    * The first reply is enough to tell us: DATA (opcode 3) means
      the file exists, ERROR (opcode 5) means the server is up but
      the file isn't there (the ERROR carries an ASCII message
      after the 2-byte error code). No reply within ``timeout_s``
      means the server is unreachable (the port is dropped, the
      daemon isn't running, or the network path is blocked).

    We do NOT ack the DATA packet, so the server quietly drops the
    transfer state on its own retry timer. Cheap probe, no
    cleanup, no risk of leaking a half-open transfer.

    Errors that should never bubble to the UI as a 500 are caught
    and reported as ``reachable=False`` with the exception's
    message in ``detail``.
    """
    target = host.strip()
    # RRQ packet: \x00\x01 + filename + \x00 + "octet" + \x00
    pkt = b"\x00\x01" + filename.encode("ascii", errors="replace") + b"\x00octet\x00"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_s)
    try:
        sock.sendto(pkt, (target, port))
        data, _addr = sock.recvfrom(1024)
    except TimeoutError:
        return TftpProbeResult(
            host=target,
            port=port,
            filename=filename,
            reachable=False,
            file_present=False,
            detail=f"no reply from {target}:{port} within {timeout_s:.1f}s",
        )
    except OSError as exc:
        # Connection refused, no route, name resolution failure, etc.
        return TftpProbeResult(
            host=target,
            port=port,
            filename=filename,
            reachable=False,
            file_present=False,
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        sock.close()
    if len(data) < 2:
        return TftpProbeResult(
            host=target,
            port=port,
            filename=filename,
            reachable=True,
            file_present=False,
            detail=f"server replied with {len(data)} bytes (truncated)",
        )
    opcode = struct.unpack("!H", data[:2])[0]
    if opcode == 3:  # DATA
        return TftpProbeResult(
            host=target,
            port=port,
            filename=filename,
            reachable=True,
            file_present=True,
            detail=f"server returned a DATA packet ({len(data)} bytes)",
        )
    if opcode == 5:  # ERROR
        # ERROR = opcode(2) + errcode(2) + ASCIIZ message
        msg = data[4:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return TftpProbeResult(
            host=target,
            port=port,
            filename=filename,
            reachable=True,
            file_present=False,
            detail=f"server returned ERROR: {msg or '(no message)'}",
        )
    return TftpProbeResult(
        host=target,
        port=port,
        filename=filename,
        reachable=True,
        file_present=False,
        detail=f"server returned unexpected opcode {opcode}",
    )


@dataclass(frozen=True)
class Interface:
    name: str
    operstate: str  # "up" / "down" / "unknown"
    # First IPv4 address + prefix on the interface, or ``None`` when
    # the interface has no v4 address.
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
        operstate = (
            operstate_path.read_text(encoding="utf-8").strip()
            if operstate_path.is_file()
            else "unknown"
        )
        ipv4, prefix = _first_ipv4(entry.name)
        out.append(Interface(name=entry.name, operstate=operstate, ipv4=ipv4, prefix=prefix))
    return out


def _first_ipv4(iface: str) -> tuple[str | None, int | None]:
    """Return ``(address, prefix)`` for the interface's first IPv4 address.

    Uses ``ip -j addr show <iface>`` and picks the first ``inet`` entry.
    ``(None, None)`` when the tool is missing, the interface has no
    addresses, or the JSON shape doesn't match expectations - the UI
    treats those as "no info".
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
