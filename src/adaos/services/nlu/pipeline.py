from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

from .ycoerce import coerce_dict, iter_mappings

_log = logging.getLogger("adaos.nlu.pipeline")

_RECENT_TTL_S = 60.0
_recent: dict[str, float] = {}

# NOTE: Keep patterns ASCII-safe by using explicit unicode escapes.
# "погода" = \u043f\u043e\u0433\u043e\u0434\u0430
# "какая"  = \u043a\u0430\u043a\u0430\u044f
# "в"      = \u0432
# "во"     = \u0432\u043e
_WEATHER_KEYWORD_RE = re.compile(r"\b(?:\u043f\u043e\u0433\u043e\u0434\u0430|weather)\b", re.IGNORECASE | re.UNICODE)
_WEATHER_CITY_RU_RE = re.compile(
    r"\b(?:\u043a\u0430\u043a\u0430\u044f\s+)?\u043f\u043e\u0433\u043e\u0434\u0430\b(?:\s+(?:\u0432|\u0432\u043e)\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)
_WEATHER_CITY_EN_RE = re.compile(
    r"\bweather\b(?:\s+in\s+(?P<city>[^?.!,;:]+))?",
    re.IGNORECASE | re.UNICODE,
)

_RULES_CACHE_TTL_S = 2.0
_rules_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_rules_lock = asyncio.Lock()


def describe_builtin_regex_rules() -> list[dict[str, Any]]:
    """
    A compact description of built-in regex rules used by the pipeline.

    Intended for observability / UI / LLM teacher context.
    """
    return [
        {
            "id": "builtin.weather.keyword",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_KEYWORD_RE.pattern,
            "notes": "Keyword gate for the built-in weather rule.",
        },
        {
            "id": "builtin.weather.ru",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_CITY_RU_RE.pattern,
            "notes": "RU weather queries, optional city captured as (?P<city>...).",
        },
        {
            "id": "builtin.weather.en",
            "intent": "desktop.open_weather",
            "pattern": _WEATHER_CITY_EN_RE.pattern,
            "notes": "EN weather queries, optional city captured as (?P<city>...).",
        },
    ]


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


def _clean_slots(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if not isinstance(k, str) or not k:
            continue
        if not isinstance(v, str):
            continue
        cleaned = v.strip().strip(" \t\r\n'\"()[]{}")
        if cleaned:
            out[k] = cleaned
    return out


async def _load_dynamic_regex_rules(webspace_id: str) -> list[dict[str, Any]]:
    """
    Load compiled regex rules from YJS for the given webspace.

    Storage: data.nlu.regex_rules = [{ id, intent, pattern, enabled, ... }]
    """
    now = time.time()
    cached = _rules_cache.get(webspace_id)
    if cached and now - cached[0] < _RULES_CACHE_TTL_S:
        return cached[1]

    async with _rules_lock:
        cached = _rules_cache.get(webspace_id)
        if cached and now - cached[0] < _RULES_CACHE_TTL_S:
            return cached[1]

        compiled: list[dict[str, Any]] = []
        try:
            async with async_get_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                nlu_obj = data_map.get("nlu")
                nlu_obj = coerce_dict(nlu_obj)
                rules = list(iter_mappings(nlu_obj.get("regex_rules")))
        except Exception:
            rules = []

        for item in rules:
            if not item.get("enabled", True):
                continue
            intent = item.get("intent")
            pattern = item.get("pattern")
            if not isinstance(intent, str) or not intent.strip():
                continue
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
            except re.error:
                continue
            compiled.append({"id": item.get("id"), "intent": intent.strip(), "pattern": pattern, "rx": rx})

        _rules_cache[webspace_id] = (now, compiled)
        return compiled


async def _try_regex_intent(text: str, *, webspace_id: str) -> tuple[str | None, dict, str, dict]:
    """
    Very small, fast regex stage (MVP).

    Goal: quickly extract intent/slots for weather queries without calling
    external interpreters.
    """
    # 1) Dynamic rules (LLM/teacher-applied) take precedence.
    for rule in await _load_dynamic_regex_rules(webspace_id):
        rx = rule.get("rx")
        if not isinstance(rx, re.Pattern):
            continue
        m = rx.search(text)
        if not m:
            continue
        intent = rule.get("intent")
        if not isinstance(intent, str) or not intent:
            continue
        slots = _clean_slots(m.groupdict())
        raw = {"rule_id": rule.get("id"), "pattern": rule.get("pattern"), "slots": slots}
        return (intent, slots, "regex.dynamic", raw)

    # 2) Built-in fallback (desktop weather MVP)
    if not _WEATHER_KEYWORD_RE.search(text):
        return (None, {}, "regex", {})

    city: str | None = None
    m_ru = _WEATHER_CITY_RU_RE.search(text)
    if m_ru:
        city = _clean_city(m_ru.group("city"))
    if city is None:
        m_en = _WEATHER_CITY_EN_RE.search(text)
        if m_en:
            city = _clean_city(m_en.group("city"))

    slots = {"city": city} if city else {}
    return ("desktop.open_weather", slots, "regex", {"builtin": "weather"})


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

    intent, slots, via, raw = await _try_regex_intent(text, webspace_id=webspace_id)
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
                "via": via,
                "_raw": raw,
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
