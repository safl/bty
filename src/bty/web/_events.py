"""In-process pub/sub bus driving SSE updates for bty-web.

Each browser subscriber to ``GET /events/machines`` gets its own
:class:`asyncio.Queue`; mutating routes call ``publish()`` after
their update, the bus fans out to every queue, and the SSE generator
forwards the event to the wire.

Scope: single-process. Run ``uvicorn`` with one worker (the default
for ``uvicorn.run(app, ...)``); a multi-worker deployment would need
a real broker (Redis pub/sub, NATS, …) which we don't need for an
appliance serving a homelab fleet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass


@dataclass(frozen=True)
class MachineEvent:
    """A single bus event. ``html`` is the body sent to SSE subscribers."""

    name: str  # "machines-update" today; reserved for future event types
    html: str


class MachineEventBus:
    """A tiny fan-out bus. Synchronous publisher, async subscribers.

    Slow consumers are dropped silently rather than blocking the
    publisher: each subscriber's queue is bounded; if it's full when
    the publisher fires, the event is dropped for that subscriber and
    they will catch up on the next mutation. Trade-off favours
    publisher latency over delivery completeness — acceptable for a
    UI-refresh stream because every event carries the full snapshot.
    """

    def __init__(self, *, queue_size: int = 64) -> None:
        self._subscribers: list[asyncio.Queue[MachineEvent]] = []
        self._queue_size = queue_size

    def publish(self, event: MachineEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncGenerator[MachineEvent, None]:
        """Yield events for one subscriber. Cleans up on cancellation."""
        queue: asyncio.Queue[MachineEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass


def sse_format(event_name: str, data: str) -> bytes:
    """Encode a single SSE message. Multi-line data is split into ``data:`` lines."""
    parts = [f"event: {event_name}"]
    parts.extend(f"data: {line}" for line in data.split("\n"))
    parts.append("")  # trailing blank line terminates the event
    return ("\n".join(parts) + "\n").encode("utf-8")
