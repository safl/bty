"""TFTP server for the bty-server appliance.

Sibling to :mod:`bty.pxe.proxy`. The PXE proxy answers DHCP
discovers with a bootfile name; the client (PXE option-ROM in
BIOS / non-HTTP UEFI firmware) then TFTPs that bootfile -- this
daemon is what serves it. The wire surface is tiny (RFC 1350 +
the option-extension RFCs 2347/2348/2349):

* RRQ  (opcode 1) -- read request: filename + mode + options
* WRQ  (opcode 2) -- write request; we reject all
* DATA (opcode 3) -- block_number + data
* ACK  (opcode 4) -- block_number
* ERROR (opcode 5) -- error_code + message
* OACK (opcode 6) -- option acknowledgement; per-option ``key,value``

The server runs read-only over a configurable root directory.
Filenames are validated against a small allowlist (default:
``ipxe.efi`` + ``undionly.kpxe``) so a TFTP server on the boot
network can't be made to spray arbitrary files.

Privileges: binding UDP 69 needs ``CAP_NET_BIND_SERVICE`` (port
< 1024). The systemd unit grants it as an ambient capability +
runs as the ``bty`` user; the daemon itself does no privilege
drop because it never had real privileges in the first place.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import struct
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from bty.pxe._daemon import bind_udp_socket, run_udp_daemon, setup_daemon_logging
from bty.pxe._events import emit as emit_event

log = logging.getLogger("bty.pxe.tftp")


def _format_peer(addr: tuple[str, int]) -> str:
    """``ip:port`` format for log lines + event payloads. Python's
    default tuple repr (``('192.168.1.50', 1234)``) is noisy in
    operator-facing output."""
    return f"{addr[0]}:{addr[1]}"


# --------------------------------------------------------------------------
# Wire codec
# --------------------------------------------------------------------------


class Opcode(IntEnum):
    RRQ = 1
    WRQ = 2
    DATA = 3
    ACK = 4
    ERROR = 5
    OACK = 6  # RFC 2347 option-extension acknowledgement


class ErrorCode(IntEnum):
    """RFC 1350 + RFC 2347 error codes. Numeric values are part of
    the wire format; don't renumber."""

    UNDEFINED = 0
    FILE_NOT_FOUND = 1
    ACCESS_VIOLATION = 2
    DISK_FULL = 3
    ILLEGAL_OPERATION = 4
    UNKNOWN_TID = 5
    FILE_EXISTS = 6
    NO_SUCH_USER = 7
    INVALID_OPTION = 8


# Default DATA payload size when neither side negotiates ``blksize``.
DEFAULT_BLKSIZE = 512

# Default per-DATA-block timeout (RFC 1350 doesn't pin a number;
# practical PXE clients retry every ~1-3 s). We pick 2s + 5 retries
# = 10s total wait per block, enough to ride out a transient drop
# without keeping a half-dead transfer open for too long.
DEFAULT_BLOCK_TIMEOUT = 2.0
DEFAULT_MAX_RETRIES = 5


@dataclass(frozen=True)
class Rrq:
    """Parsed read-request packet."""

    filename: str
    mode: str  # "octet" / "netascii" / "mail" -- we only serve octet
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Ack:
    """Parsed ACK packet -- client acknowledging a DATA block (or
    a 0-block ACK for our OACK reply)."""

    block: int


class TftpError(ValueError):
    """Raised by :func:`parse` on malformed packets. Inherits from
    ``ValueError`` so the server's catch-all also catches us."""


