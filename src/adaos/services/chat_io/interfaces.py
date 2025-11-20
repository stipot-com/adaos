# src\adaos\services\chat_io\interfaces.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Optional, Dict, Any, List, Mapping


# ---- Input (root -> hubs) ----
@dataclass(slots=True)
class ChatInputEvent:
    type: str  # "text|audio|photo|document|action|unknown"
    source: str  # "telegram" | "slack" | ...
    bot_id: str
    hub_id: Optional[str]  # may be None until router resolves
    chat_id: str
    user_id: str
    update_id: str
    payload: Dict[str, Any]  # {text|audio_path|photo_path|document_path|action|meta}


# ---- Output (hubs -> root -> platform) ----
@dataclass(slots=True)
class ChatOutputMessage:
    type: str  # "text|voice|photo"
    text: Optional[str] = None
    audio_path: Optional[str] = None
    image_path: Optional[str] = None
    keyboard: Optional[Dict[str, Any]] = None


@dataclass(slots=True)
class ChatOutputEvent:
    target: Dict[str, str]  # {"bot_id","hub_id","chat_id"}
    messages: List[ChatOutputMessage]
    options: Dict[str, Any] | None = None  # {"replace_last": bool, "reply_to": int}


# ---- Protocols (универсальные) ----
class ChatAdapter(Protocol):
    def validate_request(self, headers: Mapping[str, str]) -> bool: ...
    def parse_webhook(self, raw_update: Mapping[str, Any], bot_id: str) -> Optional[ChatInputEvent]: ...


class ChatSender(Protocol):
    async def send(self, out: ChatOutputEvent) -> None: ...


class PairingProvider(Protocol):
    async def issue(self, *, bot_id: str, hub_id: Optional[str], ttl_sec: int) -> Dict[str, Any]: ...
    async def confirm(self, *, code: str, platform_user: Dict[str, Any]) -> Dict[str, Any]: ...
    async def status(self, *, code: str) -> Dict[str, Any]: ...
    async def revoke(self, *, code: str) -> Dict[str, Any]: ...
