# src\adaos\services\chat_io\router.py
from __future__ import annotations

"""Routing helpers for chat IO (placeholder).

- Resolve hub_id by binding or route rules (by locale), with a default.
"""

from typing import Optional, Dict, Any
import time
import yaml

from adaos.services.agent_context import get_ctx
from adaos.adapters.db import sqlite as sqlite_db


_CACHE: Dict[str, tuple[float, Optional[str]]] = {}
_CACHE_TTL = 300  # seconds


def _cache_get(user_id: str) -> Optional[str]:
    rec = _CACHE.get(user_id)
    if not rec:
        return None
    ts, val = rec
    if (time.time() - ts) > _CACHE_TTL:
        _CACHE.pop(user_id, None)
        return None
    return val


def _cache_set(user_id: str, hub_id: Optional[str]) -> None:
    _CACHE[user_id] = (time.time(), hub_id)


def resolve_hub_id(*, platform: str, user_id: str, bot_id: str, locale: Optional[str]) -> Optional[str]:
    cached = _cache_get(user_id)
    if cached is not None:
        return cached

    # 1) binding lookup
    b = sqlite_db.get_binding_by_user(platform, user_id, bot_id)
    if b and b.get("hub_id"):
        _cache_set(user_id, b["hub_id"])
        return b["hub_id"]

    # 2) route rules
    ctx = get_ctx()
    rules_path = ctx.settings.route_rules_path
    hub: Optional[str] = None
    if rules_path:
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = yaml.safe_load(f) or {}
            # try by locale key exact or prefix
            locales = (data.get("locales") or {}) if isinstance(data, dict) else {}
            if locale:
                if locale in locales:
                    hub = locales.get(locale)
                else:
                    # try short prefix (e.g., 'en' for 'en-US')
                    short = locale.split("-", 1)[0]
                    hub = locales.get(short)
            if not hub:
                hub = data.get("default_hub") or None
        except Exception:
            hub = None

    # 3) default_hub from settings
    if not hub:
        hub = ctx.settings.default_hub

    _cache_set(user_id, hub)
    return hub
