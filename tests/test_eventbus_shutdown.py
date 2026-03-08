import asyncio

import pytest

from adaos.domain import Event
from adaos.services.eventbus import LocalEventBus


@pytest.mark.asyncio
async def test_local_event_bus_waits_for_async_handlers():
    bus = LocalEventBus()
    seen: list[str] = []

    async def handler(event: Event):
        await asyncio.sleep(0.05)
        seen.append(event.type)

    bus.subscribe("subnet.", handler)
    bus.publish(Event(type="subnet.stopping", payload={}, source="test", ts=0.0))

    ok = await bus.wait_for_idle(timeout=1.0)

    assert ok is True
    assert seen == ["subnet.stopping"]
