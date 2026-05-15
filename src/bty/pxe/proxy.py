"""asyncio proxy-DHCP daemon: listen on UDP 67, answer PXE clients.

The daemon stays narrowly scoped:

* It does NOT allocate IPs. The operator's existing DHCP server
  (a router, pfSense, ISC dhcpd, whatever) keeps doing that.
* It only emits ``DHCPOFFER`` messages, never ``ACK`` / ``NAK``.
* It only answers clients identifying via option 60 as
  ``PXEClient`` or ``HTTPClient``. Everything else is dropped
  silently -- it's not ours to respond to.

The decision rule (RFC 4578 + experience):

  PXEClient + arch 0  -> ``undionly.kpxe`` via TFTP (legacy BIOS)
  PXEClient + arch 6  -> ``ipxe.efi`` via TFTP (UEFI IA32)
  PXEClient + arch 7  -> ``ipxe.efi`` via TFTP (UEFI BC; common)
  PXEClient + arch 9  -> ``ipxe.efi`` via TFTP (UEFI x86_64)
  PXEClient + arch 11 -> ``ipxe.efi`` via TFTP (UEFI ARM64; placeholder)
  HTTPClient *        -> ``http://<server>:8080/boot/ipxe.efi`` (UEFI HTTP)

The bootfile lands in BOTH the BOOTP ``file[]`` field AND option 67
(bootfile-name) -- modern UEFI firmware reads one or the other, so
we set both to avoid the dnsmasq-proxy-DHCP class of failure where
the bootfile only appears in option 43 PXE sub-options that the
client doesn't parse.

Privileges: binding UDP 67 needs root or
``CAP_NET_BIND_SERVICE``. The systemd unit will run the process
under one of those, drop to the ``bty`` user after bind. Until
that's wired, run via ``sudo bty-pxe-proxy --interface enp90s0``.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import signal
import socket
import sys

from bty.pxe import wire
from bty.pxe.wire import MsgType, Op, Opt, Packet

log = logging.getLogger("bty.pxe.proxy")

# Arch -> (bootfile, transport-hint). For PXEClient (TFTP) we serve
# the bootfile by name relative to dnsmasq's tftp-root. For HTTPClient
# we serve an absolute URL the firmware fetches directly over HTTP.
_PXE_BOOTFILE_BY_ARCH: dict[int, str] = {
    0: "undionly.kpxe",  # legacy BIOS x86
    6: "ipxe.efi",  # UEFI IA32 (uncommon but seen)
    7: "ipxe.efi",  # UEFI BC (x86_64 byte code) -- the common case
    9: "ipxe.efi",  # UEFI x86_64 native
    11: "ipxe.efi",  # UEFI ARM64; placeholder until we ship arm64.efi
}

# bty-web's HTTP route for the iPXE binary. The path matches the
# /boot/{name} endpoint already in bty-web; the activate helper +
# release pipeline put ipxe.efi at this URL.
_HTTP_BOOTFILE_PATH = "/boot/ipxe.efi"
_HTTP_BOOTFILE_PORT = 8080


class ProxyConfig:
    """Resolved daemon config: which interface to listen on + the
    IP to advertise. Kept as a plain class (not a dataclass) so
    the runtime can mutate it on interface flap if we add that
    later. For now it's set at startup and read-only."""

    __slots__ = ("http_port", "interface", "server_ip", "tftp_only")

    def __init__(
        self,
        *,
        interface: str,
        server_ip: str,
        http_port: int = _HTTP_BOOTFILE_PORT,
        tftp_only: bool = False,
    ) -> None:
        self.interface = interface
        self.server_ip = server_ip
        self.http_port = http_port
        # ``tftp_only`` makes the daemon refuse HTTPClient discovers.
        # Useful when the appliance hasn't staged the HTTP-Boot binary
        # at /boot/ipxe.efi yet -- better to fail loud than offer a URL
        # that 404s.
        self.tftp_only = tftp_only


