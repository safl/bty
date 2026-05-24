"""Direct unit tests for the in-process SSE bus and wire format.

Exercising end-to-end SSE through ``TestClient.stream`` is flaky - the
body is open-ended, so reading it sync hangs forever. Test the bus
behaviour at the source instead. We use ``asyncio.run`` rather than
adding a ``pytest-asyncio`` dep just for this file.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

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

    Bus is non-blocking by design - over a full queue, events are
    dropped for that subscriber rather than back-pressuring the route
    handler. Other subscribers are unaffected.
    """

    async def scenario() -> None:
        bus = MachineEventBus(queue_size=2)

        async def slow() -> None:
            async for _ in bus.subscribe():
                # Never advance - exercise the QueueFull branch.
                await asyncio.sleep(10)

        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        # Publish more than queue_size - must not raise or block.
        for i in range(5):
            bus.publish(MachineEvent(name="machines-update", html=f"<tr id={i}/>"))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_event_bus_unregisters_on_unsubscribe() -> None:
    async def scenario() -> int:
        bus = MachineEventBus()

        gen = bus.subscribe()
        # Drive one iteration so the queue is registered, then close
        # the iterator explicitly. ``aclose`` runs the ``finally`` that
        # unregisters the subscriber - same lifecycle uvicorn triggers
        # when the SSE client disconnects.
        publish_task = asyncio.get_event_loop().call_soon(
            bus.publish, MachineEvent(name="machines-update", html="")
        )
        del publish_task
        await anext(gen)
        await gen.aclose()
        return len(bus._subscribers)

    assert asyncio.run(scenario()) == 0


def test_event_bus_close_is_idempotent() -> None:
    """Multiple ``close()`` calls must not raise. The lifespan
    finally-block fires once on normal shutdown, but a test or
    operator-driven reload path may call it again later."""

    async def scenario() -> None:
        bus = MachineEventBus()
        bus.attach(asyncio.get_running_loop())
        await bus.close()
        await bus.close()
        await bus.close()

    asyncio.run(scenario())


def test_event_bus_close_without_attach_falls_through_safely() -> None:
    """``close()`` called before ``attach()`` (no event loop bound
    yet) must still set the closed flag without raising."""

    async def scenario() -> bool:
        bus = MachineEventBus()
        # No attach!
        await bus.close()
        return bus._closed.is_set()

    assert asyncio.run(scenario()) is True


def test_event_bus_close_wakes_idle_subscriber() -> None:
    """``close()`` unblocks every subscribe() generator immediately so
    bty-web shutdown isn't held up by SSE clients waiting on
    ``queue.get`` (previously caused uvicorn's full 90s graceful-
    shutdown timeout on every restart with an open browser tab)."""

    async def scenario() -> bool:
        bus = MachineEventBus()
        bus.attach(asyncio.get_running_loop())
        gen = bus.subscribe()
        # First yield drives the queue registration.
        first_task = asyncio.create_task(anext(gen))
        # Give the generator a tick to enter its wait state.
        await asyncio.sleep(0)
        # Close before any event is published; the subscriber must
        # exit (StopAsyncIteration) within the loop tick rather than
        # hang forever.
        await bus.close()
        try:
            await asyncio.wait_for(first_task, timeout=1.0)
            # If we got a value, that's a regression -- close should
            # cause StopAsyncIteration via the generator's return.
            return False
        except StopAsyncIteration:
            return True
        except TimeoutError:
            first_task.cancel()
            return False

    assert asyncio.run(scenario()) is True


def test_event_bus_close_unblocks_full_queue_subscriber() -> None:
    """A subscriber that's holding a full queue (slow consumer) must
    still unblock on ``close()`` so shutdown isn't held up by
    queue-saturated SSE generators. Edge case the lifespan
    finally-block needs to handle to avoid the 90s SIGKILL path."""

    async def scenario() -> bool:
        bus = MachineEventBus(queue_size=1)
        bus.attach(asyncio.get_running_loop())
        gen = bus.subscribe()
        # Saturate the queue.
        bus.publish(MachineEvent(name="machines-update", html="a"))
        # First yield consumes the queued event.
        await anext(gen)
        # Publish a second one; the subscriber hasn't pulled yet so
        # the queue has the latest event waiting.
        bus.publish(MachineEvent(name="machines-update", html="b"))
        # Now close; the next anext must resolve (with either the
        # remaining event or StopAsyncIteration -- either is fine).
        await bus.close()
        try:
            with contextlib.suppress(StopAsyncIteration):
                await asyncio.wait_for(anext(gen), timeout=1.0)
            return True
        except TimeoutError:
            return False

    assert asyncio.run(scenario()) is True


def test_event_bus_close_after_event_still_delivers_event() -> None:
    """If a publish happened and the subscriber was waiting on it,
    ``close`` mid-flight shouldn't drop the event already-queued.
    Guards against an over-eager shutdown that loses the last
    'machine flashed' broadcast."""

    async def scenario() -> str:
        bus = MachineEventBus()
        bus.attach(asyncio.get_running_loop())
        gen = bus.subscribe()
        # Publish first so the event is queued.
        bus.publish(MachineEvent(name="machines-update", html="<row/>"))
        # First yield reads the queued event.
        evt = await anext(gen)
        # Now close; the next yield resolves to StopAsyncIteration.
        await bus.close()
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        return evt.html

    assert asyncio.run(scenario()) == "<row/>"


def test_worker_event_payload_shape() -> None:
    """``worker_event`` packs the (kind, key, status) triple as a JSON
    payload under the well-known event name. The SSE wire format
    treats the payload opaquely; the browser parses it back."""
    import json

    from bty.web._events import WORKER_STATE_CHANGED, worker_event

    evt = worker_event("backup", "2026-05-23T10-00-00Z", "completed")
    assert evt.name == WORKER_STATE_CHANGED
    assert json.loads(evt.html) == {
        "kind": "backup",
        "key": "2026-05-23T10-00-00Z",
        "status": "completed",
    }


def test_worker_event_emits_through_bus() -> None:
    """End-to-end: ``worker_event`` builds an event the bus delivers
    to subscribers with the well-known event name."""
    from bty.web._events import WORKER_STATE_CHANGED, worker_event

    async def scenario() -> tuple[str, str]:
        bus = MachineEventBus()
        received: list[MachineEvent] = []

        async def consumer() -> None:
            async for ev in bus.subscribe():
                received.append(ev)
                break  # one event is enough

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)
        bus.publish(worker_event("hash", "demo.img.gz", "running"))
        await asyncio.wait_for(task, timeout=1.0)
        return received[0].name, received[0].html

    name, payload = asyncio.run(scenario())
    assert name == WORKER_STATE_CHANGED
    import json as _json

    assert _json.loads(payload)["kind"] == "hash"
    assert _json.loads(payload)["status"] == "running"
