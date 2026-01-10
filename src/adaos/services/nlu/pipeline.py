from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.pipeline")

_RECENT_TTL_S = 60.0
_recent: dict[str, float] = {}

_WEATHER_KEYWORD_RE = re.compile(r"\b(?:погода|weather)\b", re.IGNORECASE | re.UNICODE)
_WEATHER_CITY_RU_RE = re.compile(
    r"\b(?:какая\s+)?погода\b(?:\s+(?:в|во)\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)
_WEATHER_CITY_EN_RE = re.compile(
    r"\bweather\b(?:\s+in\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _request_id(payload: Mapping[str, Any], *, text: str, webspace_id: str) -> str:
    rid = payload.get("request_id") or payload.get("id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    seed = f"{webspace_id}:{text}:{payload.get('ts') or ''}"
    return "auto." + hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _seen_recent(rid: str) -> bool:
    now = time.time()
    if len(_recent) > 512:
        cutoff = now - _RECENT_TTL_S
        for k, ts in list(_recent.items()):
            if ts < cutoff:
                _recent.pop(k, None)
    ts = _recent.get(rid)
    if ts is not None and now - ts < _RECENT_TTL_S:
        return True
    _recent[rid] = now
    return False


def _clean_city(city: str | None) -> str | None:
    if not isinstance(city, str):
        return None
    value = city.strip().strip(" \t\r\n'\"()[]{}")
    return value if value else None


def _try_regex_intent(text: str) -> tuple[str | None, dict]:
    """
    Very small, fast regex stage (MVP).

    Goal: quickly extract intent/slots for weather queries without calling
    external interpreters.
    """
    if not _WEATHER_KEYWORD_RE.search(text):
        return (None, {})

    city: str | None = None
    m_ru = _WEATHER_CITY_RU_RE.search(text)
    if m_ru:
        city = _clean_city(m_ru.group("city"))
    if city is None:
        m_en = _WEATHER_CITY_EN_RE.search(text)
        if m_en:
            city = _clean_city(m_en.group("city"))

    slots = {"city": city} if city else {}
    return ("desktop.open_weather", slots)


@subscribe("nlp.intent.detect.request")
async def _on_detect_request(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}

    ctx = get_ctx()
    webspace_id = _resolve_webspace_id(payload)
    rid = _request_id(payload, text=text, webspace_id=webspace_id)
    if _seen_recent(rid):
        return

    intent, slots = _try_regex_intent(text)
    if intent:
        bus_emit(
            ctx.bus,
            "nlp.intent.detected",
            {
                "intent": intent,
                "confidence": 1.0,
                "slots": slots,
                "text": text,
                "webspace_id": webspace_id,
                "request_id": rid,
                "via": "regex",
                "_meta": meta,
            },
            source="nlu.pipeline",
        )
        return

    bus_emit(
        ctx.bus,
        "nlp.intent.detect.rasa",
        {"text": text, "webspace_id": webspace_id, "request_id": rid, "_meta": meta},
        source="nlu.pipeline",
    )