def parse(data: bytes) -> Rrq | Ack | None:
    """Decode an incoming TFTP packet to a high-level message.

    Returns ``None`` for opcodes we don't handle inbound (DATA,
    OACK, our-own-ERROR echo). Raises :class:`TftpError` on truncated
    or malformed bytes -- the server drops such packets silently.
    We don't try to reconstruct partial-but-mostly-OK packets.
    """
    if len(data) < 2:
        raise TftpError(f"packet too short: {len(data)} bytes")
    (opcode,) = struct.unpack("!H", data[:2])
    body = data[2:]
    if opcode == Opcode.RRQ:
        return _parse_rrq(body)
    if opcode == Opcode.WRQ:
        # Write requests are not part of our surface; the caller
        # signals an error back to the client.
        raise TftpError("WRQ not supported (read-only server)")
    if opcode == Opcode.ACK:
        if len(body) != 2:
            raise TftpError(f"ACK body must be exactly 2 bytes, got {len(body)}")
        (block,) = struct.unpack("!H", body)
        return Ack(block=block)
    if opcode in (Opcode.DATA, Opcode.OACK, Opcode.ERROR):
        # Server-side packets the client should never send us back.
        return None
    raise TftpError(f"unknown opcode {opcode}")


def _decode_ascii(raw: bytes, what: str) -> str:
    """ASCII-decode a TFTP wire field or raise :class:`TftpError`.
    Centralised so the parser doesn't repeat the try/except shape
    for every nul-terminated string."""
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TftpError(f"non-ASCII {what}: {exc}") from exc


def _parse_rrq(body: bytes) -> Rrq:
    """RRQ body: nul-terminated filename + nul-terminated mode +
    (optional) ``key\0value\0`` pairs per RFC 2347. body ends with
    ``\0`` so the final ``split`` chunk is an empty string."""
    parts = body.split(b"\x00")
    if len(parts) < 3:
        raise TftpError(f"RRQ truncated: only {len(parts) - 1} nul-terminated fields")
    filename = _decode_ascii(parts[0], "RRQ filename")
    mode = _decode_ascii(parts[1], "RRQ mode").lower()
    # Remaining parts are options: pairs of key, value. The last
    # part is the empty trailing string from the final ``\0``.
    extras = parts[2:-1] if parts[-1] == b"" else parts[2:]
    if len(extras) % 2 != 0:
        raise TftpError(f"RRQ options not paired: {len(extras)} entries")
    options = {
        _decode_ascii(extras[i], "RRQ option name").lower(): _decode_ascii(
            extras[i + 1], "RRQ option value"
        )
        for i in range(0, len(extras), 2)
    }
    return Rrq(filename=filename, mode=mode, options=options)


def build_data(block: int, payload: bytes) -> bytes:
    """DATA packet: opcode (3) + block number (2 bytes) + payload."""
    if block < 0 or block > 0xFFFF:
        raise ValueError(f"DATA block number out of range: {block}")
    return struct.pack("!HH", Opcode.DATA, block & 0xFFFF) + payload


def build_oack(options: dict[str, str]) -> bytes:
    """OACK packet (RFC 2347): opcode (6) + ``key\0value\0`` pairs."""
    body = bytearray()
    for k, v in options.items():
        body += k.encode("ascii") + b"\x00" + v.encode("ascii") + b"\x00"
    return struct.pack("!H", Opcode.OACK) + bytes(body)


def build_error(code: ErrorCode, message: str) -> bytes:
    """ERROR packet: opcode (5) + error code (2 bytes) +
    nul-terminated ASCII message."""
    return (
        struct.pack("!HH", Opcode.ERROR, int(code))
        + message.encode("ascii", errors="replace")
        + b"\x00"
    )


# --------------------------------------------------------------------------
# OACK option negotiation
# --------------------------------------------------------------------------


# Per RFC 2348: blksize range 8..65464. Practical ceiling drops to
# the MTU's payload (~1456 over plain Ethernet). We cap a bit lower
# than that to avoid fragmentation on networks with MTU surprises
# (VPN tunnels, jumbo-frame mismatch, etc).
_MIN_BLKSIZE = 8
_MAX_BLKSIZE = 1432


def _parse_int(s: str, default: int) -> int:
    """Parse ``s`` as int, or fall back to ``default`` on a
    non-numeric value. Single place to put the swallow-ValueError
    pattern for RRQ option parsing."""
    try:
        return int(s)
    except ValueError:
        return default


