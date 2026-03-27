import asyncio
import logging

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


def test_local_event_bus_subscribe_debug_is_quiet_by_default(monkeypatch, caplog):
    monkeypatch.delenv("ADAOS_EVENTBUS_TRACE_SUBSCRIBE", raising=False)
    bus = LocalEventBus()

    def handler(event: Event):
        return None

    with caplog.at_level(logging.DEBUG, logger="adaos.eventbus"):
        bus.subscribe("subnet.", handler)

    assert "bus.subscribe" not in caplog.text


def test_local_event_bus_subscribe_debug_can_be_enabled(monkeypatch, caplog):
    monkeypatch.setenv("ADAOS_EVENTBUS_TRACE_SUBSCRIBE", "1")
    bus = LocalEventBus()

    def handler(event: Event):
        return None

    with caplog.at_level(logging.DEBUG, logger="adaos.eventbus"):
        bus.subscribe("subnet.", handler)

    assert "bus.subscribe" in caplog.text
