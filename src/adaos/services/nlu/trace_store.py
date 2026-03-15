from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.trace")

_MAX_ITEMS = int(os.getenv("ADAOS_NLU_TRACE_MAX", "200") or "200")


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


async def _append_trace_item(webspace_id: str, item: dict) -> None:
    # Import lazily to avoid importing y_py at module import time in contexts
    # where YJS is not available.
    from adaos.services.yjs.doc import async_get_ydoc

    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        current = data_map.get("nlu_trace")
        items = []
        if isinstance(current, dict) and isinstance(current.get("items"), list):
            items = list(current.get("items") or [])
        items.append(item)
        if _MAX_ITEMS > 0 and len(items) > _MAX_ITEMS:
            items = items[-_MAX_ITEMS:]
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_trace", {"items": items})


def _compact_meta(meta: Mapping[str, Any] | None) -> dict:
    if not isinstance(meta, Mapping):
        return {}
    out: dict = {}
    for key in ("webspace_id", "device_id", "scenario_id", "route_id", "trace_id"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


@subscribe("nlp.intent.detect.request")
async def on_detect_request(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str):
        text = ""
    webspace_id = _resolve_webspace_id(payload)
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    rid = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    item = {
        "ts": time.time(),
        "type": "nlp.intent.detect.request",
        "text": text,
        "request_id": rid,
        "_meta": _compact_meta(meta),
    }
    try:
        await _append_trace_item(webspace_id, item)
    except Exception:
        _log.debug("failed to append nlu trace item (detect.request)", exc_info=True)


@subscribe("nlp.intent.detected")
async def on_detected(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str):
        text = ""
    webspace_id = _resolve_webspace_id(payload)
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    item = {
        "ts": time.time(),
        "type": "nlp.intent.detected",
        "text": text,
        "intent": payload.get("intent"),
        "confidence": payload.get("confidence"),
        "slots": payload.get("slots") if isinstance(payload.get("slots"), dict) else {},
        "via": payload.get("via"),
        "request_id": payload.get("request_id"),
        "_meta": _compact_meta(meta),
    }
    try:
        await _append_trace_item(webspace_id, item)
    except Exception:
        _log.debug("failed to append nlu trace item (detected)", exc_info=True)


@subscribe("nlp.intent.not_obtained")
async def on_not_obtained(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str):
        text = ""
    webspace_id = _resolve_webspace_id(payload)
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    item = {
        "ts": time.time(),
        "type": "nlp.intent.not_obtained",
        "text": text,
        "reason": payload.get("reason"),
        "via": payload.get("via"),
        "request_id": payload.get("request_id"),
        "_meta": _compact_meta(meta),
    }
    try:
        await _append_trace_item(webspace_id, item)
    except Exception:
        _log.debug("failed to append nlu trace item (not_obtained)", exc_info=True)

