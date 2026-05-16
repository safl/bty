"""Shared asyncio-UDP daemon shell for the bty PXE stack.

Both :mod:`bty.pxe.proxy` and :mod:`bty.pxe.tftp` run the same
shape: a single ``DatagramProtocol`` bound to one well-known UDP
port, plus a SIGINT/SIGTERM-driven graceful stop. This module
extracts that scaffolding so each daemon module focuses on its
protocol-specific logic.

Not generic-async-server infrastructure -- just enough shape to
deduplicate the two PXE-stack daemons. If a third asyncio daemon
ships under :mod:`bty.pxe` it can use this; if a non-UDP daemon
ever needs the signal-handler dance, that's a separate helper.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from collections.abc import Callable

log = logging.getLogger("bty.pxe.daemon")


async def run_udp_daemon(
    sock: socket.socket,
    protocol_factory: Callable[[], asyncio.DatagramProtocol],
    log_prefix: str,
) -> None:
    """Drive a UDP daemon to completion: attach ``protocol_factory``
    to the pre-bound ``sock``, wait for SIGINT / SIGTERM, then
    close the transport.

    Caller owns ``sock`` and its bind state (port, interface
    pinning, broadcast flag, etc.); this helper just attaches it to
    the asyncio event loop. On any error during endpoint creation
    the socket is closed and the exception re-raised, so the caller
    doesn't have to remember the close-on-error step.

    ``log_prefix`` lands in the "signal X received" log line so
    operators see ``pxe: ...`` vs ``tftp: ...`` rather than a
    generic ``daemon: ...``.
    """
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(protocol_factory, sock=sock)
    except Exception:
        sock.close()
        raise
    stop = asyncio.Event()

    def _sig_handler(signum: int) -> None:
        log.info("%s: signal %d received; stopping", log_prefix, signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sig_handler, sig)

    try:
        await stop.wait()
    finally:
        transport.close()
