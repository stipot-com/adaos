from __future__ import annotations
import hashlib


def outbound_msg_hash(chat_id: str, message: str) -> str:
    key = f"{chat_id}|{message}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()

