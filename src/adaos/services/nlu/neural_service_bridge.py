from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
from typing import Any, Dict, Mapping
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.skill.service_supervisor import get_service_supervisor
from .neural_skill_installer import ensure_neural_service_skill_installed

_log = logging.getLogger("adaos.nlu.neural")

_SEMAPHORE = asyncio.Semaphore(2)
_START_LOCK = asyncio.Lock()
_PARSE_TIMEOUT_S = float(os.getenv("ADAOS_NLU_NEURAL_TIMEOUT_S", "6.0") or "6.0")
_MIN_CONFIDENCE = float(os.getenv("ADAOS_NLU_NEURAL_MIN_CONFIDENCE", "0.75") or "0.75")


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
        token = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(token, str) and token.strip():
            return token.strip()
    token = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _http_post_json(url: str, payload: dict, *, timeout_ms: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def _emit_rasa_fallback(*, text: str, webspace_id: str | None, request_id: str | None, meta: Mapping[str, Any]) -> None:
    ctx = get_ctx()
    payload: Dict[str, Any] = {"text": text}
    if webspace_id:
        payload["webspace_id"] = webspace_id
    if request_id:
        payload["request_id"] = request_id
    if isinstance(meta, Mapping) and meta:
        payload["_meta"] = dict(meta)
    bus_emit(ctx.bus, "nlp.intent.detect.rasa", payload, source="nlu.neural")


@subscribe("nlp.intent.detect.neural")
async def _on_nlp_intent_detect_neural(evt: Any) -> None:
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    webspace_id = _resolve_webspace_id(payload)
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    locale = payload.get("locale") if isinstance(payload.get("locale"), str) else None

    supervisor = get_service_supervisor()

    try:
        async with _START_LOCK:
            await supervisor.start("neural_nlu_service_skill")
    except KeyError:
        installed = ensure_neural_service_skill_installed()
        if installed is not None:
            try:
                await supervisor.refresh_discovered()
                async with _START_LOCK:
                    await supervisor.start("neural_nlu_service_skill")
            except Exception:
                _log.warning("failed to bootstrap/start neural_nlu_service_skill from template", exc_info=True)
                _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
                return
        else:
            _log.debug("neural service is not configured/installed; fallback to rasa")
            _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
            return
    except Exception:
        _log.warning("failed to start neural_nlu_service_skill", exc_info=True)
        _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
        return

    base_url = supervisor.resolve_base_url("neural_nlu_service_skill")
    if not base_url:
        _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
        return

    loop = asyncio.get_running_loop()
    try:
        req_payload: Dict[str, Any] = {"text": text}
        if webspace_id:
            req_payload["webspace_id"] = webspace_id
        if locale:
            req_payload["locale"] = locale

        async with _SEMAPHORE:
            future = loop.run_in_executor(
                None,
                functools.partial(
                    _http_post_json,
                    f"{base_url}/parse",
                    req_payload,
                    timeout_ms=int(_PARSE_TIMEOUT_S * 1000),
                ),
            )
            data = await asyncio.wait_for(future, timeout=_PARSE_TIMEOUT_S)
    except Exception:
        _log.debug("neural parse failed; fallback to rasa", exc_info=True)
        _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
        return

    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, Mapping):
        result = data if isinstance(data, Mapping) else {}

    top_intent = result.get("top_intent") or result.get("intent")
    confidence = result.get("confidence")

    if not isinstance(top_intent, str) or not top_intent.strip():
        _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
        return

    confidence_val = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    if confidence_val < _MIN_CONFIDENCE:
        _emit_rasa_fallback(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta)
        return

    slots_raw = result.get("slots")
    slots = dict(slots_raw) if isinstance(slots_raw, Mapping) else {}

    out: Dict[str, Any] = {
        "intent": top_intent.strip(),
        "confidence": confidence_val,
        "slots": slots,
        "text": text,
        "via": "neural",
        "_raw": dict(result),
    }
    if webspace_id:
        out["webspace_id"] = webspace_id
    if request_id:
        out["request_id"] = request_id
    if isinstance(meta, Mapping) and meta:
        out["_meta"] = dict(meta)

    ctx = get_ctx()
    bus_emit(ctx.bus, "nlp.intent.detected", out, source="nlu.neural")
