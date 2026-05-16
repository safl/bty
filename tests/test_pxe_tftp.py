"""Tests for the bty TFTP server.

Two layers, mirroring src/bty/pxe/tftp.py:

1. Wire codec (:func:`parse`, :func:`build_data`, :func:`build_oack`,
   :func:`build_error`) -- pure functions, no socket.
2. End-to-end transfer over loopback -- one full RRQ -> DATA -> ACK
   ping-pong driven by a real asyncio server + a hand-rolled
   ``asyncio.DatagramProtocol`` test client.

The end-to-end layer doesn't bind UDP 69 (that needs root); it
invokes the lower-level helpers (_Transfer + ephemeral sockets)
directly.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path

import pytest

from bty.pxe.tftp import (
    DEFAULT_BLKSIZE,
    Ack,
    ErrorCode,
    Opcode,
    Rrq,
    TftpConfig,
    TftpError,
    _ListenerProtocol,
    _negotiate_options,
    _resolve_safe_path,
    build_data,
    build_error,
    build_oack,
    parse,
)

# --------------------------------------------------------------------------
# Wire codec
# --------------------------------------------------------------------------


def _build_rrq(filename: str, mode: str = "octet", options: dict[str, str] | None = None) -> bytes:
    body = filename.encode("ascii") + b"\x00" + mode.encode("ascii") + b"\x00"
    if options:
        for k, v in options.items():
            body += k.encode("ascii") + b"\x00" + v.encode("ascii") + b"\x00"
    return struct.pack("!H", Opcode.RRQ) + body


def test_parse_rrq_octet_no_options() -> None:
    pkt = _build_rrq("ipxe.efi")
    msg = parse(pkt)
    assert isinstance(msg, Rrq)
    assert msg.filename == "ipxe.efi"
    assert msg.mode == "octet"
    assert msg.options == {}


def test_parse_rrq_with_blksize_tsize_options() -> None:
    """RFC 2347 option-extension RRQ: blksize + tsize. Names + values
    are case-insensitive on the wire; we lowercase the option name."""
    pkt = _build_rrq("ipxe.efi", options={"BLKSIZE": "1432", "tsize": "0"})
    msg = parse(pkt)
    assert isinstance(msg, Rrq)
    assert msg.options == {"blksize": "1432", "tsize": "0"}


def test_parse_rrq_rejects_truncated() -> None:
    """An RRQ with no nul-terminator on the mode field is truncated
    -- we drop it rather than guess."""
    pkt = struct.pack("!H", Opcode.RRQ) + b"ipxe.efi\x00octet"  # missing trailing \0
    with pytest.raises(TftpError):
        parse(pkt)


def test_parse_wrq_is_rejected_explicitly() -> None:
    """We're a read-only server; rejecting WRQ with an exception
    lets the server fall through to an ERROR reply path."""
    pkt = struct.pack("!H", Opcode.WRQ) + b"upload.bin\x00octet\x00"
    with pytest.raises(TftpError, match="WRQ not supported"):
        parse(pkt)


def test_parse_ack_returns_ack_dataclass() -> None:
    pkt = struct.pack("!HH", Opcode.ACK, 42)
    msg = parse(pkt)
    assert msg == Ack(block=42)


def test_parse_ack_with_wrong_body_length_raises() -> None:
    pkt = struct.pack("!H", Opcode.ACK) + b"\x00"  # 1 byte instead of 2
    with pytest.raises(TftpError, match="ACK body"):
        parse(pkt)


def test_parse_ignores_server_only_opcodes() -> None:
    """DATA / OACK / ERROR arriving on the listener socket from
    a client are not part of the legitimate inbound surface --
    parse returns None so the dispatcher silently drops them."""
    assert parse(struct.pack("!HH", Opcode.DATA, 1) + b"payload") is None
    assert parse(struct.pack("!H", Opcode.OACK) + b"blksize\x001432\x00") is None
    assert parse(build_error(ErrorCode.FILE_NOT_FOUND, "nope")) is None


def test_parse_rejects_unknown_opcode() -> None:
    pkt = struct.pack("!H", 999) + b"junk"
    with pytest.raises(TftpError, match="unknown opcode"):
        parse(pkt)


def test_parse_rejects_too_short() -> None:
    with pytest.raises(TftpError, match="too short"):
        parse(b"\x00")


def test_build_data_format() -> None:
    pkt = build_data(7, b"hello")
    assert pkt == struct.pack("!HH", Opcode.DATA, 7) + b"hello"


def test_build_data_block_zero_is_legal() -> None:
    """Block 0 isn't used for DATA but the codec accepts it; the
    spec values used for OACK-ack are block 0."""
    pkt = build_data(0, b"")
    assert pkt == struct.pack("!HH", Opcode.DATA, 0)


def test_build_data_rejects_out_of_range_block() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_data(-1, b"x")
    with pytest.raises(ValueError, match="out of range"):
        build_data(0x10000, b"x")


def test_build_oack_serializes_pairs_in_order() -> None:
    pkt = build_oack({"blksize": "1432", "tsize": "123456"})
    expected = struct.pack("!H", Opcode.OACK) + b"blksize\x001432\x00" + b"tsize\x00123456\x00"
    assert pkt == expected


def test_build_error_serializes_code_and_message() -> None:
    pkt = build_error(ErrorCode.ACCESS_VIOLATION, "no")
    assert pkt == struct.pack("!HH", Opcode.ERROR, ErrorCode.ACCESS_VIOLATION) + b"no\x00"


# --------------------------------------------------------------------------
# OACK negotiation
# --------------------------------------------------------------------------


def test_negotiate_clamps_blksize_high() -> None:
    """Client asks for the maximum-allowed-by-spec 65464; we cap
    well below MTU to dodge fragmentation."""
    accepted = _negotiate_options({"blksize": "65464"}, file_size=1234)
    assert int(accepted["blksize"]) <= 1500


def test_negotiate_clamps_blksize_low() -> None:
    """Client asks for absurdly small blksize; floor at the
    RFC 2348 minimum of 8."""
    accepted = _negotiate_options({"blksize": "1"}, file_size=1234)
    assert int(accepted["blksize"]) >= 8


def test_negotiate_tsize_zero_returns_filesize() -> None:
    """Per RFC 2349: client sends tsize=0 as a query; we echo back
    the actual file size."""
    accepted = _negotiate_options({"tsize": "0"}, file_size=12345)
    assert accepted["tsize"] == "12345"


def test_negotiate_drops_unknown_options() -> None:
    """Options the server doesn't speak (windowsize etc.) get
    silently dropped; client falls back to default behaviour."""
    accepted = _negotiate_options({"windowsize": "16", "blksize": "1024"}, file_size=100)
    assert "windowsize" not in accepted
    assert "blksize" in accepted


def test_negotiate_drops_garbage_blksize() -> None:
    """A non-numeric blksize doesn't crash; we treat it as default."""
    accepted = _negotiate_options({"blksize": "lol"}, file_size=100)
    assert int(accepted["blksize"]) == DEFAULT_BLKSIZE


