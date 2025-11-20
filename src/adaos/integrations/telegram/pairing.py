from __future__ import annotations
from typing import Mapping, Any, Optional


def extract_start_code(update: Mapping[str, Any]) -> Optional[str]:
    """Return code from '/start <code>' message if present, else None."""
    msg = (update.get("message") or {}) if isinstance(update, dict) else {}
    text = (msg.get("text") or "").strip()
    if not text:
        return None
    if text.lower().startswith("/start "):
        return text.split(" ", 1)[1].strip() or None
    return None

