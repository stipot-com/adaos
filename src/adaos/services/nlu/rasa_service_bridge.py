from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Mapping
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.skill.service_supervisor import get_service_supervisor

_log = logging.getLogger("adaos.nlu.rasa")
_SEMAPHORE = asyncio.Semaphore(2)


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str | None:
    meta = payload.get("_meta") or {}
    if isinstance(meta, Mapping):
        meta_ws = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(meta_ws, str) and meta_ws.strip():
            return meta_ws.strip()
    token = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _http_post_json(url: str, payload: dict, *, timeout_ms: int = 5000) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


async def _parse_and_emit(*, text: str, webspace_id: str | None, request_id: str | None = None) -> None:
    ctx = get_ctx()
    supervisor = get_service_supervisor()
    base_url = supervisor.resolve_base_url("rasa_nlu_service_skill")
    if not base_url:
        _log.debug("rasa service is not configured/installed")
        return

    loop = asyncio.get_running_loop()
    try:
        async with _SEMAPHORE:
            future = loop.run_in_executor(None, _http_post_json, f"{base_url}/parse", {"text": text})
            data = await asyncio.wait_for(future, timeout=2.0)
    except TimeoutError:
        _log.debug("rasa service parse timed out text=%r", text)
        return
    except Exception:
        _log.warning("rasa service parse failed text=%r", text, exc_info=True)
        return

    if not isinstance(data, dict) or not data.get("ok"):
        _log.debug("rasa parse returned not-ok: %r", data)
        return

    result = data.get("result") or {}
    if not isinstance(result, dict):
        _log.debug("rasa parse returned invalid result: %r", result)
        return

    intent_block = result.get("intent") or {}
    intent_name = intent_block.get("name") if isinstance(intent_block, dict) else None
    confidence = intent_block.get("confidence") if isinstance(intent_block, dict) else None
    if not isinstance(intent_name, str) or not intent_name.strip():
        return

    slots: Dict[str, Any] = {}
    entities = result.get("entities") or []
    if isinstance(entities, list):
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = ent.get("entity")
            value = ent.get("value")
            if isinstance(name, str) and name and value is not None:
                slots.setdefault(name, value)

    detected_payload: Dict[str, Any] = {
        "intent": intent_name,
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "slots": slots,
        "text": text,
        "_raw": result,
    }
    if webspace_id:
        detected_payload["webspace_id"] = webspace_id

    if request_id:
        detected_payload["request_id"] = request_id
    detected_payload["via"] = "rasa"
    bus_emit(ctx.bus, "nlp.intent.detected", detected_payload, source="nlu.rasa")


@subscribe("nlp.intent.detect.rasa")
async def _on_nlp_intent_detect(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    webspace_id = _resolve_webspace_id(payload)
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    asyncio.create_task(
        _parse_and_emit(text=text, webspace_id=webspace_id, request_id=request_id),
        name=f"adaos-nlu-rasa-parse:{request_id or 'noid'}",
    )
