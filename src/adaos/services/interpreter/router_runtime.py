from __future__ import annotations

"""
Event-driven bridge between AdaOS text events and the interpreter runtime.

Listens for ``nlp.intent.detect`` commands carrying raw user text and emits
``nlp.intent.detected`` events that are then consumed by the NLU dispatcher
(``adaos.services.nlu.dispatcher``) and mapped to scenario/skill actions.
"""

from typing import Any, Dict, Mapping
import asyncio
import logging

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.interpreter.workspace import InterpreterWorkspace
from adaos.services.interpreter.runtime import RasaNLURuntime

_log = logging.getLogger("adaos.interpreter.router")


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


@subscribe("nlp.intent.detect")
async def _on_nlp_intent_detect(evt: Any) -> None:
    """
    Handle generic "detect intent" commands and run them through the
    Rasa-based interpreter model.

    Expected payload shape:
      - text / utterance: user text
      - webspace_id / workspace_id / _meta.webspace_id: optional routing hint
    """
    payload = _payload(evt)
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return
    text = text.strip()

    ctx = get_ctx()
    ws = InterpreterWorkspace(ctx)
    runtime = RasaNLURuntime(ws)

    # Run heavy Rasa parsing in a worker thread to avoid blocking the
    # main event loop (which also serves YJS websockets / HTTP).
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, runtime.parse, text)
    except Exception:
        _log.warning("nlp.intent.detect failed text=%r", text, exc_info=True)
        return

    intent_block = result.get("intent") or {}
    intent_name = intent_block.get("name") if isinstance(intent_block, dict) else None
    confidence = intent_block.get("confidence") if isinstance(intent_block, dict) else None

    if not isinstance(intent_name, str) or not intent_name.strip():
        # No confident intent – for MVP log and ignore; later emit nlp.intent.unknown.
        _log.debug("nlp.intent.detect: no intent for text=%r result=%r", text, result)
        return

    # Simple slots extraction from entities: name -> value
    slots: Dict[str, Any] = {}
    entities = result.get("entities") or []
    if isinstance(entities, list):
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = ent.get("entity")
            value = ent.get("value")
            if isinstance(name, str) and name and value is not None:
                # First value wins; if needed we can switch to lists later.
                slots.setdefault(name, value)

    webspace_id = _resolve_webspace_id(payload)

    _log.debug(
        "nlp.intent.detected intent=%s confidence=%s webspace=%s slots=%s text=%r",
        intent_name,
        confidence,
        webspace_id,
        slots,
        text,
    )

    detected_payload: Dict[str, Any] = {
        "intent": intent_name,
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "slots": slots,
        "text": text,
    }
    if webspace_id:
        detected_payload["webspace_id"] = webspace_id

    # Optionally attach raw Rasa result for debugging / future use.
    detected_payload["_raw"] = result

    bus_emit(ctx.bus, "nlp.intent.detected", detected_payload, source="interpreter.router")
