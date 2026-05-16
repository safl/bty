"""Structured event emission for bty-pxe-proxy and bty-tftp.

Each daemon writes one-line JSON to stdout for every meaningful
protocol event (DHCP discover received, DHCP offer sent, TFTP RRQ
received, transfer complete, transfer failed, ...). systemd
captures stdout as journald ``MESSAGE=`` fields; bty-web reads
them back via ``journalctl --output=json`` to render a live
diagnostic feed on /ui/settings.

Why JSON-on-stdout (vs. zmq, vs. a unix socket, vs. a shared
database): zero new runtime deps, daemons stay self-contained,
delivery is durable (journald persists), and operators can
``journalctl -u bty-pxe-proxy -f --output=json`` from a shell
to see the same data without going through the web UI.

Shape:

    {"evt": "dhcp.offer", "mac": "aa:bb:..", "arch": 7,
     "bootfile": "ipxe.efi", "server_ip": "192.168.1.31"}

The ``evt`` field is the only required key; everything else is
event-specific context. Consumers attempt ``json.loads`` on each
journal MESSAGE; non-JSON lines (startup banner, errors) parse
as ``JSONDecodeError`` and get skipped cleanly.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def emit(event: str, /, **fields: Any) -> None:
    """Print one-line JSON to stdout describing a protocol event.

    ``event`` is a dotted name like ``dhcp.discover`` /
    ``dhcp.offer`` / ``tftp.rrq`` / ``tftp.complete``. Keyword
    arguments are merged verbatim into the payload; values must
    be JSON-serialisable (``str``, ``int``, ``float``, ``bool``,
    ``None``, or nested lists/dicts of those).

    The line is flushed immediately so journald sees it without
    buffering delay even on a low event rate. ``flush()`` on
    each call is fine -- structured emits are low frequency
    (one per PXE boot, not per packet) and the syscall cost is
    negligible next to the network I/O they describe.
    """
    payload = {"evt": event, **fields}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()
