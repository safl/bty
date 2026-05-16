"""BOOTP/DHCP packet parse + build for the proxy-DHCP daemon.

Wire-level only. No sockets, no asyncio, no business logic --
everything in here is a pure function over ``bytes``. Lets us
exercise the protocol surface in unit tests with constructed
packets (or pcap-captured ones) without touching the network.

References:
  * RFC 951 -- BOOTP message format (fixed header).
  * RFC 2131 -- DHCP message format (BOOTP + magic cookie + options).
  * RFC 2132 -- DHCP options (option type + length + value).
  * RFC 4578 -- PXE / option 93 (client system architecture).

Out of scope here:
  * Unaligned or relay-agent option-82 extensions (we never relay).
  * IPv6 / DHCPv6 (bty's PXE world is IPv4 only).
  * Lease management, ARP, ICMP -- proxy mode has none of these.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

# RFC 951 BOOTP fixed-header layout (240 bytes including the 4-byte
# DHCP magic cookie at the start of the options area). The struct
# format string is in the order the bytes appear on the wire.
#
#   op (1)    -- 1=BOOTREQUEST, 2=BOOTREPLY
#   htype (1) -- hardware addr type; 1 = Ethernet
#   hlen (1)  -- hardware addr length; 6 for Ethernet
#   hops (1)  -- relay hop count
#   xid (4)   -- transaction ID, picked by client
#   secs (2)  -- seconds since client started trying
#   flags (2) -- bit 0 = broadcast-reply (0x8000 in big-endian)
#   ciaddr (4) -- client IP (when already has one); we always send 0
#   yiaddr (4) -- "your" IP; 0 in proxy mode (we don't allocate)
#   siaddr (4) -- "next server"; we set this to our IP for TFTP
#   giaddr (4) -- relay agent IP; 0 in our scope
#   chaddr (16) -- client hardware address (padded to 16)
#   sname (64)  -- "server name"; usually empty in PXE proxy
#   file (128)  -- bootfile name; we put the PXE bootfile here too
#   magic (4)  -- DHCP cookie 0x63 82 53 63
# Cached Struct instance so the format string is compiled once at
# import time, not re-parsed per call. The format is otherwise
# identical to what would be passed to struct.pack/unpack.
_BOOTP = struct.Struct("!BBBB I HH 4s4s4s4s 16s 64s 128s 4s")
_BOOTP_LEN = _BOOTP.size
assert _BOOTP_LEN == 240, "BOOTP fixed header size drift"

DHCP_MAGIC = b"\x63\x82\x53\x63"


# RFC 2132 option codes we care about. Names mirror dnsmasq's
# option:foo aliases so cross-references stay obvious.
class Opt(IntEnum):
    SUBNET_MASK = 1
    HOSTNAME = 12
    REQ_IP = 50
    LEASE_TIME = 51
    MSG_TYPE = 53
    SERVER_ID = 54
    PARAM_REQ_LIST = 55
    MAX_MSG_SIZE = 57
    VENDOR_CLASS = 60
    CLIENT_ID = 61
    VENDOR_SPECIFIC = 43  # PXE sub-options live inside this option
    TFTP_SERVER_NAME = 66
    BOOTFILE_NAME = 67
    USER_CLASS = 77
    CLIENT_ARCH = 93
    CLIENT_NETWORK_DEVICE_IF = 94
    CLIENT_MACHINE_ID = 97  # PXE / UEFI GUID
    END = 255


class MsgType(IntEnum):
    DISCOVER = 1
    OFFER = 2
    REQUEST = 3
    DECLINE = 4
    ACK = 5
    NAK = 6
    RELEASE = 7
    INFORM = 8


class Op(IntEnum):
    BOOTREQUEST = 1
    BOOTREPLY = 2


# BOOTP flags bit-0 (high-order bit in network-order field) = broadcast.
FLAG_BROADCAST = 0x8000


@dataclass(frozen=True)
class Packet:
    """Parsed BOOTP/DHCP packet.

    Fixed-header fields land in dedicated attributes; the DHCP
    options TLV stream is decoded to a ``dict[int, bytes]``. Option
    255 (end) is consumed but not stored. Option 0 (pad) is skipped.
    """

    op: int
    htype: int
    hlen: int
    hops: int
    xid: int
    secs: int
    flags: int
    ciaddr: bytes  # 4 bytes
    yiaddr: bytes  # 4 bytes
    siaddr: bytes  # 4 bytes
    giaddr: bytes  # 4 bytes
    chaddr: bytes  # 16 bytes (first 6 are the MAC for htype=1)
    sname: bytes  # 64 bytes
    file: bytes  # 128 bytes
    options: dict[int, bytes] = field(default_factory=dict)

    @property
    def mac(self) -> bytes:
        """Client MAC, sliced from chaddr by hlen. Always 6 bytes
        for Ethernet (htype=1)."""
        return self.chaddr[: self.hlen]

    @property
    def mac_pretty(self) -> str:
        """Lowercase colon-separated MAC (``aa:bb:cc:dd:ee:ff``).
        Used by event payloads + log lines so callers don't repeat
        the join expression."""
        return ":".join(f"{b:02x}" for b in self.mac)

    @property
    def msg_type(self) -> int | None:
        """DHCP message type (option 53) as int, or ``None`` if
        the packet is plain BOOTP with no DHCP options."""
        raw = self.options.get(Opt.MSG_TYPE)
        if raw is None or len(raw) != 1:
            return None
        return raw[0]

    @property
    def vendor_class(self) -> bytes | None:
        """Option 60 raw bytes. PXEClient discovers are ASCII like
        ``b"PXEClient:Arch:00007:UNDI:003016"`` but we keep bytes
        to dodge encoding pitfalls."""
        return self.options.get(Opt.VENDOR_CLASS)

    @property
    def user_class(self) -> bytes | None:
        """Option 77. iPXE writes ``b"iPXE"`` here on its second-
        stage DHCP after the binary loaded."""
        return self.options.get(Opt.USER_CLASS)

    @property
    def client_arch(self) -> int | None:
        """Option 93 (RFC 4578) as a 16-bit big-endian arch ID.
        ``None`` if the option isn't present.

        Common values: 0 = BIOS x86, 6 = UEFI IA32, 7 = UEFI BC
        (byte code, common), 9 = UEFI x86_64, 11 = UEFI ARM64,
        16 = HTTP-Boot UEFI x86_64.
        """
        raw = self.options.get(Opt.CLIENT_ARCH)
        if raw is None or len(raw) != 2:
            return None
        return int.from_bytes(raw, "big")

    @property
    def bootfile_name(self) -> bytes | None:
        """Option 67. The bootfile in our PXE offers, or ``None``
        on a packet that's missing it (every DISCOVER, since clients
        ask for the bootfile -- they don't send one)."""
        return self.options.get(Opt.BOOTFILE_NAME)

    @property
    def server_id(self) -> bytes | None:
        """Option 54 (server-identifier) -- the 4-byte packed IP of
        the DHCP server emitting this packet. ``None`` on a packet
        without it (i.e. a discover, which has no server yet)."""
        return self.options.get(Opt.SERVER_ID)


