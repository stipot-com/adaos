from __future__ import annotations
from adaos.services.chat_io.interfaces import ChatSender, ChatOutputEvent, ChatOutputMessage
from adaos.services.agent_context import get_ctx
from adaos.services.io_bus.rate_limit import PerChatLimiter
from adaos.services.chat_io import telemetry as tm
from typing import Any
import asyncio
import httpx


class TelegramSender(ChatSender):
    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self._token = get_ctx().settings.tg_bot_token
        self._limiter = PerChatLimiter(rate_per_sec=1.0, capacity=30)

    async def send(self, out: ChatOutputEvent) -> None:
        # TODO: respect rate-limit, idempotency
        for m in out.messages:
            await self._send_one(out, m)

    async def _send_one(self, out: ChatOutputEvent, m: ChatOutputMessage) -> None:
        chat_id = out.target.get("chat_id")
        if not chat_id or not self._token:
            return
        # rate limit per chat
        if not self._limiter.allow(chat_id):
            await asyncio.sleep(0.5)
        if m.type == "text" and m.text:
            await self._call("sendMessage", {"chat_id": chat_id, "text": m.text})
            tm.record_event("outbound_total", {"type": "text"})
        elif m.type == "photo" and m.image_path:
            # simple caption within text if provided
            await self._call_multipart("sendPhoto", {"chat_id": chat_id}, file_field="photo", file_path=m.image_path)
            tm.record_event("outbound_total", {"type": "photo"})
        elif m.type == "voice" and m.audio_path:
            await self._call_multipart("sendVoice", {"chat_id": chat_id}, file_field="voice", file_path=m.audio_path)
            tm.record_event("outbound_total", {"type": "voice"})

    async def _call(self, method: str, payload: dict[str, Any]) -> None:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        await _with_retries_json(url, payload)

    async def _call_multipart(self, method: str, fields: dict[str, Any], *, file_field: str, file_path: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        files = {file_field: (file_path.split("/")[-1], open(file_path, "rb"), "application/octet-stream")}
        await _with_retries_multipart(url, fields, files)

async def _with_retries_json(url: str, payload: dict[str, Any], *, attempts: int = 3) -> None:
    backoff = 0.5
    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(attempts):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code in (200, 201, 202):
                    return
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
                return
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
    raise RuntimeError("telegram_http_failed")


async def _with_retries_multipart(url: str, fields: dict[str, Any], files: dict[str, Any], *, attempts: int = 3) -> None:
    backoff = 0.5
    async with httpx.AsyncClient(timeout=20.0) as client:
        for _ in range(attempts):
            try:
                resp = await client.post(url, data=fields, files=files)
                if resp.status_code in (200, 201, 202):
                    return
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
                return
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
    raise RuntimeError("telegram_http_failed")
