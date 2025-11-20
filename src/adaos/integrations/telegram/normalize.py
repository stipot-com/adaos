from __future__ import annotations
from typing import Mapping, Any, Optional, Dict
from adaos.services.chat_io.interfaces import ChatInputEvent


def to_input_event(bot_id: str, update: Mapping[str, Any], hub_id: Optional[str] = None) -> ChatInputEvent:
    upd_id = str(update.get("update_id", ""))

    # message / edited_message / callback_query
    msg = update.get("message") or update.get("edited_message") or {}
    cb = update.get("callback_query") or {}
    if cb:
        msg = cb.get("message") or {}

    frm = (msg.get("from") or cb.get("from") or {}) or {}
    chat = (msg.get("chat") or {}) or {}
    user_id = str(frm.get("id") or "")
    chat_id = str(chat.get("id") or (cb.get("message") or {}).get("chat", {}).get("id") or "")

    # text
    if msg.get("text"):
        payload: Dict[str, Any] = {
            "text": msg["text"],
            "meta": {"msg_id": msg.get("message_id"), "lang": frm.get("language_code")},
        }
        return ChatInputEvent(
            type="text",
            source="telegram",
            bot_id=bot_id,
            hub_id=hub_id,
            chat_id=chat_id,
            user_id=user_id,
            update_id=upd_id,
            payload=payload,
        )

    # callback_query
    if cb:
        payload = {
            "action": {"id": cb.get("data")},
            "meta": {"msg_id": (cb.get("message") or {}).get("message_id")},
        }
        return ChatInputEvent(
            type="action",
            source="telegram",
            bot_id=bot_id,
            hub_id=hub_id,
            chat_id=chat_id,
            user_id=user_id,
            update_id=upd_id,
            payload=payload,
        )

    # voice (путь заполним после скачивания)
    if msg.get("voice"):
        v = msg["voice"]
        payload = {
            "meta": {"msg_id": msg.get("message_id"), "mime": "audio/ogg", "duration": v.get("duration")},
            "file_id": v.get("file_id"),
        }
        return ChatInputEvent(
            type="audio",
            source="telegram",
            bot_id=bot_id,
            hub_id=hub_id,
            chat_id=chat_id,
            user_id=user_id,
            update_id=upd_id,
            payload=payload,
        )

    # photo
    if msg.get("photo"):
        sizes = msg["photo"]
        file_id = sizes[-1].get("file_id") if sizes else None
        payload = {"file_id": file_id, "meta": {"msg_id": msg.get("message_id")}}
        return ChatInputEvent(
            type="photo",
            source="telegram",
            bot_id=bot_id,
            hub_id=hub_id,
            chat_id=chat_id,
            user_id=user_id,
            update_id=upd_id,
            payload=payload,
        )

    # document
    if msg.get("document"):
        d = msg["document"]
        payload = {"file_id": d.get("file_id"), "meta": {"msg_id": msg.get("message_id")}}
        return ChatInputEvent(
            type="document",
            source="telegram",
            bot_id=bot_id,
            hub_id=hub_id,
            chat_id=chat_id,
            user_id=user_id,
            update_id=upd_id,
            payload=payload,
        )

    # fallback
    return ChatInputEvent(
        type="unknown",
        source="telegram",
        bot_id=bot_id,
        hub_id=hub_id,
        chat_id=chat_id,
        user_id=user_id,
        update_id=upd_id,
        payload={"meta": {"raw_kind": "unknown"}},
    )
