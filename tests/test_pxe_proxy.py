"""Tests for the bty PXE proxy-DHCP daemon.

Two layers, mirroring src/bty/pxe/:

1. Wire codec (:mod:`bty.pxe.wire`) -- parse + build BOOTP/DHCP
   packets. Constructed inputs / pcap-shaped inputs; no socket.
2. Proxy logic (:mod:`bty.pxe.proxy.build_offer`) -- given a
   discover Packet, decide the right bootfile + emit a complete
   offer. Pure function; called directly.

The asyncio listener loop itself (open UDP 67, bind, etc.) isn't
exercised here -- that needs root + a real NIC. End-to-end is
hardware-side verification.
"""

from __future__ import annotations

import socket
import struct

import pytest

from bty.pxe import wire
from bty.pxe.proxy import (
    IGNORE_HTTP_DISABLED,
    IGNORE_UNKNOWN_ARCH,
    BootDecision,
    ProxyConfig,
    _parse_args,
    _resolve_bootfile,
    build_offer,
)
from bty.pxe.wire import FLAG_BROADCAST, MsgType, Op, Opt, Packet

# --------------------------------------------------------------------------
# Helpers: build a realistic PXE DISCOVER like the ones we saw in the
# hardware-debug pcaps (target 84:47:09:77:8a:18, arch 7).
# --------------------------------------------------------------------------


def _make_pxe_discover(
    *,
    mac: bytes = b"\x84\x47\x09\x77\x8a\x18",
    xid: int = 0x12345678,
    arch: int = 7,
    vendor_class: bytes = b"PXEClient:Arch:00007:UNDI:003016",
    guid: bytes = bytes(17),
    user_class: bytes | None = None,
) -> bytes:
    """Build a BOOTP/DHCP discover with the option set a modern
    UEFI PXE ROM emits. Matches the structural shape we observed
    in pcaps."""
    options: dict[int, bytes] = {
        Opt.MSG_TYPE: bytes((MsgType.DISCOVER,)),
        Opt.MAX_MSG_SIZE: struct.pack("!H", 1472),
        Opt.PARAM_REQ_LIST: bytes((1, 3, 6, 12, 15, 43, 54, 60, 66, 67, 97)),
        Opt.VENDOR_CLASS: vendor_class,
        Opt.CLIENT_MACHINE_ID: guid,
        Opt.CLIENT_ARCH: struct.pack("!H", arch),
    }
    if user_class is not None:
        options[Opt.USER_CLASS] = user_class
    return wire.build(
        op=Op.BOOTREQUEST,
        xid=xid,
        flags=FLAG_BROADCAST,
        chaddr=mac,
        options=options,
    )


# --------------------------------------------------------------------------
# Wire codec: parse + build round-trip.
# --------------------------------------------------------------------------


def test_parse_round_trips_a_built_packet() -> None:
    """Build a packet, parse it back, assert structural equality on
    every field that matters. Catches the "I wrote the struct format
    wrong" class of regression."""
    pkt = _make_pxe_discover()
    parsed = wire.parse(pkt)
    assert parsed.op == Op.BOOTREQUEST
    assert parsed.htype == 1
    assert parsed.hlen == 6
    assert parsed.xid == 0x12345678
    assert parsed.flags == FLAG_BROADCAST
    assert parsed.mac == b"\x84\x47\x09\x77\x8a\x18"
    assert parsed.msg_type == MsgType.DISCOVER
    assert parsed.vendor_class == b"PXEClient:Arch:00007:UNDI:003016"
    assert parsed.client_arch == 7


def test_parse_rejects_short_packet() -> None:
    """Anything shorter than the 240-byte BOOTP header is invalid."""
    with pytest.raises(ValueError, match="too short"):
        wire.parse(b"\x00" * 100)


def test_parse_rejects_missing_magic_cookie() -> None:
    """A 240-byte header without the DHCP magic cookie is plain
    BOOTP -- our world is DHCP-shaped, reject explicitly so we
    don't silently misinterpret legacy BOOTP."""
    bogus = b"\x00" * 240
    with pytest.raises(ValueError, match="magic cookie"):
        wire.parse(bogus)


