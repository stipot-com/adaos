from __future__ import annotations
from typing import Mapping, Any
import json
import urllib.request


class HttpFallbackBus:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def post(self, path: str, payload: Mapping[str, Any]) -> int:
        url = f"{self._base}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return resp.status

    # IO bus parity methods
    async def connect(self) -> None:
        return None

    async def publish_input(self, hub_id: str, envelope: Mapping[str, Any]) -> None:
        # Convention path for fallback bus
        self.post(f"/io/bus/tg.input.{hub_id}", envelope)

    async def subscribe_output(self, bot_id: str, handler):  # pragma: no cover - simplistic fallback
        # Not supported; user should switch to NATS or local bus
        raise NotImplementedError("HTTP fallback output subscription is not supported")
