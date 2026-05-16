"""Tests for the shared :mod:`bty.pxe._daemon` helpers.

``run_udp_daemon`` is exercised end-to-end by the proxy + tftp
integration tests; here we cover ``bind_udp_socket`` in isolation
since it carries the socket-option matrix shared by both daemons.

The socket-binding tests bind to port 0 to avoid colliding with
real UDP services on the test host -- the helper still sets every
socket option the production callers care about (REUSEADDR,
BROADCAST, BINDTODEVICE) along the way.
"""

from __future__ import annotations

import os
import socket

import pytest

from bty.pxe._daemon import bind_udp_socket


def test_bind_udp_socket_sets_reuseaddr_and_nonblocking() -> None:
    """Every caller wants REUSEADDR (so a cutover from another
    daemon doesn't hang on TIME_WAIT) + non-blocking (so asyncio
    can attach via create_datagram_endpoint)."""
    s = bind_udp_socket(0)
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        assert s.getblocking() is False
    finally:
        s.close()


def test_bind_udp_socket_no_broadcast_by_default() -> None:
    """TFTP doesn't broadcast; the helper must NOT enable SO_BROADCAST
    unless the caller asks for it."""
    s = bind_udp_socket(0)
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST) == 0
    finally:
        s.close()


def test_bind_udp_socket_broadcast_flag_enables_so_broadcast() -> None:
    """PXE proxy sends to 255.255.255.255:68 -- requires SO_BROADCAST."""
    s = bind_udp_socket(0, broadcast=True)
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST) != 0
    finally:
        s.close()


@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="SO_BINDTODEVICE needs root / CAP_NET_RAW; skip in user-mode test runs",
)
def test_bind_udp_socket_with_interface_sets_bindtodevice() -> None:  # pragma: no cover (root-only)
    """Smoke test for the SO_BINDTODEVICE path. Only runs under
    root since the kernel rejects the setsockopt otherwise."""
    s = bind_udp_socket(0, interface="lo")
    try:
        bound = s.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16)
        assert bound.rstrip(b"\x00") == b"lo"
    finally:
        s.close()
