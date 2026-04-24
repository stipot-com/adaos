import asyncio
from pathlib import Path
import sys
import types

import pytest

from adaos.domain import Event
from adaos.services.eventbus import LocalEventBus

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

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


async def test_io_out_stream_publish_routes_to_webspace_scoped_browser_topic() -> None:
    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    seen: list[object] = []
    bus.subscribe("webio.stream.default.telemetry_feed", lambda ev: seen.append(ev))
    bus.publish(
        Event(
            type="io.out.stream.publish",
            source="test",
            ts=123.0,
            payload={
                "receiver": "telemetry_feed",
                "data": {"value": 42},
                "_meta": {"webspace_id": "default"},
            },
        )
    )

    await bus.wait_for_idle(timeout=1.0)
    assert len(seen) == 1
    event = seen[0]
    assert getattr(event, "type", "") == "webio.stream.default.telemetry_feed"
    assert getattr(event, "payload", {}).get("data") == {"value": 42}
    assert getattr(event, "payload", {}).get("webspace_id") == "default"

