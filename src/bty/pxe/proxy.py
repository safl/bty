"""asyncio proxy-DHCP daemon: listen on UDP 67, answer PXE clients.

The daemon stays narrowly scoped:

* It does NOT allocate IPs. The operator's existing DHCP server
  (a router, pfSense, ISC dhcpd, whatever) keeps doing that.
* It only emits ``DHCPOFFER`` messages, never ``ACK`` / ``NAK``.
* It only answers clients identifying via option 60 as
  ``PXEClient`` or ``HTTPClient``. Everything else is dropped
  silently -- it's not ours to respond to.

The decision rule (RFC 4578 + experience):

  PXEClient + arch 0  -> ``undionly.kpxe``       via TFTP (legacy BIOS)
  PXEClient + arch 6  -> ``ipxe-i386.efi``       via TFTP (UEFI IA32)
  PXEClient + arch 7  -> ``ipxe.efi``            via TFTP (UEFI BC; common)
  PXEClient + arch 9  -> ``ipxe.efi``            via TFTP (UEFI x86_64)
  PXEClient + arch 10 -> ``ipxe-arm32.efi``      via TFTP (UEFI ARM 32)
  PXEClient + arch 11 -> ``ipxe-arm64.efi``      via TFTP (UEFI ARM 64)
  iPXE second-stage   -> ``http://<server>:8080/pxe-bootstrap.ipxe``
  HTTPClient *        -> ``http://<server>:8080/boot/ipxe.efi`` (UEFI HTTP)

For arches whose binary isn't on disk at /var/lib/tftpboot, the
offer still goes out and TFTP cleanly answers FILE_NOT_FOUND --
the target falls through to the operator's main DHCP.

The bootfile lands in BOTH the BOOTP ``file[]`` field AND option 67
(bootfile-name) -- modern UEFI firmware reads one or the other, so
we set both to avoid the dnsmasq-proxy-DHCP class of failure where
the bootfile only appears in option 43 PXE sub-options that the
client doesn't parse.

Privileges: binding UDP 67 needs ``CAP_NET_BIND_SERVICE`` (port
< 1024) + ``CAP_NET_RAW`` (for SO_BINDTODEVICE). The systemd unit
grants both as ambient capabilities and runs the daemon as the
``bty`` user.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import socket
import sys
from dataclasses import dataclass
from typing import Literal

from bty.pxe import wire
from bty.pxe._daemon import bind_udp_socket, run_udp_daemon, setup_daemon_logging
from bty.pxe._events import emit as emit_event
from bty.pxe.wire import MsgType, Op, Opt, Packet

log = logging.getLogger("bty.pxe.proxy")

# Linux IFNAMSIZ-1 is 15. The kernel rejects anything longer at
# SO_BINDTODEVICE time; we reject early so the operator sees a
# clear argparse error instead of a confusing OSError later.
# Character set mirrors the activate-helper's validation -- one
# place that calls this binary, one source of truth.
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,15}$")

# Arch -> bootfile (RFC 4578 client-arch numbers).
#
# Each entry MUST point at a binary that actually matches the
# arch -- sending an x86_64 iPXE binary to an ARM64 UEFI client
# would let the offer flow succeed (option 67 set + TFTP serves
# the bytes) only to fail at execution on the target, which is
# strictly worse than no offer at all. If the operator hasn't
# staged a particular arch's iPXE binary at /var/lib/tftpboot/,
# TFTP returns FILE_NOT_FOUND and the target falls through to
# the operator's main DHCP -- a clean "no, not me" signal.
#
# Bty's default appliance ships ipxe.efi (x86-64 UEFI) +
# undionly.kpxe (legacy BIOS) only. Other arches need their
# iPXE binaries added by the operator -- see the RPi-over-PXE
# notes in src/bty/pxe/__init__.py.
_PXE_BOOTFILE_BY_ARCH: dict[int, str] = {
    0: "undionly.kpxe",  # legacy BIOS x86
    6: "ipxe-i386.efi",  # UEFI IA32 (uncommon but seen)
    7: "ipxe.efi",  # UEFI BC (x86-64 byte code; common)
    9: "ipxe.efi",  # UEFI x86-64 native
    10: "ipxe-arm32.efi",  # UEFI ARM 32-bit (rare)
    11: "ipxe-arm64.efi",  # UEFI ARM 64-bit (Raspberry Pi, etc.)
}

# bty-web HTTP paths the proxy points clients at.
#
# * ``_HTTP_BOOTFILE_PATH`` -- the iPXE binary itself, served by
#   bty-web's /boot/{name} route. Used for UEFI HTTP-Boot
#   (HTTPClient discovers, arch 16): firmware downloads ipxe.efi
#   directly over HTTP, no TFTP roundtrip.
# * ``_PXE_BOOTSTRAP_PATH`` -- the iPXE chain script bty-web
#   renders for second-stage discovers. After ipxe.efi loads
#   (via either TFTP or HTTP-Boot) it re-DHCPs with user-class
#   "iPXE"; we point it here for the per-MAC boot plan.
_HTTP_BOOTFILE_PATH = "/boot/ipxe.efi"
_PXE_BOOTSTRAP_PATH = "/pxe-bootstrap.ipxe"
_HTTP_BOOTFILE_PORT = 8080

# PXE vendor-specific sub-option payload for option 43.
#
# Old PXE 2.x firmware reads option 43 for PXE_DISCOVERY_CONTROL
# (sub-option 6) before honouring the bootfile-name option. We
# set bits 2+3 -- "skip multicast / broadcast PXE discovery; use
# the bootfile in option 67 / BOOTP file[] directly". Modern UEFI
# ignores option 43 entirely, so emitting this is pure leniency
# toward legacy hardware; no downside on current clients.
#
# Layout: ``sub_code(1) + sub_len(1) + value + ... + END(0xff)``.
# Sub-option 6 (PXE_DISCOVERY_CONTROL) takes a 1-byte value.
_PXE_VENDOR_OPT_BODY: bytes = bytes((6, 1, 0x0C, 0xFF))


@dataclass(frozen=True)
class ProxyConfig:
    """Resolved daemon config: which interface to listen on + the
    IP to advertise.

    ``tftp_only`` makes the daemon refuse HTTPClient discovers --
    useful when the appliance hasn't staged the HTTP-Boot binary at
    /boot/ipxe.efi yet (better to fail loud than offer a URL that
    404s). Default off: when bty-web is up, /boot/ipxe.efi is
    served and HTTPClient targets boot cleanly.
    """

    interface: str
    server_ip: str
    http_port: int = _HTTP_BOOTFILE_PORT
    tftp_only: bool = False

    @property
    def server_ip_bytes(self) -> bytes:
        """Packed 4-byte server IP, ready to drop into BOOTP siaddr
        / DHCP option 54. Frozen-dataclass-friendly: computed every
        call but the call is one ``inet_aton``."""
        return socket.inet_aton(self.server_ip)


@dataclass(frozen=True)
class BootDecision:
    """Outcome of ``_resolve_bootfile``: what to stamp into the
    offer + how to tag the response.

    ``vendor_class_response`` MUST echo the client's class --
    HTTPClient firmware rejects offers tagged ``PXEClient`` and
    vice versa. Cheap to mess up, hence the explicit field.
    """

    bootfile: bytes
    vendor_class_response: bytes


# Tagged reasons for declining to offer; surfaced verbatim as the
# ``reason`` field of the dhcp.ignore event so operators can tell
# "unknown arch" from "operator disabled HTTP-Boot" at a glance.
# Literal alias lets type checkers exhaustiveness-check ``match``
# statements + flag typos at the call site.
IgnoreReason = Literal["unknown_arch", "http_disabled"]
IGNORE_UNKNOWN_ARCH: IgnoreReason = "unknown_arch"
IGNORE_HTTP_DISABLED: IgnoreReason = "http_disabled"


def _http_url(cfg: ProxyConfig, path: str) -> bytes:
    """Build an ASCII URL pointing at a bty-web HTTP route. Returns
    bytes so the caller can drop it straight into ``BootDecision``
    without an extra ``.encode``."""
    return f"http://{cfg.server_ip}:{cfg.http_port}{path}".encode("ascii")


def _resolve_bootfile(
    cfg: ProxyConfig,
    vendor_class: bytes,
    arch: int | None,
    is_ipxe: bool,
) -> BootDecision | IgnoreReason:
    """Pick the right bootfile bytes for the client's arch + class.

    Returns a :class:`BootDecision` on success, or one of the
    ``IGNORE_*`` reason strings when we decline to offer.
    """
    if is_ipxe:
        # iPXE second-stage: chain to bty-web's bootstrap script.
        # Tag the offer as PXEClient (iPXE itself sends that vendor
        # class on its second-stage DHCP).
        return BootDecision(
            bootfile=_http_url(cfg, _PXE_BOOTSTRAP_PATH),
            vendor_class_response=b"PXEClient",
        )
    if vendor_class.startswith(b"HTTPClient"):
        if cfg.tftp_only:
            return IGNORE_HTTP_DISABLED
        # UEFI HTTP-Boot. The bootfile MUST be an absolute URL and
        # the response's option 60 MUST be "HTTPClient" -- firmware
        # rejects PXEClient-tagged offers when it asked HTTP.
        return BootDecision(
            bootfile=_http_url(cfg, _HTTP_BOOTFILE_PATH),
            vendor_class_response=b"HTTPClient",
        )
    # PXEClient: pick a TFTP bootfile by arch. Some BIOS-era
    # clients don't send option 93; treat missing arch as 0 (legacy
    # BIOS x86) so they still get a sensible bootfile.
    arch_key = arch if arch is not None else 0
    bootfile = _PXE_BOOTFILE_BY_ARCH.get(arch_key)
    if bootfile is None:
        log.warning("pxe: no bootfile mapping for arch=%d", arch_key)
        return IGNORE_UNKNOWN_ARCH
    return BootDecision(bootfile=bootfile.encode("ascii"), vendor_class_response=b"PXEClient")


def build_offer(cfg: ProxyConfig, discover: Packet) -> bytes | None:
    """Build a proxy DHCPOFFER reply for a client's DISCOVER.

    Returns the wire bytes, or ``None`` when we shouldn't reply
    (filter mismatch, unknown arch, etc). Pure function -- no
    network I/O. The daemon dispatches the bytes; tests can call
    this directly with constructed discovers.
    """
    if not wire.is_pxe_client_discover(discover):
        return None
    decision = _resolve_bootfile(
        cfg,
        vendor_class=discover.vendor_class or b"",
        arch=discover.client_arch,
        is_ipxe=(discover.user_class or b"") == b"iPXE",
    )
    if not isinstance(decision, BootDecision):
        return None
    return _build_offer_packet(cfg, discover, decision)


def _build_offer_packet(cfg: ProxyConfig, discover: Packet, decision: BootDecision) -> bytes:
    """Assemble the wire-format DHCPOFFER. Split out from
    :func:`build_offer` so the decision-vs-wire-assembly halves
    can be unit-tested separately and the daemon can log the
    decision without re-parsing the bytes."""
    # Options in send-order. Modern UEFI is picky about a few of
    # these being present:
    #   53 (msg-type) first -- always the discoverable bit.
    #   54 (server-id) -- "who is the DHCP server"; required by RFC.
    #   60 (vendor-class) -- MUST echo "PXEClient" or "HTTPClient"
    #     depending on what the client sent.
    #   43 (vendor-specific) -- PXE_DISCOVERY_CONTROL hint for old
    #     PXE 2.x firmware (see _PXE_VENDOR_OPT_BODY); modern UEFI
    #     ignores it.
    #   66 (tftp-server) -- redundant with siaddr for most firmware
    #     but some implementations only check this. Cheap insurance.
    #   67 (bootfile-name) -- the actual answer. modern UEFI firmware
    #     reads this for PXEClient and HTTPClient alike. Length can
    #     exceed 64 bytes for an http:// URL, so we MUST put it in
    #     option 67 (not the 128-byte BOOTP file[] field alone --
    #     even though we also fill file[] below for legacy clients).
    #   97 (client-machine-id) -- echo client's GUID back when
    #     present. Some firmware uses it as a sanity check.
    server_ip_bytes = cfg.server_ip_bytes
    options: dict[int, bytes] = {
        Opt.MSG_TYPE: bytes((MsgType.OFFER,)),
        Opt.SERVER_ID: server_ip_bytes,
        Opt.VENDOR_CLASS: decision.vendor_class_response,
        Opt.VENDOR_SPECIFIC: _PXE_VENDOR_OPT_BODY,
        Opt.TFTP_SERVER_NAME: cfg.server_ip.encode("ascii"),
        Opt.BOOTFILE_NAME: decision.bootfile,
    }
    if Opt.CLIENT_MACHINE_ID in discover.options:
        options[Opt.CLIENT_MACHINE_ID] = discover.options[Opt.CLIENT_MACHINE_ID]

    # file[] is a 128-byte fixed-position field; URLs longer than
    # that have to live in option 67 only. We truncate rather than
    # silently losing bytes -- modern firmware reads option 67
    # anyway.
    file_field = decision.bootfile[:128]
    if len(decision.bootfile) > 128:
        log.debug(
            "pxe: bootfile too long for file[] (%d > 128 bytes); option 67 carries it",
            len(decision.bootfile),
        )

    # Populate the BOOTP sname (server-name) field with our IP as
    # an ASCII string. Modern UEFI ignores sname entirely, but some
    # PXE 2.x firmware reads it as "boot server hostname"; an empty
    # sname pushed those clients to a fallback path that sometimes
    # didn't work. ``server_ip`` is always ASCII-clean and short.
    sname_field = cfg.server_ip.encode("ascii")
    packet = wire.build(
        op=Op.BOOTREPLY,
        xid=discover.xid,
        secs=discover.secs,
        flags=discover.flags,  # echo client's broadcast bit
        # Don't allocate -- yiaddr stays 0. siaddr = our IP (TFTP
        # server / next-server).
        siaddr=server_ip_bytes,
        chaddr=discover.chaddr,
        sname=sname_field,
        file=file_field,
        options=options,
    )
    return wire.pad_to_min(packet, 300)


class _DhcpServerProtocol(asyncio.DatagramProtocol):
    """asyncio glue. ``datagram_received`` -> parse -> ``build_offer``
    -> sendto. Errors get logged + dropped; we never let a single
    bad packet take the daemon down."""

    def __init__(self, cfg: ProxyConfig) -> None:
        self._cfg = cfg
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport
        log.info(
            "bty-pxe-proxy: listening on %s (UDP 67); advertising %s as the boot server",
            self._cfg.interface,
            self._cfg.server_ip,
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            discover = wire.parse(data)
        except ValueError as exc:
            log.debug("pxe: malformed DHCP packet from %s: %s", addr, exc)
            return
        if not wire.is_pxe_client_discover(discover):
            return
        mac_pretty = discover.mac_pretty
        vendor_class = (discover.vendor_class or b"").decode("ascii", errors="replace")
        user_class = (discover.user_class or b"").decode("ascii", errors="replace")
        emit_event(
            "dhcp.discover",
            mac=mac_pretty,
            arch=discover.client_arch,
            vendor_class=vendor_class,
            user_class=user_class or None,
        )
        # One defensive try: a single bad packet must never take
        # the daemon down.
        try:
            decision = _resolve_bootfile(
                self._cfg,
                vendor_class=discover.vendor_class or b"",
                arch=discover.client_arch,
                is_ipxe=(discover.user_class or b"") == b"iPXE",
            )
            if not isinstance(decision, BootDecision):
                emit_event(
                    "dhcp.ignore",
                    mac=mac_pretty,
                    arch=discover.client_arch,
                    reason=decision,
                )
                return
            offer = _build_offer_packet(self._cfg, discover, decision)
        except Exception as exc:
            log.exception("pxe: failed to build offer (xid=%#x): %s", discover.xid, exc)
            emit_event("dhcp.error", mac=mac_pretty, error=str(exc))
            return
        assert self._transport is not None
        # Broadcast the reply -- the client has no IP yet so we
        # can't unicast.
        self._transport.sendto(offer, ("255.255.255.255", 68))
        bootfile_str = decision.bootfile.decode("ascii", errors="replace")
        log.info(
            "pxe: offered %s to %s (arch=%s, vendor-class=%s, xid=%#x)",
            bootfile_str,
            mac_pretty,
            discover.client_arch,
            vendor_class,
            discover.xid,
        )
        emit_event(
            "dhcp.offer",
            mac=mac_pretty,
            arch=discover.client_arch,
            bootfile=bootfile_str,
            server_ip=self._cfg.server_ip,
        )

    def error_received(self, exc: Exception) -> None:
        log.warning("pxe: socket error: %s", exc)


def _detect_interface_ip(interface: str) -> str:
    """Return the IPv4 address on ``interface``. Linux-only (uses
    ``ioctl(SIOCGIFADDR)``). Raises ``OSError`` when the interface
    is missing or has no IPv4."""
    # Linux struct ifreq on 64-bit: IFNAMSIZ (16) + the largest
    # union member (24, struct ifmap). 40 bytes is the minimum
    # fcntl.ioctl needs to round-trip SIOCGIFADDR; the kernel
    # writes the sockaddr response back into the same buffer.
    SIOCGIFADDR = 0x8915
    IFREQ_LEN = 40
    import fcntl

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifname_padded = interface.encode("ascii")[:15].ljust(IFREQ_LEN, b"\x00")
        result = fcntl.ioctl(s.fileno(), SIOCGIFADDR, ifname_padded)
    finally:
        s.close()
    # The struct sockaddr_in's IPv4 bytes live at offset 20..24.
    ip = socket.inet_ntoa(result[20:24])
    return ip


async def _serve(cfg: ProxyConfig) -> None:
    sock = bind_udp_socket(67, interface=cfg.interface, broadcast=True)
    await run_udp_daemon(sock, lambda: _DhcpServerProtocol(cfg), log_prefix="pxe")


def _parse_args(argv: list[str]) -> tuple[ProxyConfig, bool]:
    """Parse + validate command-line args. Returns the resolved
    config plus a verbose-flag the caller uses to set up logging.
    Pure: no side effects on global state (logging, env)."""
    parser = argparse.ArgumentParser(
        prog="bty-pxe-proxy",
        description=(
            "Proxy-DHCP daemon for bty. Listens on UDP 67 for "
            "PXEClient/HTTPClient discovers and replies with offers "
            "carrying the bootfile inline. Does NOT allocate IPs -- "
            "your existing DHCP server keeps doing that."
        ),
    )
    parser.add_argument(
        "--interface",
        required=True,
        help="Network interface to listen on (e.g. enp90s0).",
    )
    parser.add_argument(
        "--server-ip",
        default=None,
        help=("IP to advertise as the boot server. Auto-detected from the interface when omitted."),
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=_HTTP_BOOTFILE_PORT,
        help="Port bty-web serves boot artefacts on (default 8080).",
    )
    parser.add_argument(
        "--tftp-only",
        action="store_true",
        help="Refuse HTTPClient (UEFI HTTP-Boot) discovers; respond only to PXE.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose (debug-level) logging.",
    )
    ns = parser.parse_args(argv)
    if not _INTERFACE_RE.fullmatch(ns.interface):
        parser.error(f"invalid --interface name: {ns.interface!r}")
    server_ip = ns.server_ip
    if server_ip is None:
        try:
            server_ip = _detect_interface_ip(ns.interface)
        except OSError as exc:
            parser.error(f"could not auto-detect IP on {ns.interface}: {exc}")
    # Validate the IP shape -- a typo here would let us serve
    # garbage as next-server. ``inet_aton`` rejects anything that
    # isn't a dotted-quad IPv4.
    try:
        socket.inet_aton(server_ip)
    except OSError as exc:
        parser.error(f"invalid --server-ip {server_ip!r}: {exc}")
    cfg = ProxyConfig(
        interface=ns.interface,
        server_ip=server_ip,
        http_port=ns.http_port,
        tftp_only=ns.tftp_only,
    )
    return cfg, ns.verbose


def main(argv: list[str] | None = None) -> int:
    """``bty-pxe-proxy`` console-script entry."""
    cfg, verbose = _parse_args(sys.argv[1:] if argv is None else argv)
    setup_daemon_logging(verbose)
    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
