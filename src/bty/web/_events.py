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
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass


@dataclass(frozen=True)
class MachineEvent:
    """A single bus event. ``html`` is the body sent to SSE subscribers."""

    name: str  # SSE event name routed to subscribers
    html: str


class MachineEventBus:
    """A tiny fan-out bus. Thread-safe publisher, async subscribers.

    Slow consumers are dropped silently rather than blocking the
    publisher: each subscriber's queue is bounded; if it's full when
    the publisher fires, the event is dropped for that subscriber and
    they will catch up on the next mutation. Trade-off favours
    publisher latency over delivery completeness - acceptable for a
    UI-refresh stream because every event carries the full snapshot.

    ``publish`` may be called from any thread. ``attach`` captures the
    asyncio loop the SSE consumers are running on; thereafter,
    cross-thread publishes hop through ``call_soon_threadsafe`` to
    deliver into ``asyncio.Queue`` safely. The worker threads in
    :mod:`bty.web._jobs` (catalog / hash / release managers) rely
    on this.
    """

    def __init__(self, *, queue_size: int = 64) -> None:
        self._subscribers: list[asyncio.Queue[MachineEvent]] = []
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        # Set by ``close()`` so every blocked ``subscribe`` generator
        # wakes up and exits cleanly on bty-web shutdown. Without
        # this the StreamingResponse held SSE clients open until
        # uvicorn's 90s graceful-shutdown timeout SIGKILL'd the
        # process every restart.
        self._closed = asyncio.Event()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the event loop SSE subscribers run on.

        Called once from ``create_app``'s lifespan startup hook so
        cross-thread publishers (the hash worker, the release-
        manager fetcher) can hop into the loop's thread before
        touching ``asyncio.Queue`` state.
        """
        self._loop = loop

    def publish(self, event: MachineEvent) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._fanout, event)
                return
            except RuntimeError:
                # Loop closed between is_running check and the call;
                # fall through to direct fanout (no-op for closed bus).
                pass
        # No loop attached (unit tests for this module) or loop isn't
        # running - direct fanout is safe in that case.
        self._fanout(event)

    def _fanout(self, event: MachineEvent) -> None:
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def subscribe(self) -> AsyncGenerator[MachineEvent, None]:
        """Yield events for one subscriber.

        Cleans up on client cancellation OR on bus close (lifespan
        shutdown). Without the close-aware wait the SSE generators
        block on ``queue.get`` indefinitely, so uvicorn's graceful
        shutdown waits its full 90s timeout before SIGKILL'ing the
        process every restart.
        """
        queue: asyncio.Queue[MachineEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        try:
            while not self._closed.is_set():
                # Race the next event against shutdown. ``wait()`` on
                # the closed event resolves the instant ``close()`` is
                # called; ``queue.get`` resolves on a published event.
                # Whichever wins, we cancel the other.
                get_task = asyncio.create_task(queue.get())
                close_task = asyncio.create_task(self._closed.wait())
                try:
                    done, _pending = await asyncio.wait(
                        (get_task, close_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (get_task, close_task):
                        if not task.done():
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await task
                if get_task in done and not get_task.cancelled():
                    yield get_task.result()
                else:
                    return
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)

    async def close(self) -> None:
        """Signal every subscribed generator to exit.

        Called from ``create_app``'s lifespan finally-block on
        shutdown so SSE-holding browser tabs don't pin the worker
        for the full uvicorn-graceful-shutdown timeout.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._closed.set)
                return
            except RuntimeError:
                pass
        self._closed.set()


def sse_format(event_name: str, data: str) -> bytes:
    """Encode a single SSE message. Multi-line data is split into ``data:`` lines."""
    parts = [f"event: {event_name}"]
    parts.extend(f"data: {line}" for line in data.split("\n"))
    parts.append("")  # trailing blank line terminates the event
    return ("\n".join(parts) + "\n").encode("utf-8")