def test_parse_rejects_truncated_option() -> None:
    """A truncated option-length blow-up shouldn't crash; raise
    ValueError so the daemon can log + drop the bad packet."""
    pkt = bytearray(_make_pxe_discover())
    # Last option's length is now overrun (claim length 200 of a
    # 5-byte tail) -- find the END marker and replace the option
    # just before it with a length-too-large value.
    end_idx = pkt.rfind(bytes((Opt.END,)))
    # Inject a code=42, length=200 with no value before the END.
    pkt[end_idx:end_idx] = b"\x2a\xc8"
    with pytest.raises(ValueError, match="truncated"):
        wire.parse(bytes(pkt))


def test_parse_ignores_pad_and_trailing_bytes() -> None:
    """PAD (option 0) is single-byte filler; trailing bytes after
    END are dropped silently. Some clients pad their packets to a
    fixed length."""
    inner = wire.build(
        op=Op.BOOTREQUEST,
        xid=1,
        chaddr=b"\x00" * 6,
        options={Opt.MSG_TYPE: bytes((MsgType.DISCOVER,))},
    )
    # Append PADs (option 0) before the existing END, plus garbage
    # after END.
    end_idx = inner.index(bytes((Opt.END,)))
    padded = inner[:end_idx] + b"\x00\x00\x00" + inner[end_idx:] + b"\xaa\xbb\xcc"
    parsed = wire.parse(padded)
    assert parsed.msg_type == MsgType.DISCOVER


def test_is_pxe_client_discover_filters_correctly() -> None:
    """Filter accepts PXEClient + HTTPClient discovers; rejects
    plain DHCP discovers (no vendor class), REQUEST messages,
    and anything else."""
    pxe = wire.parse(_make_pxe_discover())
    assert wire.is_pxe_client_discover(pxe) is True

    http = wire.parse(_make_pxe_discover(vendor_class=b"HTTPClient:Arch:00016:UNDI:003016"))
    assert wire.is_pxe_client_discover(http) is True

    plain = wire.build(
        op=Op.BOOTREQUEST,
        xid=1,
        chaddr=b"\x00" * 6,
        options={Opt.MSG_TYPE: bytes((MsgType.DISCOVER,))},
    )
    assert wire.is_pxe_client_discover(wire.parse(plain)) is False

    request = wire.parse(
        wire.build(
            op=Op.BOOTREQUEST,
            xid=1,
            chaddr=b"\x00" * 6,
            options={
                Opt.MSG_TYPE: bytes((MsgType.REQUEST,)),
                Opt.VENDOR_CLASS: b"PXEClient:Arch:00007",
            },
        )
    )
    assert wire.is_pxe_client_discover(request) is False


# --------------------------------------------------------------------------
# Proxy logic: build_offer for each arch + client class.
# --------------------------------------------------------------------------


def _cfg() -> ProxyConfig:
    return ProxyConfig(interface="enp90s0", server_ip="192.168.1.31")


def _decode_offer(blob: bytes) -> Packet:
    return wire.parse(blob)


def test_build_offer_pxeclient_arch7_stamps_ipxe_efi() -> None:
    """The hardware-debug case: UEFI client arch 7 sends a
    PXEClient discover; we must offer ipxe.efi via TFTP. Asserts
    that BOTH the BOOTP file[] field AND option 67 carry the
    bootfile -- modern UEFI reads one, legacy clients read the
    other, our daemon sets both."""
    discover = wire.parse(_make_pxe_discover(arch=7))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    assert parsed.op == Op.BOOTREPLY
    assert parsed.msg_type == MsgType.OFFER
    assert parsed.xid == discover.xid
    # BOOTP file[] field stamped (truncated at 128 bytes; short
    # name fits unchanged).
    assert parsed.file.rstrip(b"\x00") == b"ipxe.efi"
    # option 67 stamped.
    assert parsed.bootfile_name == b"ipxe.efi"
    # siaddr / option 54 / option 66 = our IP.
    expected_ip = socket.inet_aton("192.168.1.31")
    assert parsed.siaddr == expected_ip
    assert parsed.server_id == expected_ip
    assert parsed.options[Opt.TFTP_SERVER_NAME] == b"192.168.1.31"
    # Vendor class echoed back as PXEClient (firmware rejects offers
    # with the wrong tag).
    assert parsed.options[Opt.VENDOR_CLASS] == b"PXEClient"