def test_negotiate_drops_out_of_range_timeout() -> None:
    """RFC 2349: timeout option must be 1..255. Out-of-range is
    silently ignored."""
    accepted = _negotiate_options({"timeout": "0"}, file_size=10)
    assert "timeout" not in accepted
    accepted = _negotiate_options({"timeout": "999"}, file_size=10)
    assert "timeout" not in accepted


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------


def test_resolve_safe_path_accepts_file_in_root(tmp_path: Path) -> None:
    (tmp_path / "ok.bin").write_bytes(b"data")
    assert _resolve_safe_path(tmp_path, "ok.bin") == (tmp_path / "ok.bin").resolve()


def test_resolve_safe_path_rejects_traversal(tmp_path: Path) -> None:
    """``../escape`` resolves outside the root; reject."""
    outside = tmp_path.parent / "secret.bin"
    outside.write_bytes(b"")
    try:
        assert _resolve_safe_path(tmp_path, f"../{outside.name}") is None
    finally:
        outside.unlink()


def test_resolve_safe_path_rejects_absolute(tmp_path: Path) -> None:
    """An absolute path obviously escapes the root."""
    assert _resolve_safe_path(tmp_path, "/etc/passwd") is None


def test_resolve_safe_path_rejects_nul_byte(tmp_path: Path) -> None:
    """Defensive: no embedded NUL allowed (some OS file APIs treat
    NUL as a delimiter; better to refuse cleanly)."""
    assert _resolve_safe_path(tmp_path, "ok\x00.bin") is None


