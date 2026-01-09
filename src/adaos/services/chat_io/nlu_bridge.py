from __future__ import annotations

"""
Chat IO -> NLU bridge.

This helper subscribes to generic io.input envelopes (e.g. from Telegram)
and, for text messages, publishes ``nlp.intent.detect`` so that the
interpreter runtime (Rasa) can resolve intents and the NLU dispatcher can
map them to scenario/skill actions.
"""

from typing import Any, Dict, Mapping

from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import LocalEventBus
from adaos.domain import Event


def _extract_text_io_input(env: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """
    Extract (text, webspace_id) from a tg.input/io.input envelope.
    For now we map Telegram chats to the default webspace; this can be
    extended later via bindings.
    """
    payload = env.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None, None
    if (payload.get("type") or "").strip() != "text":
        return None, None
    inner = payload.get("payload") or {}
    if not isinstance(inner, Mapping):
        return None, None
    text = inner.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return None, None
    # For MVP, Telegram text is routed to default webspace; later we can
    # map chats to specific workspaces.
    webspace_id = None
    return text.strip(), webspace_id


def register_chat_nlu_bridge(bus: LocalEventBus | None = None) -> None:
    """
    Attach a handler to io.input.* subjects on the local event bus and
    dispatch NLU detection commands for text messages.
    """
    ctx = get_ctx()
    bus = bus or ctx.bus

    def _on_io_input(evt: Event) -> None:
        try:
            env = evt.payload or {}
            if not isinstance(env, Mapping):
                return
            text, webspace_id = _extract_text_io_input(env)
            if not text:
                return
            payload: Dict[str, Any] = {"text": text}
            if webspace_id:
                payload["webspace_id"] = webspace_id
            bus.publish(
                Event(
                    type="nlp.intent.detect",
                    source="chat_io",
                    ts=evt.ts,
                    payload=payload,
                )
            )
        except Exception:
            # Best-effort bridge; do not crash on malformed envelopes.
            return

    # Subscribe to the tg.input.<hub_id> subject emitted by bootstrap.
    bus.subscribe(f"tg.input.{ctx.config.subnet_id}", _on_io_input)

