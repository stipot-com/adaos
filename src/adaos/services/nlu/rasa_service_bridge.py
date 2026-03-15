from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Mapping
from urllib.request import Request, urlopen

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.skill.service_supervisor import get_service_supervisor

_log = logging.getLogger("adaos.nlu.rasa")
_SEMAPHORE = asyncio.Semaphore(2)
_PARSE_TIMEOUT_S = float(os.getenv("ADAOS_NLU_RASA_PARSE_TIMEOUT_S", "8.0") or "8.0")
_START_LOCK = asyncio.Lock()
_ISSUE_WINDOW_S = float(os.getenv("ADAOS_NLU_RASA_ISSUE_WINDOW_S", "60") or "60")
_ISSUE_THRESHOLD = int(os.getenv("ADAOS_NLU_RASA_ISSUE_THRESHOLD", "3") or "3")
_MIN_CONFIDENCE = float(os.getenv("ADAOS_NLU_RASA_MIN_CONFIDENCE", "0.6") or "0.6")
_issue_times: dict[str, list[float]] = {}


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


def _emit_not_obtained(
    *,
    ctx: Any,
    text: str,
    webspace_id: str | None,
    request_id: str | None,
    meta: Mapping[str, Any],
    reason: str,
) -> None:
    out: Dict[str, Any] = {"reason": reason, "text": text, "via": "rasa"}
    if webspace_id:
        out["webspace_id"] = webspace_id
    if request_id:
        out["request_id"] = request_id
    if isinstance(meta, Mapping) and meta:
        out["_meta"] = dict(meta)
    bus_emit(ctx.bus, "nlp.intent.not_obtained", out, source="nlu.rasa")


def _http_post_json(url: str, payload: dict, *, timeout_ms: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)


def _record_failure(kind: str) -> int:
    now = asyncio.get_running_loop().time()
    times = _issue_times.get(kind) or []
    window_s = _ISSUE_WINDOW_S if _ISSUE_WINDOW_S > 0 else 60.0
    times = [t for t in times if now - t <= window_s]
    times.append(now)
    _issue_times[kind] = times
    return len(times)


async def _parse_and_emit(
    *,
    text: str,
    webspace_id: str | None,
    request_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> None:
    ctx = get_ctx()
    supervisor = get_service_supervisor()
    meta = meta if isinstance(meta, Mapping) else {}

    # Best-effort: ensure service is running (and venv exists) before calling /parse.
    try:
        async with _START_LOCK:
            await supervisor.start("rasa_nlu_service_skill")
    except KeyError:
        _log.debug("rasa service is not configured/installed")
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_not_installed")
        return
    except Exception:
        _log.warning("failed to start rasa_nlu_service_skill service", exc_info=True)
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_start_failed")
        return

    base_url = supervisor.resolve_base_url("rasa_nlu_service_skill")
    if not base_url:
        _log.debug("rasa service base_url unresolved")
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_base_url_unresolved")
        return

    loop = asyncio.get_running_loop()
    try:
        async with _SEMAPHORE:
            future = loop.run_in_executor(
                None,
                _http_post_json,
                f"{base_url}/parse",
                {"text": text},
                int(_PARSE_TIMEOUT_S * 1000),
            )
            data = await asyncio.wait_for(future, timeout=_PARSE_TIMEOUT_S)
    except TimeoutError:
        count = _record_failure("timeout")
        if count >= max(_ISSUE_THRESHOLD, 1):
            _log.warning("rasa service parse timed out (x%d) timeout_s=%.1f", count, _PARSE_TIMEOUT_S)
            try:
                await supervisor.inject_issue(
                    "rasa_nlu_service_skill",
                    issue_type="rasa_timeout",
                    message="rasa parse timed out",
                    details={"timeout_s": _PARSE_TIMEOUT_S, "text": text, "request_id": request_id, "count": count},
                )
            except Exception:
                pass
        else:
            _log.debug("rasa service parse timed out (x%d) text=%r", count, text)
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_timeout")
        return
    except Exception:
        count = _record_failure("failed")
        if count >= max(_ISSUE_THRESHOLD, 1):
            _log.warning("rasa service parse failed (x%d) text=%r", count, text, exc_info=True)
            try:
                await supervisor.inject_issue(
                    "rasa_nlu_service_skill",
                    issue_type="rasa_failed",
                    message="rasa parse failed",
                    details={"text": text, "request_id": request_id, "count": count},
                )
            except Exception:
                pass
        else:
            _log.debug("rasa service parse failed (x%d) text=%r", count, text, exc_info=True)
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_failed")
        return

    if not isinstance(data, dict) or not data.get("ok"):
        _log.debug("rasa parse returned not-ok: %r", data)
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_not_ok")
        return

    result = data.get("result") or {}
    if not isinstance(result, dict):
        _log.debug("rasa parse returned invalid result: %r", result)
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_invalid_result")
        return

    intent_block = result.get("intent") or {}
    intent_name = intent_block.get("name") if isinstance(intent_block, dict) else None
    confidence = intent_block.get("confidence") if isinstance(intent_block, dict) else None
    if not isinstance(intent_name, str) or not intent_name.strip():
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_no_intent")
        return
    if isinstance(confidence, (int, float)) and float(confidence) < _MIN_CONFIDENCE:
        _emit_not_obtained(ctx=ctx, text=text, webspace_id=webspace_id, request_id=request_id, meta=meta, reason="rasa_low_confidence")
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
        "_meta": dict(meta) if isinstance(meta, Mapping) else {},
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
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    asyncio.create_task(
        _parse_and_emit(text=text, webspace_id=webspace_id, request_id=request_id, meta=meta),
        name=f"adaos-nlu-rasa-parse:{request_id or 'noid'}",
    )