def _resolve_bootfile(
    cfg: ProxyConfig,
    vendor_class: bytes,
    arch: int | None,
    is_ipxe: bool,
) -> tuple[bytes, bytes] | None:
    """Pick the right bootfile bytes for the client's arch + class.

    Returns ``(bootfile_bytes, vendor_class_response_bytes)`` or
    ``None`` when we don't have a sensible answer (unknown arch,
    HTTPClient when tftp_only is set, etc). ``vendor_class_response``
    must echo the client's class -- a HTTPClient that gets a
    "PXEClient" offer back will reject it.
    """
    if is_ipxe:
        # iPXE second-stage: chain to bty-web's bootstrap script.
        # Tag the offer as PXEClient (iPXE itself sends that vendor
        # class on its second-stage DHCP).
        url = f"http://{cfg.server_ip}:{cfg.http_port}/pxe-bootstrap.ipxe"
        return url.encode("ascii"), b"PXEClient"
    if vendor_class.startswith(b"HTTPClient"):
        if cfg.tftp_only:
            return None
        # UEFI HTTP-Boot. The bootfile MUST be an absolute URL and
        # the response's option 60 MUST be "HTTPClient" -- firmware
        # rejects PXEClient-tagged offers when it asked HTTP.
        url = f"http://{cfg.server_ip}:{cfg.http_port}{_HTTP_BOOTFILE_PATH}"
        return url.encode("ascii"), b"HTTPClient"
    # PXEClient: pick a TFTP bootfile by arch. Some BIOS-era
    # clients don't send option 93; treat missing arch as 0 (legacy
    # BIOS x86) so they still get a sensible bootfile.
    arch_key = arch if arch is not None else 0
    bootfile = _PXE_BOOTFILE_BY_ARCH.get(arch_key)
    if bootfile is None:
        log.warning("pxe: no bootfile mapping for arch=%d", arch_key)
        return None
    return bootfile.encode("ascii"), b"PXEClient"


