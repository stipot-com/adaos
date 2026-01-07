"""Unified IO output helpers for web/native frontends.

These helpers do not write to Yjs directly. They only publish events onto the
local bus. The RouterService is responsible for projecting them into concrete
outputs (chat history, TTS queues, etc.) based on `_meta`.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.io.context import get_current_meta
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as _emit

__all__ = ["chat_append", "say"]


def _publish(topic: str, payload: dict, *, source: str) -> None:
    ctx = get_ctx()
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise RuntimeError("AgentContext.bus is not initialized")
    _emit(bus, topic, payload, source)


@tool(
    "io.out.chat.append",
    summary="Append a chat message (router decides where it renders).",
    stability="experimental",
    examples=[
        "io.out.chat.append('Hello', from_='user', _meta={'webspace_id':'default'})",
        "io.out.chat.append('Hi!', from_='hub')",
    ],
)
def chat_append(
    text: str | None,
    *,
    from_: str = "hub",
    msg_id: str | None = None,
    ts: float | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, bool]:
    if not isinstance(text, str) or not text.strip():
        return {"ok": False}

    payload: dict[str, Any] = {
        "text": text.strip(),
        "from": str(from_ or "hub"),
        "id": str(msg_id) if msg_id else "",
        "ts": float(ts) if ts is not None else time.time(),
    }
    meta = get_current_meta()
    if _meta:
        meta.update(dict(_meta))
    if meta:
        payload["_meta"] = meta
    _publish("io.out.chat.append", payload, source="sdk.io.out")
    return {"ok": True}


@tool(
    "io.out.say",
    summary="Enqueue a TTS message (router decides which devices/webspaces play it).",
    stability="experimental",
    examples=[
        "io.out.say('Weather is sunny', lang='en-US', _meta={'webspace_id':'default'})",
    ],
)
def say(
    text: str | None,
    *,
    lang: str | None = None,
    voice: str | None = None,
    rate: float | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, bool]:
    if not isinstance(text, str) or not text.strip():
        return {"ok": False}

    payload: dict[str, Any] = {
        "text": text.strip(),
        "ts": time.time(),
    }
    if isinstance(lang, str) and lang.strip():
        payload["lang"] = lang.strip()
    if isinstance(voice, str) and voice.strip():
        payload["voice"] = voice.strip()
    if isinstance(rate, (int, float)):
        payload["rate"] = float(rate)
    meta = get_current_meta()
    if _meta:
        meta.update(dict(_meta))
    if meta:
        payload["_meta"] = meta

    _publish("io.out.say", payload, source="sdk.io.out")
    return {"ok": True}