def _negotiate_options(requested: dict[str, str], file_size: int) -> dict[str, str]:
    """Decide which RRQ options we accept + what values we report
    back in OACK. Per RFC 2347, options the server doesn't recognise
    are silently dropped -- the client falls back to defaults.

    Supported:
      * ``blksize``  -- agree on min(requested, MAX). Clamp to
        sane range so the client can't talk us into 64KB packets
        that fragment on every link.
      * ``tsize``    -- per RFC 2349, when the client sends
        ``tsize=0`` it's asking for the file size. We answer with
        the actual bytes. (Servers may also use tsize on WRQs to
        announce incoming file size; we don't accept WRQs.)
      * ``timeout``  -- RFC 2349 timeout-interval negotiation.
        Echoed back verbatim if in valid range.

    Anything else is ignored. ``windowsize`` (RFC 7440) isn't
    supported yet -- pure ACK-per-DATA flow keeps the server
    simple; modern PXE ROMs negotiate windowsize for speed but
    fall back fine when the server doesn't echo it.
    """
    accepted: dict[str, str] = {}
    if "blksize" in requested:
        requested_blksize = _parse_int(requested["blksize"], DEFAULT_BLKSIZE)
        clamped = max(_MIN_BLKSIZE, min(requested_blksize, _MAX_BLKSIZE))
        accepted["blksize"] = str(clamped)
    if "tsize" in requested:
        accepted["tsize"] = str(file_size)
    if "timeout" in requested:
        # RFC 2349 mandates 1..255 seconds for the timeout option.
        requested_timeout = _parse_int(requested["timeout"], int(DEFAULT_BLOCK_TIMEOUT))
        if 1 <= requested_timeout <= 255:
            accepted["timeout"] = str(requested_timeout)
    return accepted


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


# Concurrent-transfer cap. Each in-flight transfer owns its own
# ephemeral UDP socket + a couple of asyncio tasks; an unbounded
# fleet of them would let a hostile client (or a flood of
# legitimate clients during a wave reboot) exhaust file
# descriptors. 32 covers a generous homelab-scale wave reboot
# (a rack-worth of PXE clients hitting the same instant) without
# being so high that DoS is meaningful.
DEFAULT_MAX_CONCURRENT_TRANSFERS = 32


@dataclass(frozen=True)
class TftpConfig:
    """Resolved daemon config: where files live, which ones are
    servable."""

    root: Path
    allowlist: frozenset[str]
    # Per-transfer timeout / retry count, set at startup. The
    # OACK ``timeout`` option can override these per-transfer if
    # the client negotiates it.
    block_timeout: float = DEFAULT_BLOCK_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_concurrent_transfers: int = DEFAULT_MAX_CONCURRENT_TRANSFERS


# Default allowlist: every iPXE bootfile the bty PXE-proxy maps
# arches to. The appliance ships ``ipxe.efi`` + ``undionly.kpxe``
# by default; the other names are listed so an operator who drops
# the right binary at /var/lib/tftpboot/ipxe-arm64.efi (etc.) gets
# it served without touching dnsmasq config or the allowlist. A
# request for a filename in the list but not on disk fails clean
# with FILE_NOT_FOUND -- the target falls through to whatever
# other DHCP is on the segment, instead of getting a bogus binary.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "undionly.kpxe",  # legacy BIOS x86
        "ipxe.efi",  # UEFI x86-64 (default ship)
        "ipxe-i386.efi",  # UEFI IA32 (operator-supplied)
        "ipxe-arm32.efi",  # UEFI ARM 32-bit (operator-supplied)
        "ipxe-arm64.efi",  # UEFI ARM 64-bit (RPi 4 in UEFI mode, etc.)
    }
)