def parse(data: bytes) -> Packet:
    """Decode a UDP payload into a :class:`Packet`.

    Raises :class:`ValueError` on packets too short to be valid
    BOOTP (< 240 bytes), missing the DHCP magic cookie, or with
    a truncated option TLV. We tolerate trailing bytes after the
    end-option marker (some clients pad).
    """
    if len(data) < _BOOTP_LEN:
        raise ValueError(f"packet too short for BOOTP: {len(data)} < {_BOOTP_LEN}")
    fields = _BOOTP.unpack_from(data)
    (
        op,
        htype,
        hlen,
        hops,
        xid,
        secs,
        flags,
        ciaddr,
        yiaddr,
        siaddr,
        giaddr,
        chaddr,
        sname,
        file_,
        magic,
    ) = fields
    if magic != DHCP_MAGIC:
        raise ValueError(f"missing DHCP magic cookie: got {magic!r}")
    options = _parse_options(data[_BOOTP_LEN:])
    return Packet(
        op=op,
        htype=htype,
        hlen=hlen,
        hops=hops,
        xid=xid,
        secs=secs,
        flags=flags,
        ciaddr=ciaddr,
        yiaddr=yiaddr,
        siaddr=siaddr,
        giaddr=giaddr,
        chaddr=chaddr,
        sname=sname,
        file=file_,
        options=options,
    )


def _parse_options(blob: bytes) -> dict[int, bytes]:
    """Walk the DHCP options TLV stream. Stops at option 255 (END).

    Option 0 (PAD) is a single byte; all other options carry a
    1-byte length and ``length`` value bytes. Repeated option codes
    overwrite earlier values -- proxy-DHCP never uses repeats so
    that simplification is safe.
    """
    out: dict[int, bytes] = {}
    i = 0
    n = len(blob)
    while i < n:
        code = blob[i]
        i += 1
        if code == Opt.END:
            break
        if code == 0:  # PAD
            continue
        if i >= n:
            raise ValueError(f"truncated option {code}: missing length")
        length = blob[i]
        i += 1
        if i + length > n:
            raise ValueError(f"truncated option {code}: claimed {length} bytes, have {n - i}")
        out[code] = blob[i : i + length]
        i += length
    return out


