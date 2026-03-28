from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import async_get_ydoc, get_ydoc, mutate_live_room
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspaces import index as workspace_index

_log = logging.getLogger("adaos.io_web.desktop")


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """
    Best-effort conversion of YJS map-like values to a plain dict.

    y_py map objects are not guaranteed to implement `collections.abc.Mapping`
    but typically expose `.items()`. Treating them as non-mapping silently
    drops state (e.g. installed apps/widgets) during scenario switches.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return {str(k): v for k, v in items()}
        except Exception:
            return {}
    return {}


def _iter_ids(value: Any) -> List[str]:
    """
    Extract string ids from list-like values (including YArray-like iterables).
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        return []
    if not isinstance(value, Iterable):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, (str, int)):
            out.append(str(item))
    return out


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
    def _next_installed_state(
        installed: WebDesktopInstalled,
        item_type: str,
        target_id: str,
    ) -> WebDesktopInstalled:
        apps = set(installed.apps)
        widgets = set(installed.widgets)
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
        return WebDesktopInstalled(apps=sorted(apps), widgets=sorted(widgets))

    @staticmethod
    def _apply_installed_state(ydoc: Any, txn: Any, installed: WebDesktopInstalled) -> None:
        data_map = ydoc.get_map("data")
        next_installed = {
            "apps": list(dict.fromkeys(installed.apps)),
            "widgets": list(dict.fromkeys(installed.widgets)),
        }
        data_map.set(txn, "installed", next_installed)
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_installed_raw = desktop_next.get("installed") or {}
        desktop_installed = _coerce_dict(desktop_installed_raw)
        desktop_installed["apps"] = next_installed["apps"]
        desktop_installed["widgets"] = next_installed["widgets"]
        desktop_next["installed"] = desktop_installed
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _read_overlay_installed(webspace_id: str) -> tuple[WebDesktopInstalled, bool]:
        if not workspace_index.has_workspace_overlay(webspace_id):
            return WebDesktopInstalled(apps=[], widgets=[]), False
        installed = workspace_index.get_workspace_installed_overlay(webspace_id)
        return (
            WebDesktopInstalled(
                apps=_iter_ids(installed.get("apps")),
                widgets=_iter_ids(installed.get("widgets")),
            ),
            True,
        )

    @staticmethod
    def _persist_overlay_installed(webspace_id: str, installed: WebDesktopInstalled) -> None:
        workspace_index.set_workspace_installed_overlay(webspace_id, installed.to_dict())

    @staticmethod
    def _read_installed(data_map: Any) -> WebDesktopInstalled:
        """
        Helper to normalise the installed structure from YDoc into a
        WebDesktopInstalled instance.
        """
        raw = data_map.get("installed") or {}
        raw_dict = _coerce_dict(raw)
        apps = _iter_ids(raw_dict.get("apps"))
        widgets = _iter_ids(raw_dict.get("widgets"))
        return WebDesktopInstalled(apps=apps, widgets=widgets)

    @staticmethod
    def _apply_install_toggle(
        webspace_id: str,
        ydoc: Any,
        txn: Any,
        item_type: str,
        target_id: str,
    ) -> None:
        data_map = ydoc.get_map("data")
        installed_raw = data_map.get("installed") or {}
        installed = WebDesktopInstalled(
            apps=_iter_ids(_coerce_dict(installed_raw).get("apps")),
            widgets=_iter_ids(_coerce_dict(installed_raw).get("widgets")),
        )
        next_installed = WebDesktopService._next_installed_state(installed, item_type, target_id)
        WebDesktopService._apply_installed_state(ydoc, txn, next_installed)
        _log.debug(
            "toggle install webspace=%s type=%s target=%s apps=%s widgets=%s",
            webspace_id,
            item_type,
            target_id,
            next_installed.apps,
            next_installed.widgets,
        )

    def get_installed(self, webspace_id: Optional[str] = None) -> WebDesktopInstalled:
        """
        Read the current set of installed apps/widgets for a webspace.

        This is a read-only helper; callers that need to change the
        installed set should use ``toggle_*`` or ``set_installed*``.
        """
        webspace = self._resolve_webspace(webspace_id)
        overlay_installed, has_overlay = self._read_overlay_installed(webspace)
        if has_overlay:
            return overlay_installed
        with get_ydoc(webspace) as ydoc:
            data_map = ydoc.get_map("data")
            installed = self._read_installed(data_map)
        if installed.apps or installed.widgets:
            self._persist_overlay_installed(webspace, installed)
        return installed

    async def get_installed_async(self, webspace_id: Optional[str] = None) -> WebDesktopInstalled:
        """
        Async variant of ``get_installed`` for use from async runtimes.

        This avoids calling the synchronous get_ydoc() helper from inside an
        active event loop and uses async_get_ydoc() instead.
        """
        webspace = self._resolve_webspace(webspace_id)
        overlay_installed, has_overlay = self._read_overlay_installed(webspace)
        if has_overlay:
            return overlay_installed
        async with async_get_ydoc(webspace) as ydoc:
            data_map = ydoc.get_map("data")
            installed = self._read_installed(data_map)
        if installed.apps or installed.widgets:
            self._persist_overlay_installed(webspace, installed)
        return installed

    def set_installed(self, installed: WebDesktopInstalled, webspace_id: Optional[str] = None) -> None:
        """
        Replace the installed apps/widgets set for a webspace.

        This is primarily intended for restoring state after a YJS
        reload; typical UI interactions should continue to use
        ``toggle_install``.
        """
        webspace = self._resolve_webspace(webspace_id)
        apps = list(dict.fromkeys(installed.apps))
        widgets = list(dict.fromkeys(installed.widgets))
        self._persist_overlay_installed(webspace, WebDesktopInstalled(apps=apps, widgets=widgets))
        with get_ydoc(webspace) as ydoc:
            with ydoc.begin_transaction() as txn:
                self._apply_installed_state(ydoc, txn, WebDesktopInstalled(apps=apps, widgets=widgets))
        _log.debug(
            "set installed webspace=%s apps=%s widgets=%s",
            webspace,
            apps,
            widgets,
        )

    async def set_installed_async(self, installed: WebDesktopInstalled, webspace_id: Optional[str] = None) -> None:
        """
        Async variant of ``set_installed`` for use from async runtimes.
        """
        webspace = self._resolve_webspace(webspace_id)
        apps = list(dict.fromkeys(installed.apps))
        widgets = list(dict.fromkeys(installed.widgets))
        self._persist_overlay_installed(webspace, WebDesktopInstalled(apps=apps, widgets=widgets))
        async with async_get_ydoc(webspace) as ydoc:
            with ydoc.begin_transaction() as txn:
                self._apply_installed_state(ydoc, txn, WebDesktopInstalled(apps=apps, widgets=widgets))
        _log.debug(
            "set installed (async) webspace=%s apps=%s widgets=%s",
            webspace,
            apps,
            widgets,
        )

    def toggle_install(self, item_type: str, target_id: str, webspace_id: Optional[str] = None) -> None:
        """
        Toggle installation of an app or widget for a given webspace.

        This method performs a synchronous YDoc mutation and is suitable for
        callers that do not operate in an async context.
        """
        webspace = self._resolve_webspace(webspace_id)
        current = self.get_installed(webspace)
        next_installed = self._next_installed_state(current, item_type, target_id)
        self.set_installed(next_installed, webspace)

    async def toggle_install_async(self, item_type: str, target_id: str, webspace_id: Optional[str] = None) -> None:
        """
        Async variant of toggle_install that uses async_get_ydoc and can be
        awaited from async runtimes.
        """
        webspace = self._resolve_webspace(webspace_id)
        current = await self.get_installed_async(webspace)
        next_installed = self._next_installed_state(current, item_type, target_id)
        await self.set_installed_async(next_installed, webspace)

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
        current = self.get_installed(webspace)
        next_installed = self._next_installed_state(current, item_type, target_id)
        self._persist_overlay_installed(webspace, next_installed)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_installed_state(doc, txn, next_installed)

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
            asyncio.run(self.set_installed_async(next_installed, webspace))
        else:
            loop.create_task(
                self.set_installed_async(next_installed, webspace),
                name=f"web-desktop-toggle-{webspace}",
            )

    def set_installed_with_live_room(
        self,
        installed: WebDesktopInstalled,
        webspace_id: Optional[str] = None,
    ) -> None:
        """
        Set the installed set while also attempting to update an in-memory
        YDoc room so that connected browsers see the change immediately.

        Intended for restoration flows (e.g. after YJS reload) rather
        than direct user actions.
        """
        webspace = self._resolve_webspace(webspace_id)
        apps = list(dict.fromkeys(installed.apps))
        widgets = list(dict.fromkeys(installed.widgets))
        next_installed = WebDesktopInstalled(apps=apps, widgets=widgets)
        self._persist_overlay_installed(webspace, next_installed)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_installed_state(doc, txn, next_installed)

        live_applied = mutate_live_room(webspace, _mutator)
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_installed webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_installed_async(next_installed, webspace))
        else:
            loop.create_task(
                self.set_installed_async(next_installed, webspace),
                name=f"web-desktop-set-installed-{webspace}",
            )
