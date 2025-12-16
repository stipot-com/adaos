from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.io_web.toast")


@dataclass(slots=True)
class WebToast:
    level: str
    message: str
    code: Optional[str] = None
    source: Optional[str] = None
    ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "code": self.code,
            "source": self.source,
            "ts": self.ts or datetime.now(timezone.utc).isoformat(),
        }


class WebToastService:
    """
    Core helper for pushing transient toast notifications into Yjs.

    Toasts are stored under ``data/desktop/toasts`` as a bounded list
    so that multiple browsers attached to the same webspace can render
    them independently while keeping hub-side logic minimal.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    async def push(
        self,
        message: str,
        *,
        level: str = "info",
        code: Optional[str] = None,
        source: Optional[str] = None,
        webspace_id: Optional[str] = None,
        max_items: int = 20,
    ) -> None:
        webspace = (webspace_id or "").strip() or default_webspace_id()
        toast = WebToast(level=level, message=message, code=code, source=source)

        async with async_get_ydoc(webspace) as ydoc:
            data_map = ydoc.get_map("data")
            with ydoc.begin_transaction() as txn:
                desktop = data_map.get("desktop")
                if not isinstance(desktop, dict):
                    desktop = {}
                raw_toasts = desktop.get("toasts") or []
                items: List[Dict[str, Any]] = []
                if isinstance(raw_toasts, list):
                    items = [it for it in raw_toasts if isinstance(it, dict)]
                items.append(toast.to_dict())
                # Keep only the last max_items entries.
                if max_items > 0 and len(items) > max_items:
                    items = items[-max_items:]
                desktop["toasts"] = json.loads(json.dumps(items))
                data_map.set(txn, "desktop", json.loads(json.dumps(desktop)))

        _log.debug("toast pushed webspace=%s level=%s code=%s", webspace, level, code)