def _resolve_safe_path(root: Path, filename: str) -> Path | None:
    """Return the absolute path of ``root/filename`` if it stays
    inside ``root`` and is a regular file. Returns ``None`` on
    any of: path traversal (``..``), absolute path, symlink to
    outside root, missing file. The allowlist already gates this
    by name; the realpath check is belt + braces against a hostile
    operator-staged symlink in ``root``.

    Path-traversal attempts (a relative_to() mismatch after
    resolve()) are surfaced via :func:`log.warning` + a
    ``tftp.traversal`` structured event so the operator sees them
    in the journal feed rather than the request just vanishing as
    FILE_NOT_FOUND.
    """
    if not filename or "\x00" in filename:
        return None
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        log.warning("tftp: rejected traversal-shaped filename %r -> %s", filename, candidate)
        emit_event("tftp.traversal", file=filename, resolved=str(candidate))
        return None
    if not candidate.is_file():
        return None
    return candidate


class _ListenerProtocol(asyncio.DatagramProtocol):
    """Listens on UDP 69. On each incoming RRQ we spawn a transfer
    task that opens its own ephemeral-port socket -- per RFC 1350,
    each transfer uses a distinct TID (transfer ID = source port)
    so multiple concurrent reads don't tangle their DATA/ACK pairs
    on a shared socket."""

    def __init__(self, cfg: TftpConfig) -> None:
        self._cfg = cfg
        self._transport: asyncio.DatagramTransport | None = None
        # Track open transfer tasks so ``stop()`` can wait for them.
        self._transfers: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport
        log.info(
            "bty-tftp: listening on UDP 69; serving %s from %s",
            sorted(self._cfg.allowlist),
            self._cfg.root,
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = parse(data)
        except TftpError as exc:
            log.debug("tftp: malformed packet from %s: %s", addr, exc)
            return
        if not isinstance(msg, Rrq):
            # ACK / DATA / OACK arriving on port 69 belongs to a
            # transfer we never started; ignore (the listener
            # socket only accepts RRQs).
            return
        # Refuse new transfers once we hit the concurrent-transfer
        # cap. Reply with an explicit ERROR (DISK_FULL is the
        # closest match in RFC 1350 -- "we ran out of capacity")
        # so the client falls through cleanly instead of timing
        # out waiting for DATA that'll never arrive.
        if len(self._transfers) >= self._cfg.max_concurrent_transfers:
            log.warning(
                "tftp: at concurrent-transfer cap %d; refusing RRQ from %s",
                self._cfg.max_concurrent_transfers,
                addr,
            )
            error_task = asyncio.ensure_future(
                self._send_error_oneshot(addr, ErrorCode.DISK_FULL, "server busy; retry later")
            )
            # Track the error-send task too so ``stop()``-style
            # waits don't leave it dangling; it removes itself from
            # the set on completion via ``add_done_callback``.
            self._transfers.add(error_task)
            error_task.add_done_callback(self._transfers.discard)
            return
        task = asyncio.ensure_future(self._serve_one(msg, addr))
        self._transfers.add(task)
        task.add_done_callback(self._transfers.discard)

    async def _serve_one(self, rrq: Rrq, client_addr: tuple[str, int]) -> None:
        """One read transfer, end to end."""
        peer = _format_peer(client_addr)
        log.info(
            "tftp: %s requested %r (mode=%s, options=%s)",
            peer,
            rrq.filename,
            rrq.mode,
            rrq.options,
        )
        emit_event("tftp.rrq", peer=peer, file=rrq.filename, mode=rrq.mode)
        payload = await self._read_servable_payload(rrq, client_addr)
        if payload is None:
            return  # _read_servable_payload already sent the error
        oack_options = _negotiate_options(rrq.options, file_size=len(payload))
        # Per-transfer timeout overrides default if negotiated.
        timeout = float(oack_options.get("timeout", self._cfg.block_timeout))
        blksize = int(oack_options.get("blksize", DEFAULT_BLKSIZE))
        await _Transfer(
            client_addr=client_addr,
            payload=payload,
            blksize=blksize,
            timeout=timeout,
            max_retries=self._cfg.max_retries,
            oack=oack_options if oack_options else None,
            filename=rrq.filename,
        ).run()

    async def _read_servable_payload(
        self, rrq: Rrq, client_addr: tuple[str, int]
    ) -> bytes | None:
        """Validate the RRQ + return the file's bytes, or ``None``
        when we sent the client an ERROR instead.

        Three failure modes, each with its own TFTP ErrorCode:

        * Non-octet mode -> ``ILLEGAL_OPERATION``. We don't serve
          netascii (would require LF/CRLF rewriting + nobody PXE-boots
          netascii) or mail (obsolete).
        * Filename not in allowlist or read failed -> ``ACCESS_VIOLATION``.
        * File missing on disk -> ``FILE_NOT_FOUND``.

        Filenames are normalised to lowercase before allowlist + path
        lookup -- some BIOS PXE ROMs send ``IPXE.EFI``, we serve them
        by treating filenames as case-insensitive end-to-end. Costs
        nothing on standard clients; fixes a real-world silent boot
        failure.
        """
        if rrq.mode != "octet":
            await self._send_error_oneshot(
                client_addr,
                ErrorCode.ILLEGAL_OPERATION,
                f"only octet mode supported, got {rrq.mode!r}",
            )
            return None
        filename = rrq.filename.lower()
        if filename not in self._cfg.allowlist:
            await self._send_error_oneshot(
                client_addr,
                ErrorCode.ACCESS_VIOLATION,
                f"file not in allowlist: {rrq.filename!r}",
            )
            return None
        path = _resolve_safe_path(self._cfg.root, filename)
        if path is None:
            await self._send_error_oneshot(
                client_addr, ErrorCode.FILE_NOT_FOUND, f"no such file: {rrq.filename!r}"
            )
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            log.warning("tftp: cannot read %s: %s", path, exc)
            await self._send_error_oneshot(
                client_addr, ErrorCode.ACCESS_VIOLATION, "file unreadable"
            )
            return None

    async def _send_error_oneshot(
        self, client_addr: tuple[str, int], code: ErrorCode, message: str
    ) -> None:
        """ERROR over a fresh ephemeral socket (per RFC: ERRORs use
        the transfer's own TID, not the listener port). One-shot
        means we don't wait for an ACK -- ERROR aborts the transfer
        from both sides per the spec, so we don't need asyncio
        plumbing here, just a plain sendto + close."""
        peer = _format_peer(client_addr)
        log.info("tftp: ERROR %s -> %s: %s", code.name, peer, message)
        emit_event("tftp.error", peer=peer, code=code.name, message=message)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", 0))  # ephemeral TID
            sock.sendto(build_error(code, message), client_addr)
        finally:
            sock.close()

    def error_received(self, exc: Exception) -> None:
        log.warning("tftp: listener socket error: %s", exc)


@dataclass
class _Transfer:
    """Single read transfer over an ephemeral-port socket. Drives
    the per-block DATA/ACK ping-pong with retry."""

    client_addr: tuple[str, int]
    payload: bytes
    blksize: int
    timeout: float
    max_retries: int
    oack: dict[str, str] | None  # OACK options to send first if any
    # Carried for structured-event payloads only; the wire path
    # neither reads nor writes it.
    filename: str = ""

    async def run(self) -> None:
        # ``local_addr=("0.0.0.0", 0)`` lets asyncio bind an
        # ephemeral port (the TID per RFC 1350) and own the socket
        # lifecycle for us; we just supply the protocol factory.
        # Empty-host shorthand "" doesn't work with asyncio's
        # resolver -- it needs an explicit address. The factory
        # closure captures the legitimate client's IP so the
        # collector can drop ACKs from a different source -- a
        # hostile actor on the same broadcast segment could
        # otherwise spoof ACKs to disrupt the transfer.
        loop = asyncio.get_running_loop()
        client_ip = self.client_addr[0]
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _AckCollector(expected_client_ip=client_ip),
                local_addr=("0.0.0.0", 0),
            )
        except OSError as exc:
            log.warning("tftp: could not open transfer socket: %s", exc)
            return
        try:
            await self._drive(transport, protocol)
        finally:
            transport.close()

    async def _drive(self, transport: asyncio.DatagramTransport, protocol: _AckCollector) -> None:
        """Send (re)transmit DATA/OACK, await ACK with timeout, repeat."""
        # Block 0 ACK: client acks our OACK (if any) before block 1 DATA flows.
        # Without OACK, we skip straight to block 1.
        if self.oack is not None and not await self._send_and_wait_ack(
            transport, protocol, build_oack(self.oack), 0
        ):
            return
        # Block 1..N data flow.
        total = len(self.payload)
        block = 1
        offset = 0
        while True:
            chunk = self.payload[offset : offset + self.blksize]
            if not await self._send_and_wait_ack(
                transport, protocol, build_data(block, chunk), block
            ):
                return
            offset += self.blksize
            if len(chunk) < self.blksize:
                # Last data block (possibly empty) signals EOF.
                peer = _format_peer(self.client_addr)
                log.info(
                    "tftp: transfer to %s complete (%d bytes in %d block(s))",
                    peer,
                    total,
                    block,
                )
                emit_event(
                    "tftp.complete",
                    peer=peer,
                    file=self.filename,
                    bytes=total,
                    blocks=block,
                )
                return
            block = (block + 1) & 0xFFFF  # 16-bit wrap is fine for our file sizes

    async def _send_and_wait_ack(
        self,
        transport: asyncio.DatagramTransport,
        protocol: _AckCollector,
        packet: bytes,
        expected_block: int,
    ) -> bool:
        """Send ``packet`` to the client, wait for an ACK with
        ``block == expected_block``, retry up to ``max_retries``
        times. Returns ``True`` on success, ``False`` on giving up.

        Two nested loops:
          * outer  -- one retransmit per iteration on real timeout.
          * inner  -- drain stale / duplicate ACKs without
            retransmitting. PXE ROMs commonly emit a dup-ACK when
            they receive our retransmit (so they got our DATA twice);
            resending the DATA *yet again* in response would waste
            bandwidth without helping. We just discard the stale
            ACK and keep waiting on the inner loop until either the
            right ACK arrives or the per-retransmit timeout elapses.
        """
        loop = asyncio.get_running_loop()
        for attempt in range(self.max_retries + 1):
            transport.sendto(packet, self.client_addr)
            deadline = loop.time() + self.timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    # Genuine timeout: jump to the outer loop's
                    # retransmit step (if we still have attempts).
                    log.debug(
                        "tftp: timeout waiting for ACK block %d from %s (attempt %d/%d)",
                        expected_block,
                        _format_peer(self.client_addr),
                        attempt + 1,
                        self.max_retries + 1,
                    )
                    break
                try:
                    ack = await asyncio.wait_for(protocol.next_ack(), timeout=remaining)
                except TimeoutError:
                    continue  # let the deadline check fire on the next inner pass
                if ack.block == expected_block:
                    return True
                # Stale / duplicate ACK -- discard and keep waiting
                # WITHOUT retransmitting.
                log.debug(
                    "tftp: stale ACK block %d (expected %d) from %s; ignoring",
                    ack.block,
                    expected_block,
                    _format_peer(self.client_addr),
                )
        peer = _format_peer(self.client_addr)
        log.warning(
            "tftp: gave up waiting for ACK block %d from %s after %d retries",
            expected_block,
            peer,
            self.max_retries,
        )
        emit_event(
            "tftp.giveup",
            peer=peer,
            file=self.filename,
            block=expected_block,
            retries=self.max_retries,
        )
        return False