def build_offer(cfg: ProxyConfig, discover: Packet) -> bytes | None:
    """Build a proxy DHCPOFFER reply for a client's DISCOVER.

    Returns the wire bytes, or ``None`` when we shouldn't reply
    (filter mismatch, unknown arch, etc). Pure function -- no
    network I/O. The daemon dispatches the bytes; tests can call
    this directly with constructed discovers.
    """
    if not wire.is_pxe_client_discover(discover):
        return None
    vc = discover.vendor_class or b""
    is_ipxe = (discover.user_class or b"") == b"iPXE"
    result = _resolve_bootfile(cfg, vc, discover.client_arch, is_ipxe)
    if result is None:
        return None
    bootfile, vc_response = result

    server_ip_bytes = socket.inet_aton(cfg.server_ip)

    # Options in send-order. Modern UEFI is picky about a few of
    # these being present:
    #   53 (msg-type) first -- always the discoverable bit.
    #   54 (server-id) -- "who is the DHCP server"; required by RFC.
    #   60 (vendor-class) -- MUST echo "PXEClient" or "HTTPClient"
    #     depending on what the client sent.
    #   66 (tftp-server) -- redundant with siaddr for most firmware
    #     but some implementations only check this. Cheap insurance.
    #   67 (bootfile-name) -- the actual answer. modern UEFI firmware
    #     reads this for PXEClient and HTTPClient alike. Length can
    #     exceed 64 bytes for an http:// URL, so we MUST put it in
    #     option 67 (not the 128-byte BOOTP file[] field alone --
    #     even though we also fill file[] below for legacy clients).
    #   97 (client-machine-id) -- echo client's GUID back when
    #     present. Some firmware uses it as a sanity check.
    options: dict[int, bytes] = {
        Opt.MSG_TYPE: bytes((MsgType.OFFER,)),
        Opt.SERVER_ID: server_ip_bytes,
        Opt.VENDOR_CLASS: vc_response,
        Opt.TFTP_SERVER_NAME: cfg.server_ip.encode("ascii"),
        Opt.BOOTFILE_NAME: bootfile,
    }
    if Opt.CLIENT_MACHINE_ID in discover.options:
        options[Opt.CLIENT_MACHINE_ID] = discover.options[Opt.CLIENT_MACHINE_ID]

    # file[] is a 128-byte fixed-position field; URLs longer than
    # that have to live in option 67 only. We truncate-with-warning
    # rather than silently losing bytes.
    file_field = bootfile[:128]
    if len(bootfile) > 128 and bootfile != file_field:
        # The URL got truncated for the legacy BOOTP file[] field,
        # but the full URL is still in option 67 -- which is what
        # modern firmware reads.
        log.debug(
            "pxe: bootfile too long for file[] (%d > 128 bytes); only option 67 carries it",
            len(bootfile),
        )

    packet = wire.build(
        op=Op.BOOTREPLY,
        xid=discover.xid,
        secs=discover.secs,
        flags=discover.flags,  # echo client's broadcast bit
        # Don't allocate -- yiaddr stays 0. siaddr = our IP (TFTP
        # server / next-server).
        siaddr=server_ip_bytes,
        chaddr=discover.chaddr,
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
        try:
            offer = build_offer(self._cfg, discover)
        except Exception as exc:
            log.exception("pxe: build_offer crashed (xid=%#x): %s", discover.xid, exc)
            return
        if offer is None:
            # Not for us, or no answer we can give.
            return
        assert self._transport is not None
        # Broadcast the reply -- the client has no IP yet so we
        # can't unicast.
        self._transport.sendto(offer, ("255.255.255.255", 68))
        mac_pretty = ":".join(f"{b:02x}" for b in discover.mac)
        log.info(
            "pxe: offered %s to %s (arch=%s, vendor-class=%s, xid=%#x)",
            offer[44 + 64 : 44 + 64 + 128].rstrip(b"\x00").decode("ascii", errors="replace")
            or "<file[]-empty>",
            mac_pretty,
            discover.client_arch,
            (discover.vendor_class or b"").decode("ascii", errors="replace"),
            discover.xid,
        )

    def error_received(self, exc: Exception) -> None:
        log.warning("pxe: socket error: %s", exc)


def _detect_interface_ip(interface: str) -> str:
    """Return the IPv4 address on ``interface``. Linux-only (uses
    ``ioctl(SIOCGIFADDR)``). Raises ``OSError`` when the interface
    is missing or has no IPv4."""
    SIOCGIFADDR = 0x8915
    import fcntl

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifname_padded = interface.encode("ascii")[:15].ljust(256, b"\x00")
        result = fcntl.ioctl(s.fileno(), SIOCGIFADDR, ifname_padded)
    finally:
        s.close()
    # The struct sockaddr_in's IPv4 bytes live at offset 20..24.
    ip = socket.inet_ntoa(result[20:24])
    return ip


def _bind_udp67_broadcast_socket(interface: str) -> socket.socket:
    """Open the listener socket explicitly so we can set the various
    flags asyncio's ``create_datagram_endpoint`` doesn't expose.

    SO_REUSEADDR  -- coexist with anything else on UDP 67 transiently
                     (e.g., dnsmasq during cutover).
    SO_BROADCAST  -- required to send 255.255.255.255 broadcasts.
    SO_BINDTODEVICE -- pin to the operator-selected interface so we
                      don't accidentally answer discovers arriving on
                      a different NIC (the appliance often has more
                      than one).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # SO_BINDTODEVICE needs CAP_NET_RAW or root; if we already need
    # root for port 67 anyway, this is "free" from a privilege POV.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode("ascii"))
    s.bind(("0.0.0.0", 67))
    s.setblocking(False)
    return s


async def _serve(cfg: ProxyConfig) -> None:
    loop = asyncio.get_running_loop()
    sock = _bind_udp67_broadcast_socket(cfg.interface)
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DhcpServerProtocol(cfg),
            sock=sock,
        )
    except Exception:
        sock.close()
        raise
    stop = asyncio.Event()

    def _sig_handler(signum: int) -> None:
        log.info("pxe: signal %d received; stopping", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sig_handler, sig)

    try:
        await stop.wait()
    finally:
        transport.close()


def _parse_args(argv: list[str]) -> ProxyConfig:
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
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server_ip = ns.server_ip
    if server_ip is None:
        try:
            server_ip = _detect_interface_ip(ns.interface)
        except OSError as exc:
            parser.error(f"could not auto-detect IP on {ns.interface}: {exc}")
        log.info("pxe: auto-detected --server-ip=%s on %s", server_ip, ns.interface)
    # Validate the IP shape -- a typo here would let us serve
    # garbage as next-server.
    try:
        ipaddress.IPv4Address(server_ip)
    except ValueError as exc:
        parser.error(f"invalid --server-ip {server_ip!r}: {exc}")
    return ProxyConfig(
        interface=ns.interface,
        server_ip=server_ip,
        http_port=ns.http_port,
        tftp_only=ns.tftp_only,
    )


def main(argv: list[str] | None = None) -> int:
    """``bty-pxe-proxy`` console-script entry."""
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
