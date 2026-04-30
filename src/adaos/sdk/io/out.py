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
from adaos.services.webspace_id import coerce_webspace_id

__all__ = ["chat_append", "say", "media_route", "stream_publish"]


def _publish(topic: str, payload: dict, *, source: str) -> None:
    ctx = get_ctx()
    bus = getattr(ctx, "bus", None)
    if bus is None:
        raise RuntimeError("AgentContext.bus is not initialized")
    _emit(bus, topic, payload, source)


def _normalize_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(meta)
    if "webspace_id" in normalized:
        normalized["webspace_id"] = coerce_webspace_id(normalized.get("webspace_id"), fallback="default")
    if "workspace_id" in normalized:
        normalized["workspace_id"] = coerce_webspace_id(normalized.get("workspace_id"), fallback="default")
    raw_ids = normalized.get("webspace_ids")
    if isinstance(raw_ids, (list, tuple)):
        out: list[str] = []
        for item in raw_ids:
            token = coerce_webspace_id(item, fallback="default")
            if token and token not in out:
                out.append(token)
        if out:
            normalized["webspace_ids"] = out
    return normalized


def _merged_meta(_meta: Mapping[str, Any] | None) -> dict[str, Any]:
    meta = get_current_meta()
    if _meta:
        meta.update(dict(_meta))
    return _normalize_meta(meta) if meta else meta


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
    meta = _merged_meta(_meta)
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
    meta = _merged_meta(_meta)
    if meta:
        payload["_meta"] = meta

    _publish("io.out.say", payload, source="sdk.io.out")
    return {"ok": True}


@tool(
    "io.out.media.route",
    summary="Publish a media route intent or normalized route contract for router-owned projection.",
    stability="experimental",
    examples=[
        "io.out.media.route(need='scenario_response_media', _meta={'webspace_id':'default'})",
        "io.out.media.route(route={'route_intent':'live_stream','active_route':'hub_webrtc_loopback'})",
    ],
)
def media_route(
    *,
    need: str | None = None,
    route: Mapping[str, Any] | None = None,
    producer_preference: str | None = None,
    preferred_member_id: str | None = None,
    direct_local_ready: bool | None = None,
    root_routed_ready: bool | None = None,
    hub_webrtc_ready: bool | None = None,
    member_browser_direct_possible: bool | None = None,
    member_browser_direct_admitted: bool | None = None,
    member_browser_direct_reason: str | None = None,
    candidate_member_total: int | None = None,
    browser_session_total: int | None = None,
    observed_failure: str | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, bool]:
    if not isinstance(route, Mapping) and (not isinstance(need, str) or not need.strip()):
        return {"ok": False}

    payload: dict[str, Any] = {
        "ts": time.time(),
    }
    if isinstance(need, str) and need.strip():
        payload["need"] = need.strip()
    if isinstance(route, Mapping):
        payload["route"] = dict(route)
    if isinstance(producer_preference, str) and producer_preference.strip():
        payload["producer_preference"] = producer_preference.strip()
    if isinstance(preferred_member_id, str) and preferred_member_id.strip():
        payload["preferred_member_id"] = preferred_member_id.strip()
    if direct_local_ready is not None:
        payload["direct_local_ready"] = bool(direct_local_ready)
    if root_routed_ready is not None:
        payload["root_routed_ready"] = bool(root_routed_ready)
    if hub_webrtc_ready is not None:
        payload["hub_webrtc_ready"] = bool(hub_webrtc_ready)
    member_browser_direct: dict[str, Any] = {}
    if member_browser_direct_possible is not None:
        member_browser_direct["possible"] = bool(member_browser_direct_possible)
    if member_browser_direct_admitted is not None:
        member_browser_direct["admitted"] = bool(member_browser_direct_admitted)
    if isinstance(member_browser_direct_reason, str) and member_browser_direct_reason.strip():
        member_browser_direct["reason"] = member_browser_direct_reason.strip()
    if isinstance(candidate_member_total, int):
        member_browser_direct["candidate_member_total"] = candidate_member_total
    if isinstance(browser_session_total, int):
        member_browser_direct["browser_session_total"] = browser_session_total
    if member_browser_direct:
        payload["member_browser_direct"] = member_browser_direct
    if isinstance(observed_failure, str) and observed_failure.strip():
        payload["observed_failure"] = observed_failure.strip()

    meta = _merged_meta(_meta)
    if meta:
        payload["_meta"] = meta

    _publish("io.out.media.route", payload, source="sdk.io.out")
    return {"ok": True}


@tool(
    "io.out.stream.publish",
    summary="Publish transport-independent browser stream data for a declarative webui receiver.",
    stability="experimental",
    examples=[
        "io.out.stream.publish('telemetry', {'value': 42}, _meta={'webspace_id':'default'})",
    ],
)
def stream_publish(
    receiver: str | None,
    data: Any = None,
    *,
    ts: float | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, bool]:
    receiver_id = str(receiver or "").strip()
    if not receiver_id:
        return {"ok": False}

    payload: dict[str, Any] = {
        "receiver": receiver_id,
        "data": data,
        "ts": float(ts) if ts is not None else time.time(),
    }
    meta = _merged_meta(_meta)
    if meta:
        payload["_meta"] = meta

    _publish("io.out.stream.publish", payload, source="sdk.io.out")
    return {"ok": True}