class _AckCollector(asyncio.DatagramProtocol):
    """Receives ACKs (and ignores anything else) on the per-transfer
    socket. The transfer driver pulls them via :meth:`next_ack`.

    Only accepts datagrams from the original client's source IP --
    per RFC 1350 the client's TID stays fixed for the connection.
    We're lenient on source port (some non-spec clients change it,
    or routers NAT it), strict on source IP because a different IP
    is unambiguously a different host: either a hostile actor
    spoofing ACKs to disrupt the transfer, or noise. The inbox
    queue is bounded so a flood of bogus ACKs from the right IP
    can't grow memory without limit either.
    """

    # Bound the inbox queue: under normal flow at most one ACK
    # lives there between sends, but a chatty client (or one
    # blasting dup-ACKs) shouldn't be able to make us hold the
    # whole stream in memory. 256 is generous for any sane
    # transfer pattern.
    _INBOX_MAXSIZE = 256

    def __init__(self, expected_client_ip: str) -> None:
        self._inbox: asyncio.Queue[Ack] = asyncio.Queue(maxsize=self._INBOX_MAXSIZE)
        self._expected_client_ip = expected_client_ip

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] != self._expected_client_ip:
            # Different source IP -- not our client. Drop silently.
            return
        try:
            msg = parse(data)
        except TftpError:
            return
        if not isinstance(msg, Ack):
            # DATA / OACK echoed back, ERROR from client, etc.
            # The driver's timeout handles missing ACKs.
            return
        try:
            self._inbox.put_nowait(msg)
        except asyncio.QueueFull:
            # Overflow: drop the new ACK rather than block. The
            # driver picks up from whatever's queued.
            log.debug(
                "tftp: ack inbox full (%d items); dropping new ACK block %d",
                self._INBOX_MAXSIZE,
                msg.block,
            )

    async def next_ack(self) -> Ack:
        """Pull the next ACK off the inbox queue. The driver checks
        the block number itself + logs stale ACKs."""
        return await self._inbox.get()