def test_build_offer_pxeclient_arch0_stamps_undionly_kpxe() -> None:
    """Legacy BIOS arch 0 gets undionly.kpxe, not ipxe.efi."""
    discover = wire.parse(_make_pxe_discover(arch=0))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    assert parsed.bootfile_name == b"undionly.kpxe"


def test_build_offer_httpclient_arch16_stamps_http_url() -> None:
    """UEFI HTTP-Boot client (the other target we saw, MAC
    e0:51:d8:1e:43:15, arch 16). Bootfile must be an absolute URL
    and option 60 MUST echo HTTPClient -- firmware rejects
    PXEClient-tagged offers when it asked for HTTP."""
    discover = wire.parse(
        _make_pxe_discover(arch=16, vendor_class=b"HTTPClient:Arch:00016:UNDI:003016")
    )
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    assert parsed.bootfile_name == b"http://192.168.1.31:8080/boot/ipxe.efi"
    assert parsed.options[Opt.VENDOR_CLASS] == b"HTTPClient"


def test_build_offer_tftp_only_refuses_httpclient() -> None:
    """``tftp_only=True`` makes the daemon decline HTTPClient
    discovers rather than offer a URL that might 404. Useful while
    the appliance hasn't staged /boot/ipxe.efi yet."""
    cfg = ProxyConfig(interface="enp90s0", server_ip="192.168.1.31", tftp_only=True)
    discover = wire.parse(_make_pxe_discover(arch=16, vendor_class=b"HTTPClient:Arch:00016"))
    assert build_offer(cfg, discover) is None


def test_build_offer_ipxe_userclass_chains_to_bty_web_bootstrap() -> None:
    """Once iPXE itself has loaded and re-DHCPs, it sets
    user-class=iPXE. We chain it to bty-web's pxe-bootstrap script
    over HTTP instead of looping with another ipxe.efi TFTP fetch."""
    discover = wire.parse(_make_pxe_discover(user_class=b"iPXE"))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    bootfile = parsed.bootfile_name
    assert bootfile == b"http://192.168.1.31:8080/pxe-bootstrap.ipxe"
    # iPXE itself sends PXEClient on its second-stage DHCP, so the
    # response also tags as PXEClient.
    assert parsed.options[Opt.VENDOR_CLASS] == b"PXEClient"


def test_build_offer_drops_non_pxe_clients() -> None:
    """A regular DHCP discover (no vendor class) is not ours;
    return None so the daemon stays silent."""
    plain = wire.build(
        op=Op.BOOTREQUEST,
        xid=1,
        chaddr=b"\x00\x01\x02\x03\x04\x05",
        options={Opt.MSG_TYPE: bytes((MsgType.DISCOVER,))},
    )
    assert build_offer(_cfg(), wire.parse(plain)) is None


def test_build_offer_drops_unknown_pxe_arch() -> None:
    """An arch value we have no mapping for shouldn't produce a
    bogus offer -- some clients send vendor-specific arch IDs we
    can't honour. Returning None lets the operator's main DHCP
    (and any other PXE servers) win that discovery."""
    discover = wire.parse(_make_pxe_discover(arch=42, vendor_class=b"PXEClient:Arch:00042"))
    assert build_offer(_cfg(), discover) is None


def test_build_offer_pads_to_300_bytes_min() -> None:
    """Some PXE ROMs drop DHCPOFFERs shorter than 300 bytes. The
    daemon pads up to that minimum."""
    discover = wire.parse(_make_pxe_discover())
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    assert len(offer) >= 300


def test_build_offer_populates_bootp_sname_field() -> None:
    """BOOTP ``sname`` (the 64-byte server-name field) gets our IP
    as ASCII. Old PXE 2.x firmware reads sname as "boot server
    hostname" and falls back when it's empty -- populating it is
    pure leniency toward legacy clients; modern UEFI ignores it.
    """
    discover = wire.parse(_make_pxe_discover())
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = wire.parse(offer)
    assert parsed.sname.rstrip(b"\x00") == b"192.168.1.31"


