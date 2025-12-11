from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import async_get_ydoc, get_ydoc, mutate_live_room
from adaos.apps.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.io_web.desktop")


@dataclass(slots=True)
class WebDesktopInstalled:
    apps: List[str]
    widgets: List[str]

    def to_dict(self) -> Dict[str, List[str]]:
        return {"apps": list(self.apps), "widgets": list(self.widgets)}


class WebDesktopService:
    """
    High-level helper for manipulating desktop state in a webspace.

    This service hides direct YDoc operations behind a stable API that can
    be used both by SDK helpers and by skill handlers. Callers should pass
    logical identifiers (webspace_id, app/widget ids) without touching Yjs.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    def _resolve_webspace(self, webspace_id: Optional[str]) -> str:
        token = (webspace_id or "").strip()
        return token or default_webspace_id()

    @staticmethod
    def _apply_install_toggle(
        webspace_id: str,
        ydoc: Any,
        txn: Any,
        item_type: str,
        target_id: str,
    ) -> None:
        data_map = ydoc.get_map("data")
        installed = data_map.get("installed") or {}
        if not isinstance(installed, dict):
            installed = {}
        apps = set(installed.get("apps") or [])
        widgets = set(installed.get("widgets") or [])
        if item_type == "app":
            if target_id in apps:
                apps.remove(target_id)
            else:
                apps.add(target_id)
        else:
            if target_id in widgets:
                widgets.remove(target_id)
            else:
                widgets.add(target_id)
        next_installed = {"apps": list(apps), "widgets": list(widgets)}
        data_map.set(txn, "installed", next_installed)
        desktop_value = data_map.get("desktop") or {}
        if not isinstance(desktop_value, dict):
            desktop_value = {}
        desktop_next = dict(desktop_value)
        desktop_installed = dict(desktop_next.get("installed") or {})
        desktop_installed["apps"] = list(apps)
        desktop_installed["widgets"] = list(widgets)
        desktop_next["installed"] = desktop_installed
        data_map.set(txn, "desktop", desktop_next)
        _log.debug(
            "toggle install webspace=%s type=%s target=%s apps=%s widgets=%s",
            webspace_id,
            item_type,
            target_id,
            sorted(apps),
            sorted(widgets),
        )

    def toggle_install(self, item_type: str, target_id: str, webspace_id: Optional[str] = None) -> None:
        """
        Toggle installation of an app or widget for a given webspace.

        This method performs a synchronous YDoc mutation and is suitable for
        callers that do not operate in an async context.
        """
        webspace = self._resolve_webspace(webspace_id)
        with get_ydoc(webspace) as ydoc:
            with ydoc.begin_transaction() as txn:
                self._apply_install_toggle(webspace, ydoc, txn, item_type, target_id)

    async def toggle_install_async(self, item_type: str, target_id: str, webspace_id: Optional[str] = None) -> None:
        """
        Async variant of toggle_install that uses async_get_ydoc and can be
        awaited from async runtimes.
        """
        webspace = self._resolve_webspace(webspace_id)
        async with async_get_ydoc(webspace) as ydoc:
            with ydoc.begin_transaction() as txn:
                self._apply_install_toggle(webspace, ydoc, txn, item_type, target_id)

    def toggle_install_with_live_room(
        self,
        item_type: str,
        target_id: str,
        webspace_id: Optional[str] = None,
    ) -> None:
        """
        Toggle installation while also attempting to mutate an in-memory YDoc
        room if one is currently attached for the target webspace.

        This ensures that all connected browsers see an immediate optimistic
        update, even if the on-disk YStore write happens asynchronously.
        """
        webspace = self._resolve_webspace(webspace_id)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_install_toggle(webspace, doc, txn, item_type, target_id)

        live_applied = mutate_live_room(webspace, _mutator)
        if not live_applied:
            _log.debug(
                "mutate_live_room skipped for toggle webspace=%s type=%s target=%s",
                webspace,
                item_type,
                target_id,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.toggle_install_async(item_type, target_id, webspace))
        else:
            loop.create_task(
                self.toggle_install_async(item_type, target_id, webspace),
                name=f"web-desktop-toggle-{webspace}",
            )

