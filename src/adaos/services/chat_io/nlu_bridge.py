from __future__ import annotations

"""
Chat IO -> NLU bridge.

This helper subscribes to generic io.input envelopes (e.g. from Telegram)
and, for text messages, publishes ``nlp.intent.detect.request`` so that the
interpreter runtime (Rasa) can resolve intents and the NLU dispatcher can
map them to scenario/skill actions.
"""

import logging
import os
from typing import Any, Dict, Mapping, Optional, Tuple

from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import LocalEventBus
from adaos.domain import Event

_log = logging.getLogger("adaos.chat_io.nlu_bridge")

def _extract_text_io_input(env: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """
    Extract (text, webspace_id, meta) from a tg.input/io.input envelope.
    For now we map Telegram chats to the default webspace; this can be
    extended later via bindings.
    """
    payload = env.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None, None, {}
    if (payload.get("type") or "").strip() != "text":
        return None, None, {}
    inner = payload.get("payload") or {}
    if not isinstance(inner, Mapping):
        return None, None, {}
    text = inner.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return None, None, {}

    # Preserve chat routing context so responses can be sent back to the same chat.
    # Envelope schema (from Root/Telegram): payload has bot_id/chat_id/user_id/hub_id,
    # plus optional meta/route blocks.
    meta: Dict[str, Any] = {}
    try:
        source = payload.get("source")
        if isinstance(source, str) and source:
            meta["io_type"] = source
        bot_id = payload.get("bot_id")
        if isinstance(bot_id, str) and bot_id:
            meta["bot_id"] = bot_id
        hub_id = payload.get("hub_id")
        if isinstance(hub_id, str) and hub_id:
            meta["hub_id"] = hub_id
        chat_id = payload.get("chat_id")
        if isinstance(chat_id, str) and chat_id:
            meta["chat_id"] = chat_id
        user_id = payload.get("user_id")
        if isinstance(user_id, str) and user_id:
            meta["user_id"] = user_id
        update_id = payload.get("update_id")
        if isinstance(update_id, str) and update_id:
            meta["update_id"] = update_id

        # Prefer replying to the same IO route when possible.
        if meta.get("io_type") == "telegram" and meta.get("chat_id"):
            meta["route_id"] = "telegram"

        # Trace id / dedup info for correlation.
        env_meta = env.get("meta")
        if isinstance(env_meta, Mapping):
            trace_id = env_meta.get("trace_id")
            if isinstance(trace_id, str) and trace_id:
                meta["trace_id"] = trace_id
        dedup_key = env.get("dedup_key")
        if isinstance(dedup_key, str) and dedup_key:
            meta["dedup_key"] = dedup_key
        msg_meta = inner.get("meta")
        if isinstance(msg_meta, Mapping):
            msg_id = msg_meta.get("msg_id")
            if isinstance(msg_id, (int, str)) and str(msg_id):
                meta["reply_to"] = int(msg_id)
    except Exception:
        meta = {}

    # For MVP, Telegram text is routed to default webspace; later we can
    # map chats to specific workspaces.
    webspace_id = None
    return text.strip(), webspace_id, meta


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
            text, webspace_id, meta = _extract_text_io_input(env)
            if not text:
                return
            try:
                if os.getenv("HUB_TG_DEBUG", "0") == "1" and isinstance(meta, dict) and meta.get("io_type") == "telegram":
                    _log.info(
                        "tg.input received hub_id=%s chat_id=%s text=%r",
                        meta.get("hub_id"),
                        meta.get("chat_id"),
                        text[:200],
                    )
            except Exception:
                pass
            payload: Dict[str, Any] = {"text": text}
            if webspace_id:
                payload["webspace_id"] = webspace_id
            if meta:
                payload["_meta"] = meta
            bus.publish(
                Event(
                    type="nlp.intent.detect.request",
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
