import asyncio
from pathlib import Path

import pytest

from adaos.services.eventbus import LocalEventBus
from adaos.services.router.service import RouterService


pytestmark = pytest.mark.anyio


async def test_ui_say_handler_is_async() -> None:
    """
    ui.say can be emitted during boot (e.g. greet_on_boot_skill). The router must not
    block the event loop in a synchronous handler because that can stall NATS WS
    handshakes and cause connect timeouts.
    """

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    handlers = list(getattr(bus, "_subs", {}).get("ui.say") or [])
    assert handlers, "expected RouterService to subscribe ui.say"
    assert any(asyncio.iscoroutinefunction(h) for h in handlers)

