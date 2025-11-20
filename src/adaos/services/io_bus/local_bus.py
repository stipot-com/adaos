from __future__ import annotations
import json
from typing import Callable, Awaitable, Any

from adaos.services.eventbus import LocalEventBus
from adaos.domain import Event


class LocalIoBus:
    """Adapts LocalEventBus to IO bus interface used in webhook/sender pipelines."""

    def __init__(self, core: LocalEventBus | None = None) -> None:
        self._core = core or LocalEventBus()

    async def connect(self) -> None:  # parity with NATS bus
        return None

    async def publish_input(self, hub_id: str, envelope: dict) -> None:
        subject = f"tg.input.{hub_id}"
        self._core.publish(Event(type=subject, payload=envelope, source="io.local", ts=0.0))

    async def subscribe_output(self, bot_id: str, handler: Callable[[str, bytes], Awaitable[None]]) -> Any:
        prefix = f"tg.output.{bot_id}."

        def _wrap(ev: Event) -> None:
            try:
                data = json.dumps(ev.payload, ensure_ascii=False).encode("utf-8")
            except Exception:
                data = b"{}"
            # run async handler
            import asyncio

            async def run():
                await handler(ev.type, data)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(run())
            except RuntimeError:
                asyncio.run(run())

        self._core.subscribe(prefix, _wrap)
        return True