def build(
    *,
    op: int,
    xid: int,
    secs: int = 0,
    flags: int = FLAG_BROADCAST,
    ciaddr: bytes = b"\x00\x00\x00\x00",
    yiaddr: bytes = b"\x00\x00\x00\x00",
    siaddr: bytes = b"\x00\x00\x00\x00",
    giaddr: bytes = b"\x00\x00\x00\x00",
    chaddr: bytes,
    sname: bytes = b"",
    file: bytes = b"",
    options: dict[int, bytes],
    htype: int = 1,
    hlen: int = 6,
    hops: int = 0,
) -> bytes:
    """Build a BOOTP/DHCP packet from its parts.

    ``chaddr`` / ``sname`` / ``file`` are zero-padded to their
    fixed widths. ``options`` is serialized in the order given by
    the dict's iteration (Python 3.7+ preserves insertion order);
    an END marker (255) is appended automatically. The packet is
    NOT padded out to any minimum DHCP size -- callers that care
    (some PXE ROMs are strict about <300-byte offers) can pad
    afterwards.
    """
    if len(ciaddr) != 4 or len(yiaddr) != 4 or len(siaddr) != 4 or len(giaddr) != 4:
        raise ValueError("each IP field must be exactly 4 bytes")
    chaddr_pad = chaddr.ljust(16, b"\x00")[:16]
    sname_pad = sname.ljust(64, b"\x00")[:64]
    file_pad = file.ljust(128, b"\x00")[:128]
    header = _BOOTP.pack(
        op,
        htype,
        hlen,
        hops,
        xid,
        secs,
        flags,
        ciaddr,
        yiaddr,
        siaddr,
        giaddr,
        chaddr_pad,
        sname_pad,
        file_pad,
        DHCP_MAGIC,
    )
    opts_blob = _build_options(options)
    return header + opts_blob


def _build_options(options: dict[int, bytes]) -> bytes:
    """Serialize the DHCP options dict as ``code,len,value...`` TLVs
    followed by the END marker. Values longer than 255 bytes are
    rejected -- proxy-DHCP doesn't need long-option encoding."""
    chunks: list[bytes] = []
    for code, value in options.items():
        if code in (0, Opt.END):
            raise ValueError(f"reserved option code {code} cannot be set directly")
        if len(value) > 255:
            raise ValueError(f"option {code} value too long: {len(value)} > 255 bytes")
        chunks.append(bytes((code, len(value))) + value)
    chunks.append(bytes((Opt.END,)))
    return b"".join(chunks)


def pad_to_min(packet: bytes, minimum: int = 300) -> bytes:
    """Some PXE ROMs reject offers smaller than 300 bytes; pad with
    zeros up to ``minimum``. No-op when the packet is already long
    enough."""
    if len(packet) >= minimum:
        return packet
    return packet + b"\x00" * (minimum - len(packet))


# Vendor-class prefixes we recognise as PXE-like discovers.
# ``PXEClient`` is the RFC 4578 mainline; ``HTTPClient`` is UEFI
# HTTP Boot. ``gPXE`` and ``iPXE`` are alternative loaders that
# sometimes tag option 60 instead of (or in addition to) option 77
# -- accepting them lets us serve those clients without conflating
# detection paths. Anything outside this set is regular DHCP we
# stay out of.
_PXE_VENDOR_CLASS_PREFIXES: tuple[bytes, ...] = (
    b"PXEClient",
    b"HTTPClient",
    b"gPXE",
    b"iPXE",
)


def is_pxe_client_discover(p: Packet) -> bool:
    """True when ``p`` is a DHCPDISCOVER from a PXE/HTTP-Boot client.

    Filter rule for the proxy: only respond to discovers whose
    vendor-class option starts with a known PXE-family prefix.
    Anything else is regular DHCP traffic the operator's main
    server owns -- we stay out of it.
    """
    if p.op != Op.BOOTREQUEST or p.msg_type != MsgType.DISCOVER:
        return False
    vc = p.vendor_class or b""
    return vc.startswith(_PXE_VENDOR_CLASS_PREFIXES)