def test_build_offer_emits_option_43_pxe_discovery_control() -> None:
    """Option 43 (vendor-specific) carries the PXE_DISCOVERY_CONTROL
    sub-option (sub-code 6, value 0x0C) which tells old PXE 2.x
    ROMs "skip multicast / broadcast discovery; use the bootfile-
    name directly". Modern UEFI ignores option 43; no downside.
    Pin so a future "trim the options" attempt doesn't silently
    re-introduce the legacy-firmware compat gap."""
    discover = wire.parse(_make_pxe_discover())
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = wire.parse(offer)
    body = parsed.options.get(43)  # raw 43 not in our IntEnum
    assert body is not None, "option 43 absent from offer"
    # Sub-option-6-len-1-value-0x0C-end-0xff.
    assert body == bytes((6, 1, 0x0C, 0xFF))


def test_build_offer_accepts_gpxe_vendor_class() -> None:
    """``gPXE`` (iPXE's predecessor) and ``iPXE`` (when tagged in
    option 60 instead of option 77) are also legitimate PXE-family
    discovers. We accept them with the standard PXE arch mapping."""
    discover = wire.parse(_make_pxe_discover(vendor_class=b"gPXE:1.0.1"))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    # Same shape as a regular PXEClient offer.
    parsed = wire.parse(offer)
    assert parsed.bootfile_name == b"ipxe.efi"


def test_build_offer_arch_6_uefi_ia32_uses_arch_specific_binary() -> None:
    """A UEFI IA32 client (arch 6) must get ``ipxe-i386.efi``,
    NOT x86_64 ``ipxe.efi`` -- sending the wrong arch's binary
    would let the offer flow succeed only to crash on execution.
    The file may not be on disk; TFTP responds FILE_NOT_FOUND
    in that case, which is preferable to serving a wrong-arch
    binary."""
    discover = wire.parse(_make_pxe_discover(arch=6))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = wire.parse(offer)
    assert parsed.bootfile_name == b"ipxe-i386.efi"


def test_build_offer_arch_10_uefi_arm32_uses_arm32_binary() -> None:
    """Arch 10 is the RFC 4578 code for UEFI ARM 32-bit firmware.
    The proxy doesn't ship the binary -- operators stage it under
    /var/lib/tftpboot if they target ARM 32 -- but the offer must
    point at ipxe-arm32.efi so TFTP can serve it (or cleanly answer
    FILE_NOT_FOUND when absent, letting the operator's main DHCP
    win). Sibling to arch_6 + arch_11 coverage; uncovered until now."""
    discover = wire.parse(
        _make_pxe_discover(arch=10, vendor_class=b"PXEClient:Arch:00010:UNDI:003016")
    )
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    assert parsed.bootfile_name == b"ipxe-arm32.efi"
    assert parsed.options[Opt.VENDOR_CLASS] == b"PXEClient"


def test_build_offer_arch_11_uefi_arm64_uses_arm64_binary() -> None:
    """ARM64 UEFI clients (RPi 4+ in UEFI mode, RockPi etc.) get
    ``ipxe-arm64.efi``. Pin the mapping so a future arch-table
    edit doesn't accidentally regress ARM64 boot support."""
    discover = wire.parse(_make_pxe_discover(arch=11))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = wire.parse(offer)
    assert parsed.bootfile_name == b"ipxe-arm64.efi"


