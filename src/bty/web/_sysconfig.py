"""System-config helpers for the ``/ui/boot`` page (DHCP / PXE +
TFTP daemon sub-sections).

What's here:

* :func:`list_interfaces` -- enumerate non-loopback network
  interfaces with their IPv4 / operstate. Used to suggest the
  bty-server's own IP to the operator in the router-config
  cheatsheet on /ui/boot?section=dhcp-pxe.
* :func:`tftp_status` / :func:`control_tftp` -- read ``systemctl
  is-active dnsmasq.service`` and shell out to a sudo'd helper
  for start / stop / restart. dnsmasq serves the appliance's
  TFTP root; the operator may want to stop or restart it from
  the UI without SSHing in (the buttons live on
  /ui/boot?section=tftp).

What used to be here (v0.14 - v0.17.1, removed in v0.18):
proxy-DHCP activate / deactivate helpers, per-daemon journald
event reader. bty no longer runs its own DHCP proxy or TFTP
daemon -- the operator's router owns DHCP (with PXE option
tagging), dnsmasq serves TFTP from the same appliance.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

SYSNET_PATH = Path("/sys/class/net")
TFTP_HELPER = "/usr/local/sbin/bty-web-tftp"

# The systemd unit that owns the TFTP root on the appliance. One
# constant + the helper script that hardcodes the same name --
# kept in lockstep on purpose; ``tftp_status`` shouldn't be
# pointing at a different unit than the helper restarts.
TFTP_UNIT = "dnsmasq.service"

# Allowlist of actions the helper accepts. Mirrored on the Python
# side so a typo here fails fast with a clean SysConfigError
# instead of getting a confusing systemctl error from the helper.
TFTP_ACTIONS: tuple[str, ...] = ("start", "stop", "restart")


class SysConfigError(Exception):
    """Helper failed; the message is safe to surface to the UI."""


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
    # No systemd around -- try pgrep. Useful inside the bty-web
    # Docker container where dnsmasq is launched by the entrypoint
    # and systemd isn't running. ``pgrep -x dnsmasq`` exits 0 when
    # there's a process named exactly ``dnsmasq``, 1 otherwise.
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


def tftp_controllable() -> bool:
    """``True`` when the UI can offer Start / Stop / Restart for
    the TFTP daemon. Requires the sudo'd helper to be installed
    AND ``sudo`` itself on PATH.

    Inside the bty-web Docker container neither is present (the
    container's dnsmasq is supervised by the entrypoint, not by
    systemd, and the bty user has no sudo grant); on the bty-server
    appliance both are present from the bake. The UI hides the
    Start/Stop/Restart buttons when this returns False so the
    operator isn't offered controls that would fail.
    """
    return Path(TFTP_HELPER).is_file() and (
        Path("/usr/bin/sudo").is_file() or Path("/bin/sudo").is_file()
    )


def control_tftp(action: str) -> None:
    """Invoke the TFTP daemon-control helper (sudo'd) to
    start / stop / restart ``dnsmasq.service``.

    The helper validates ``action`` against the same allowlist
    we check here; both sides keeping the allowlist means a typo
    on either side fails fast with a clear error instead of an
    unexpected systemctl invocation as root.
    """
    if not action:
        # Surfaces nicely instead of the generic "unknown action:
        # ''" path. The form field arriving empty would otherwise
        # show as an empty-quoted error in the flash.
        raise SysConfigError("no action specified")
    if action not in TFTP_ACTIONS:
        raise SysConfigError(f"unknown action: {action!r} (allowed: {', '.join(TFTP_ACTIONS)})")
    try:
        subprocess.run(
            ["sudo", "-n", TFTP_HELPER, action],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise SysConfigError(
            f"tftp helper exited {exc.returncode}: {(exc.stderr or '').strip()}"
        ) from exc
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise SysConfigError(f"tftp helper failed: {exc}") from exc


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
