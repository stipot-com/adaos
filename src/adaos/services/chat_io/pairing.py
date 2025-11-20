# src\adaos\services\chat_io\pairing.py
from __future__ import annotations
from typing import Dict, Any, Optional
import time

from adaos.services.agent_context import get_ctx
from adaos.adapters.db import sqlite as sqlite_db


async def issue_pair_code(*, bot_id: str, hub_id: Optional[str], ttl_sec: int) -> Dict[str, Any]:
    rec = sqlite_db.pair_issue(bot_id, hub_id or None, ttl_sec=ttl_sec)
    code = rec["code"]
    # Deep-link best effort; require bot username in settings if provided
    ctx = get_ctx()
    bot_username = None  # could be provided via settings later
    deep_link = f"https://t.me/{bot_username}?start={code}" if bot_username else None
    return {"pair_code": code, "deep_link": deep_link, "qr_path": None, "expires_at": rec.get("expires_at")}


async def confirm_pair_code(*, code: str, platform_user: Dict[str, Any]) -> Dict[str, Any]:
    rec = sqlite_db.pair_confirm(code)
    if not rec:
        return {"ok": False, "error": "not_found"}
    if rec.get("state") == "expired":
        return {"ok": False, "error": "expired"}
    if rec.get("state") not in ("confirmed",):
        # already confirmed/revoked
        if rec.get("state") == "revoked":
            return {"ok": False, "error": "revoked"}
    # create binding
    platform = platform_user.get("platform") or "telegram"
    user_id = str(platform_user.get("user_id") or "")
    bot_id = str(platform_user.get("bot_id") or rec.get("bot_id") or "")
    b = sqlite_db.binding_upsert(platform, user_id, bot_id, hub_id=rec.get("hub_id"), ada_user_id=None)
    return {"ok": True, "hub_id": b.get("hub_id"), "ada_user_id": b.get("ada_user_id")}


async def pair_status(*, code: str) -> Dict[str, Any]:
    rec = sqlite_db.pair_get(code)
    if not rec:
        return {"state": "not_found"}
    now = int(time.time())
    ttl = (rec.get("expires_at") or 0) - now
    return {"state": rec.get("state"), "expires_in": max(ttl, 0)}


async def revoke_pair_code(*, code: str) -> Dict[str, Any]:
    ok = sqlite_db.pair_revoke(code)
    return {"ok": bool(ok)}