def test_build_offer_echoes_client_machine_id_when_present() -> None:
    """Option 97 (client-machine-id / GUID) is echoed back when the
    client sent one -- some firmware uses it as a sanity check
    against the OFFER."""
    guid = bytes(range(17))  # 17-byte GUID payload
    discover = wire.parse(_make_pxe_discover(guid=guid))
    offer = build_offer(_cfg(), discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    assert parsed.options[Opt.CLIENT_MACHINE_ID] == guid


def test_build_offer_long_http_bootfile_lands_in_option_67_not_file_field() -> None:
    """When the bootfile is an http:// URL longer than 128 bytes,
    BOOTP's fixed 128-byte file[] field can't hold it. Option 67
    is the load-bearing field for modern UEFI; assert it carries
    the full URL even when file[] gets truncated."""
    cfg = ProxyConfig(
        interface="enp90s0",
        server_ip="10.10.10.10",
        http_port=80,
    )
    # Use HTTPClient + arch 16; bootfile becomes the HTTP URL.
    long_vc = b"HTTPClient:Arch:00016"
    discover = wire.parse(_make_pxe_discover(arch=16, vendor_class=long_vc))
    offer = build_offer(cfg, discover)
    assert offer is not None
    parsed = _decode_offer(offer)
    # Option 67 always carries the full URL.
    assert parsed.bootfile_name.startswith(b"http://10.10.10.10")


# --------------------------------------------------------------------------
# Packet.mac_pretty helper.
# --------------------------------------------------------------------------


def test_packet_mac_pretty_formats_as_colon_separated_hex() -> None:
    discover = wire.parse(_make_pxe_discover())
    assert discover.mac_pretty == "84:47:09:77:8a:18"


# --------------------------------------------------------------------------
# Tagged ignore reasons returned by _resolve_bootfile.
# --------------------------------------------------------------------------


def test_resolve_bootfile_returns_unknown_arch_reason() -> None:
    """PXEClient with an arch we have no mapping for should return
    the IGNORE_UNKNOWN_ARCH sentinel so the daemon can emit a
    specific ignore reason in the event feed."""
    cfg = ProxyConfig(interface="enp90s0", server_ip="192.168.1.31")
    result = _resolve_bootfile(cfg, vendor_class=b"PXEClient", arch=42, is_ipxe=False)
    assert result == IGNORE_UNKNOWN_ARCH


def test_resolve_bootfile_returns_http_disabled_reason_when_tftp_only() -> None:
    """HTTPClient + tftp_only=True should yield IGNORE_HTTP_DISABLED
    so operators can distinguish "blocked by config" from "unknown
    arch" in the diagnostic feed."""
    cfg = ProxyConfig(interface="enp90s0", server_ip="192.168.1.31", tftp_only=True)
    result = _resolve_bootfile(cfg, vendor_class=b"HTTPClient:Arch:00016", arch=16, is_ipxe=False)
    assert result == IGNORE_HTTP_DISABLED


def test_resolve_bootfile_known_arch_returns_decision() -> None:
    cfg = ProxyConfig(interface="enp90s0", server_ip="192.168.1.31")
    result = _resolve_bootfile(cfg, vendor_class=b"PXEClient", arch=7, is_ipxe=False)
    assert isinstance(result, BootDecision)
    assert result.bootfile == b"ipxe.efi"


# --------------------------------------------------------------------------
# --interface name validation.
# --------------------------------------------------------------------------


def test_parse_args_rejects_invalid_interface_name() -> None:
    """The activate helper validates the interface name; the daemon
    re-validates so a manual / cron / systemd invocation with a
    bogus name also fails fast with a clear error."""
    with pytest.raises(SystemExit):
        _parse_args(["--interface", "eth0; rm -rf /", "--server-ip", "192.168.1.31"])


def test_parse_args_rejects_too_long_interface_name() -> None:
    """Linux IFNAMSIZ-1 is 15. SO_BINDTODEVICE would reject anything
    longer; we reject at argparse time for a clearer error."""
    with pytest.raises(SystemExit):
        _parse_args(["--interface", "a" * 16, "--server-ip", "192.168.1.31"])


def test_parse_args_accepts_standard_interface_names() -> None:
    cfg, _verbose = _parse_args(["--interface", "enp90s0", "--server-ip", "192.168.1.31"])
    assert cfg.interface == "enp90s0"


# --------------------------------------------------------------------------
# _DhcpServerProtocol.datagram_received exception path.
# --------------------------------------------------------------------------


def test_datagram_received_emits_dhcp_error_when_build_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bug in offer-building must not take the daemon down; the
    handler emits ``dhcp.error`` + drops the packet. Patch
    ``_build_offer_packet`` to force the failure path."""
    from unittest.mock import patch

    from bty.pxe.proxy import _DhcpServerProtocol

    cfg = ProxyConfig(interface="enp90s0", server_ip="192.168.1.31")
    proto = _DhcpServerProtocol(cfg)
    # transport never gets used on the error path, but the assert in
    # datagram_received needs it set; supply a stub.
    proto._transport = object()  # type: ignore[assignment]
    discover_bytes = _make_pxe_discover()
    with patch(
        "bty.pxe.proxy._build_offer_packet",
        side_effect=RuntimeError("boom in offer assembly"),
    ):
        proto.datagram_received(discover_bytes, ("0.0.0.0", 68))
    stdout = capsys.readouterr().out
    assert '"evt":"dhcp.error"' in stdout
    assert '"error":"boom in offer assembly"' in stdout