def test_resolve_safe_path_rejects_missing(tmp_path: Path) -> None:
    assert _resolve_safe_path(tmp_path, "no-such-file") is None


def test_resolve_safe_path_rejects_symlink_outside(tmp_path: Path) -> None:
    """An attacker-staged symlink in the root that points outside
    the root must NOT be followed -- the realpath check catches it."""
    outside_target = tmp_path.parent / "leak.bin"
    outside_target.write_bytes(b"sekret")
    try:
        (tmp_path / "trick.bin").symlink_to(outside_target)
        assert _resolve_safe_path(tmp_path, "trick.bin") is None
    finally:
        outside_target.unlink()


# --------------------------------------------------------------------------
# End-to-end: listener + transfer over loopback (no UDP 69)
# --------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


class _TestClient(asyncio.DatagramProtocol):
    """Tiny asyncio-driven TFTP client for end-to-end tests.
    Pushes every received datagram onto a queue so the test can
    pull one packet at a time + reply appropriately."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        return None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.inbox.put_nowait((data, addr))


async def _spawn_listener(cfg: TftpConfig) -> tuple[asyncio.DatagramTransport, int]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    transport, _ = await loop.create_datagram_endpoint(lambda: _ListenerProtocol(cfg), sock=sock)
    return transport, port


async def _spawn_test_client() -> tuple[asyncio.DatagramTransport, _TestClient]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _TestClient, local_addr=("127.0.0.1", 0)
    )
    return transport, protocol


def test_listener_serves_file_to_loopback_client(tmp_path: Path) -> None:
    """Spin up a _ListenerProtocol on an ephemeral high port (not
    UDP 69 -- the test runner isn't root), send an RRQ from a
    loopback asyncio client, ACK each block, assert the reassembled
    payload matches the file on disk. Tests the full read-side
    state machine without needing root."""
    payload = b"\xaa" * (DEFAULT_BLKSIZE * 3 + 17)  # 3 full blocks + tail
    (tmp_path / "ipxe.efi").write_bytes(payload)
    cfg = TftpConfig(
        root=tmp_path,
        allowlist=frozenset({"ipxe.efi"}),
        block_timeout=2.0,
        max_retries=2,
    )

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            collected = bytearray()
            client_transport.sendto(_build_rrq("ipxe.efi"), ("127.0.0.1", listener_port))
            transfer_addr: tuple[str, int] | None = None
            expected_block = 1
            while True:
                data, addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
                opcode = struct.unpack("!H", data[:2])[0]
                assert opcode == Opcode.DATA, f"non-DATA opcode {opcode} from server"
                block = struct.unpack("!H", data[2:4])[0]
                chunk = data[4:]
                if transfer_addr is None:
                    # First DATA arrives from the server's per-
                    # transfer ephemeral port. Pin that for ACKs.
                    transfer_addr = addr
                assert block == expected_block
                collected.extend(chunk)
                # Send ACK from the client's TID back to the
                # server's per-transfer port.
                client_transport.sendto(struct.pack("!HH", Opcode.ACK, block), transfer_addr)
                if len(chunk) < DEFAULT_BLKSIZE:
                    break
                expected_block += 1
            assert bytes(collected) == payload
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())


def test_listener_rejects_file_not_in_allowlist(tmp_path: Path) -> None:
    """An RRQ for a filename outside the allowlist should yield an
    ACCESS_VIOLATION ERROR packet (not just silent drop -- the
    client needs the negative signal to fall through cleanly)."""
    (tmp_path / "secret.bin").write_bytes(b"nope")
    cfg = TftpConfig(root=tmp_path, allowlist=frozenset({"ipxe.efi"}))

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            client_transport.sendto(_build_rrq("secret.bin"), ("127.0.0.1", listener_port))
            data, _addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            opcode = struct.unpack("!H", data[:2])[0]
            assert opcode == Opcode.ERROR
            code = struct.unpack("!H", data[2:4])[0]
            assert code == ErrorCode.ACCESS_VIOLATION
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())


def test_listener_negotiates_blksize_via_oack(tmp_path: Path) -> None:
    """Client sends RRQ with blksize=1024 + tsize=0; we reply with
    OACK echoing the negotiated values, then DATA at the new
    block size. Tests that the OACK-then-DATA branch produces a
    legal sequence."""
    payload = b"\xbb" * 3000  # 2x 1024 + 952
    (tmp_path / "ipxe.efi").write_bytes(payload)
    cfg = TftpConfig(root=tmp_path, allowlist=frozenset({"ipxe.efi"}))

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            client_transport.sendto(
                _build_rrq("ipxe.efi", options={"blksize": "1024", "tsize": "0"}),
                ("127.0.0.1", listener_port),
            )
            # First packet back should be OACK.
            data, addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            opcode = struct.unpack("!H", data[:2])[0]
            assert opcode == Opcode.OACK
            body = data[2:].split(b"\x00")
            # Parse the option pairs out.
            opts = dict(zip(body[::2], body[1::2], strict=False))
            assert opts[b"blksize"] == b"1024"
            assert opts[b"tsize"] == str(len(payload)).encode("ascii")
            # ACK the OACK with block 0; server then sends DATA at the new size.
            client_transport.sendto(struct.pack("!HH", Opcode.ACK, 0), addr)
            data2, _ = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            opcode2 = struct.unpack("!H", data2[:2])[0]
            assert opcode2 == Opcode.DATA
            assert struct.unpack("!H", data2[2:4])[0] == 1
            # The DATA payload should be 1024 bytes (the negotiated blksize).
            assert len(data2[4:]) == 1024
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())


def test_listener_refuses_rrq_when_at_concurrent_cap(tmp_path: Path) -> None:
    """When ``max_concurrent_transfers`` is hit, new RRQs get a
    DISK_FULL error instead of a transfer slot. Prevents fd
    exhaustion from a flood of legitimate or hostile RRQs.

    Forces the cap to 0 (refuse everything) so we don't have to
    keep transfers alive concurrently in the test."""
    (tmp_path / "ipxe.efi").write_bytes(b"\x00")
    cfg = TftpConfig(
        root=tmp_path,
        allowlist=frozenset({"ipxe.efi"}),
        max_concurrent_transfers=0,  # refuse every RRQ
    )

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            client_transport.sendto(_build_rrq("ipxe.efi"), ("127.0.0.1", listener_port))
            data, _addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            assert struct.unpack("!H", data[:2])[0] == Opcode.ERROR
            assert struct.unpack("!H", data[2:4])[0] == ErrorCode.DISK_FULL
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())


def test_ack_collector_drops_acks_from_wrong_source_ip() -> None:
    """The per-transfer _AckCollector accepts ACKs only from the
    legitimate client's IP. ACK packets arriving from a different
    IP are dropped silently -- otherwise a hostile actor on the
    same LAN could fire spoofed ACKs to force premature transfer
    completion (and let the client end up with a truncated
    bootfile). Test by hand-feeding the collector packets from
    two different addresses."""
    from bty.pxe.tftp import _AckCollector

    collector = _AckCollector(expected_client_ip="10.0.0.42")
    # Legitimate ACK from the right IP, any port.
    legit = struct.pack("!HH", Opcode.ACK, 7)
    collector.datagram_received(legit, ("10.0.0.42", 54321))
    # Spoofed ACK from a different IP -- must be silently dropped.
    spoof = struct.pack("!HH", Opcode.ACK, 99)
    collector.datagram_received(spoof, ("10.0.0.99", 54321))
    # Pulling one ACK out should give back the legitimate one;
    # the spoofed one was filtered.
    assert collector._inbox.qsize() == 1
    queued = collector._inbox.get_nowait()
    assert queued.block == 7


def test_listener_accepts_uppercase_filename_via_case_insensitive_match(
    tmp_path: Path,
) -> None:
    """Some BIOS PXE ROMs uppercase the bootfile name (``IPXE.EFI``).
    The allowlist is defined in lowercase; the request name should
    be normalised before lookup so legacy clients aren't rejected
    with ACCESS_VIOLATION when the only sin is a different case."""
    payload = b"\x11" * 64
    (tmp_path / "ipxe.efi").write_bytes(payload)
    cfg = TftpConfig(root=tmp_path, allowlist=frozenset({"ipxe.efi"}))

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            # Note: uppercase filename in the RRQ.
            client_transport.sendto(_build_rrq("IPXE.EFI"), ("127.0.0.1", listener_port))
            data, _addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            # We expect DATA, not ERROR.
            assert struct.unpack("!H", data[:2])[0] == Opcode.DATA, (
                f"expected DATA, got opcode {struct.unpack('!H', data[:2])[0]} "
                f"(case-insensitive filename match regression)"
            )
            assert data[4:] == payload
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())


def test_listener_discards_duplicate_acks_without_retransmitting(tmp_path: Path) -> None:
    """A duplicate ACK for an earlier block must NOT cause the
    server to retransmit the current block -- PXE ROMs commonly
    emit dup-ACKs when they receive our retransmit, and replying
    with another retransmit wastes bandwidth without helping.

    Two DATA blocks total. The client ACKs block 1, then sends a
    SECOND ACK for block 1 (the dup), then ACKs block 2. The
    server should only emit DATA block 2 once -- exactly one DATA
    packet per block, no extras driven by the stale ACK."""
    payload = b"\xcc" * (DEFAULT_BLKSIZE + 100)  # 2 blocks: full + tail
    (tmp_path / "ipxe.efi").write_bytes(payload)
    cfg = TftpConfig(
        root=tmp_path,
        allowlist=frozenset({"ipxe.efi"}),
        block_timeout=2.0,
        max_retries=2,
    )

    async def _drive() -> None:
        listener_transport, listener_port = await _spawn_listener(cfg)
        client_transport, client = await _spawn_test_client()
        try:
            client_transport.sendto(_build_rrq("ipxe.efi"), ("127.0.0.1", listener_port))
            # Block 1 arrives.
            data1, server_addr = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            assert struct.unpack("!H", data1[:2])[0] == Opcode.DATA
            assert struct.unpack("!H", data1[2:4])[0] == 1
            # Send the legitimate ACK for block 1.
            client_transport.sendto(struct.pack("!HH", Opcode.ACK, 1), server_addr)
            # Now send a stale dup-ACK for block 1 -- this is what
            # PXE ROMs do when they receive our retransmit. The
            # server MUST NOT respond by retransmitting block 2;
            # block 2 should arrive exactly once, driven by the
            # legitimate ACK above.
            client_transport.sendto(struct.pack("!HH", Opcode.ACK, 1), server_addr)
            # Block 2 arrives -- once.
            data2, _ = await asyncio.wait_for(client.inbox.get(), timeout=5.0)
            assert struct.unpack("!H", data2[:2])[0] == Opcode.DATA
            assert struct.unpack("!H", data2[2:4])[0] == 2
            # Nothing else should be pending. If the dup-ACK had
            # caused a retransmit, a second block-2 DATA would be
            # in the queue right now.
            try:
                bonus = await asyncio.wait_for(client.inbox.get(), timeout=0.5)
            except TimeoutError:
                bonus = None
            assert bonus is None, (
                f"unexpected extra packet: opcode {struct.unpack('!H', bonus[0][:2])[0]}"
            )
            # Finish the transfer.
            client_transport.sendto(struct.pack("!HH", Opcode.ACK, 2), server_addr)
        finally:
            client_transport.close()
            listener_transport.close()

    _run(_drive())