async def _serve(cfg: TftpConfig, interface: str | None) -> None:
    sock = bind_udp_socket(69, interface=interface)
    await run_udp_daemon(sock, lambda: _ListenerProtocol(cfg), log_prefix="tftp")


def _parse_args(argv: list[str]) -> tuple[TftpConfig, str | None, bool]:
    """Parse + validate command-line args. Returns the resolved
    config, the operator's interface choice (or None for "all
    interfaces"), and a verbose-flag the caller uses to set up
    logging. Pure: no side effects on global state."""
    parser = argparse.ArgumentParser(
        prog="bty-tftp",
        description=(
            "Read-only TFTP server for bty's PXE bootfiles. Serves a "
            "small allowlist of files (iPXE binaries by default) from "
            "a single root directory. Pairs with bty-pxe-proxy.service."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/var/lib/tftpboot"),
        help="Directory the allowlisted files live in (default: /var/lib/tftpboot).",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=None,
        help=(
            "Add a filename to the served allowlist. Repeatable. When "
            "any --allow is passed, REPLACES the default allowlist "
            "(ipxe.efi + undionly.kpxe + ipxe-arm64.efi). The allowlist "
            "is matched on the request's exact ``filename`` field; "
            "subdirectories are rejected."
        ),
    )
    parser.add_argument(
        "--interface",
        default=None,
        help=(
            "Bind exclusively to a specific network interface "
            "(SO_BINDTODEVICE). Optional; without it the daemon "
            "answers RRQs on every interface."
        ),
    )
    parser.add_argument(
        "--block-timeout",
        type=float,
        default=DEFAULT_BLOCK_TIMEOUT,
        help=f"Per-block ACK timeout in seconds (default: {DEFAULT_BLOCK_TIMEOUT}).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Per-block retry count before giving up (default: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (debug-level) logging."
    )
    ns = parser.parse_args(argv)
    if not ns.root.is_dir():
        parser.error(f"--root {ns.root!s} is not a directory")
    allowlist = frozenset(ns.allow) if ns.allow else DEFAULT_ALLOWLIST
    cfg = TftpConfig(
        root=ns.root.resolve(),
        allowlist=allowlist,
        block_timeout=ns.block_timeout,
        max_retries=ns.max_retries,
    )
    return cfg, ns.interface, ns.verbose


def main(argv: list[str] | None = None) -> int:
    """``bty-tftp`` console-script entry."""
    cfg, interface, verbose = _parse_args(sys.argv[1:] if argv is None else argv)
    setup_daemon_logging(verbose)
    try:
        asyncio.run(_serve(cfg, interface))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
