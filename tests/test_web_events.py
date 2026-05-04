"""Direct unit tests for the in-process SSE bus and wire format.

Exercising end-to-end SSE through ``TestClient.stream`` is flaky — the
body is open-ended, so reading it sync hangs forever. Test the bus
behaviour at the source instead. We use ``asyncio.run`` rather than
adding a ``pytest-asyncio`` dep just for this file.
"""

from __future__ import annotations

import asyncio

from bty.web._events import MachineEvent, MachineEventBus, sse_format


def test_sse_format_single_line() -> None:
    out = sse_format("hello", "world")
    assert out == b"event: hello\ndata: world\n\n"


def test_sse_format_multiline_data() -> None:
    out = sse_format("snapshot", "<tr>a</tr>\n<tr>b</tr>")
    assert out == b"event: snapshot\ndata: <tr>a</tr>\ndata: <tr>b</tr>\n\n"


def test_event_bus_delivers_event_to_subscriber() -> None:
    async def scenario() -> list[MachineEvent]:
        bus = MachineEventBus()
        received: list[MachineEvent] = []

        async def consumer() -> None:
            async for event in bus.subscribe():
                received.append(event)
                break

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)  # let consumer register
        bus.publish(MachineEvent(name="machines-update", html="<tr/>"))
        await asyncio.wait_for(task, timeout=1.0)
        return received

    assert asyncio.run(scenario()) == [MachineEvent(name="machines-update", html="<tr/>")]


def test_event_bus_multi_subscriber_fanout() -> None:
    async def scenario() -> tuple[int, int]:
        bus = MachineEventBus()
        seen_a: list[MachineEvent] = []
        seen_b: list[MachineEvent] = []

        async def consumer(out: list[MachineEvent]) -> None:
            async for event in bus.subscribe():
                out.append(event)
                break

        a = asyncio.create_task(consumer(seen_a))
        b = asyncio.create_task(consumer(seen_b))
        await asyncio.sleep(0)
        bus.publish(MachineEvent(name="machines-update", html="<tr/>"))
        await asyncio.wait_for(asyncio.gather(a, b), timeout=1.0)
        return len(seen_a), len(seen_b)

    assert asyncio.run(scenario()) == (1, 1)


def test_event_bus_drops_for_full_subscriber() -> None:
    """Slow consumers must not block the publisher.

    Bus is non-blocking by design — over a full queue, events are
    dropped for that subscriber rather than back-pressuring the route
    handler. Other subscribers are unaffected.
    """

    async def scenario() -> None:
        bus = MachineEventBus(queue_size=2)

        async def slow() -> None:
            async for _ in bus.subscribe():
                # Never advance — exercise the QueueFull branch.
                await asyncio.sleep(10)

        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        # Publish more than queue_size — must not raise or block.
        for i in range(5):
            bus.publish(MachineEvent(name="machines-update", html=f"<tr id={i}/>"))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_event_bus_unregisters_on_unsubscribe() -> None:
    async def scenario() -> int:
        bus = MachineEventBus()

        gen = bus.subscribe()
        # Drive one iteration so the queue is registered, then close
        # the iterator explicitly. ``aclose`` runs the ``finally`` that
        # unregisters the subscriber — same lifecycle uvicorn triggers
        # when the SSE client disconnects.
        publish_task = asyncio.get_event_loop().call_soon(
            bus.publish, MachineEvent(name="machines-update", html="")
        )
        del publish_task
        await anext(gen)
        await gen.aclose()
        return len(bus._subscribers)

    assert asyncio.run(scenario()) == 0
