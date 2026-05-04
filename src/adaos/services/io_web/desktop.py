from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import async_get_ydoc, async_read_ydoc, get_ydoc, mutate_live_room
from adaos.services.yjs.store import ystore_write_metadata, ystore_write_metadata_sync
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspaces import index as workspace_index

_log = logging.getLogger("adaos.io_web.desktop")


def _desktop_async_write_meta():
    return ystore_write_metadata(
        root_names=["data", "ui"],
        source="io_web.desktop",
        owner="core:desktop",
        channel="core.desktop.async",
    )


def _desktop_sync_write_meta():
    return ystore_write_metadata_sync(
        root_names=["data", "ui"],
        source="io_web.desktop",
        owner="core:desktop",
        channel="core.desktop.sync",
    )


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


@dataclass(slots=True)
class WebDesktopSnapshot:
    installed: WebDesktopInstalled
    pinned_widgets: List[Dict[str, Any]]
    topbar: List[Any]
    page_schema: Dict[str, Any]
    icon_order: List[str] = field(default_factory=list)
    widget_order: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed.to_dict(),
            "pinnedWidgets": _clone_pinned_widgets(self.pinned_widgets),
            "topbar": _clone_json_list(self.topbar),
            "pageSchema": _clone_json_dict(self.page_schema),
            "iconOrder": _clone_text_list(self.icon_order),
            "widgetOrder": _clone_text_list(self.widget_order),
        }


def _clone_pinned_widgets(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        payload = dict(item)
        payload["id"] = item_id
        item_type = payload.get("type")
        if item_type is not None:
            payload["type"] = str(item_type)
        out.append(payload)
    return out


def _clone_json_dict(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        payload = json.loads(json.dumps(value, ensure_ascii=True))
    except Exception:
        payload = dict(value)
    return payload if isinstance(payload, dict) else {}


def _clone_json_list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        return []
    try:
        payload = json.loads(json.dumps(value, ensure_ascii=True))
    except Exception:
        payload = list(value)
    return payload if isinstance(payload, list) else []


def _clone_text_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in value:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


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
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_installed_overlay", False):
            return WebDesktopInstalled(apps=[], widgets=[]), False
        installed = getattr(row, "installed_overlay", {}) or {}
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
    def _read_overlay_pinned_widgets(webspace_id: str) -> tuple[List[Dict[str, Any]], bool]:
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_pinned_widgets_overlay", False):
            return [], False
        return _clone_pinned_widgets(getattr(row, "pinned_widgets_overlay", []) or []), True

    @staticmethod
    def _persist_overlay_pinned_widgets(webspace_id: str, items: List[Dict[str, Any]]) -> None:
        workspace_index.set_workspace_pinned_widgets_overlay(webspace_id, _clone_pinned_widgets(items))

    @staticmethod
    def _read_overlay_topbar(webspace_id: str) -> tuple[List[Any], bool]:
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_topbar_overlay", False):
            return [], False
        return _clone_json_list(getattr(row, "topbar_overlay", []) or []), True

    @staticmethod
    def _persist_overlay_topbar(webspace_id: str, topbar: List[Any]) -> None:
        workspace_index.set_workspace_topbar_overlay(webspace_id, _clone_json_list(topbar))

    @staticmethod
    def _read_overlay_page_schema(webspace_id: str) -> tuple[Dict[str, Any], bool]:
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_page_schema_overlay", False):
            return {}, False
        return _clone_json_dict(getattr(row, "page_schema_overlay", {}) or {}), True

    @staticmethod
    def _persist_overlay_page_schema(webspace_id: str, page_schema: Dict[str, Any]) -> None:
        workspace_index.set_workspace_page_schema_overlay(webspace_id, _clone_json_dict(page_schema))

    @staticmethod
    def _read_overlay_icon_order(webspace_id: str) -> tuple[List[str], bool]:
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_icon_order_overlay", False):
            return [], False
        return _clone_text_list(getattr(row, "icon_order_overlay", []) or []), True

    @staticmethod
    def _persist_overlay_icon_order(webspace_id: str, icon_order: List[str]) -> None:
        workspace_index.set_workspace_icon_order_overlay(webspace_id, _clone_text_list(icon_order))

    @staticmethod
    def _read_overlay_widget_order(webspace_id: str) -> tuple[List[str], bool]:
        row = workspace_index.get_workspace(webspace_id)
        if row is None or not getattr(row, "has_widget_order_overlay", False):
            return [], False
        return _clone_text_list(getattr(row, "widget_order_overlay", []) or []), True

    @staticmethod
    def _persist_overlay_widget_order(webspace_id: str, widget_order: List[str]) -> None:
        workspace_index.set_workspace_widget_order_overlay(webspace_id, _clone_text_list(widget_order))

    @staticmethod
    def _apply_pinned_widgets_state(ydoc: Any, txn: Any, pinned_widgets: List[Dict[str, Any]]) -> None:
        next_pinned = _clone_pinned_widgets(pinned_widgets)
        ui_map = ydoc.get_map("ui")
        application_raw = ui_map.get("application") or {}
        application_next = _coerce_dict(application_raw)
        app_desktop_raw = application_next.get("desktop") or {}
        app_desktop = _coerce_dict(app_desktop_raw)
        app_desktop["pinnedWidgets"] = next_pinned
        application_next["desktop"] = app_desktop
        ui_map.set(txn, "application", application_next)

        data_map = ydoc.get_map("data")
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_next["pinnedWidgets"] = next_pinned
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _apply_topbar_state(ydoc: Any, txn: Any, topbar: List[Any]) -> None:
        next_topbar = _clone_json_list(topbar)
        ui_map = ydoc.get_map("ui")
        application_raw = ui_map.get("application") or {}
        application_next = _coerce_dict(application_raw)
        app_desktop_raw = application_next.get("desktop") or {}
        app_desktop = _coerce_dict(app_desktop_raw)
        app_desktop["topbar"] = next_topbar
        application_next["desktop"] = app_desktop
        ui_map.set(txn, "application", application_next)

        data_map = ydoc.get_map("data")
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_next["topbar"] = next_topbar
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _apply_page_schema_state(ydoc: Any, txn: Any, page_schema: Dict[str, Any]) -> None:
        next_schema = _clone_json_dict(page_schema)
        ui_map = ydoc.get_map("ui")
        application_raw = ui_map.get("application") or {}
        application_next = _coerce_dict(application_raw)
        app_desktop_raw = application_next.get("desktop") or {}
        app_desktop = _coerce_dict(app_desktop_raw)
        app_desktop["pageSchema"] = next_schema
        application_next["desktop"] = app_desktop
        ui_map.set(txn, "application", application_next)

        data_map = ydoc.get_map("data")
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_next["pageSchema"] = next_schema
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _apply_icon_order_state(ydoc: Any, txn: Any, icon_order: List[str]) -> None:
        next_order = _clone_text_list(icon_order)
        data_map = ydoc.get_map("data")
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_next["iconOrder"] = next_order
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _apply_widget_order_state(ydoc: Any, txn: Any, widget_order: List[str]) -> None:
        next_order = _clone_text_list(widget_order)
        data_map = ydoc.get_map("data")
        desktop_raw = data_map.get("desktop") or {}
        desktop_next = _coerce_dict(desktop_raw)
        desktop_next["widgetOrder"] = next_order
        data_map.set(txn, "desktop", desktop_next)

    @staticmethod
    def _apply_snapshot_state(ydoc: Any, txn: Any, snapshot: WebDesktopSnapshot) -> None:
        WebDesktopService._apply_installed_state(ydoc, txn, snapshot.installed)
        WebDesktopService._apply_pinned_widgets_state(ydoc, txn, snapshot.pinned_widgets)
        WebDesktopService._apply_topbar_state(ydoc, txn, snapshot.topbar)
        WebDesktopService._apply_page_schema_state(ydoc, txn, snapshot.page_schema)
        WebDesktopService._apply_icon_order_state(ydoc, txn, snapshot.icon_order)
        WebDesktopService._apply_widget_order_state(ydoc, txn, snapshot.widget_order)

    @staticmethod
    def _read_materialized_snapshot_from_doc(
        ydoc: Any,
        *,
        installed: WebDesktopInstalled,
        pinned_widgets: List[Dict[str, Any]],
        topbar: List[Any],
        page_schema: Dict[str, Any],
        icon_order: List[str],
        widget_order: List[str],
    ) -> WebDesktopSnapshot:
        data_map = ydoc.get_map("data")
        ui_map = ydoc.get_map("ui")

        installed_raw = _coerce_dict(data_map.get("installed") or {})
        installed_next = WebDesktopInstalled(
            apps=_iter_ids(installed_raw.get("apps")) or list(installed.apps),
            widgets=_iter_ids(installed_raw.get("widgets")) or list(installed.widgets),
        )

        desktop_raw = _coerce_dict(data_map.get("desktop") or {})
        pinned_next = _clone_pinned_widgets(desktop_raw.get("pinnedWidgets"))
        if not pinned_next:
            pinned_next = _clone_pinned_widgets(pinned_widgets)

        application_raw = _coerce_dict(ui_map.get("application") or {})
        app_desktop = _coerce_dict(application_raw.get("desktop") or {})
        topbar_next = _clone_json_list(app_desktop.get("topbar"))
        if not topbar_next:
            topbar_next = _clone_json_list(desktop_raw.get("topbar"))
        if not topbar_next:
            topbar_next = _clone_json_list(topbar)

        page_schema_next = _clone_json_dict(app_desktop.get("pageSchema"))
        if not page_schema_next:
            page_schema_next = _clone_json_dict(desktop_raw.get("pageSchema"))
        if not page_schema_next:
            page_schema_next = _clone_json_dict(page_schema)

        icon_order_next = _clone_text_list(desktop_raw.get("iconOrder"))
        if not icon_order_next:
            icon_order_next = _clone_text_list(icon_order)

        widget_order_next = _clone_text_list(desktop_raw.get("widgetOrder"))
        if not widget_order_next:
            widget_order_next = _clone_text_list(widget_order)

        return WebDesktopSnapshot(
            installed=installed_next,
            pinned_widgets=pinned_next,
            topbar=topbar_next,
            page_schema=page_schema_next,
            icon_order=icon_order_next,
            widget_order=widget_order_next,
        )

    def get_installed(self, webspace_id: Optional[str] = None) -> WebDesktopInstalled:
        """
        Read the current set of installed apps/widgets for a webspace.

        This is a read-only helper; callers that need to change the
        installed set should use ``toggle_*`` or ``set_installed*``.
        """
        webspace = self._resolve_webspace(webspace_id)
        overlay_installed, has_overlay = self._read_overlay_installed(webspace)
        if not has_overlay:
            return WebDesktopInstalled(apps=[], widgets=[])
        return overlay_installed

    async def get_installed_async(self, webspace_id: Optional[str] = None) -> WebDesktopInstalled:
        """
        Async variant of ``get_installed`` for use from async runtimes.
        """
        webspace = self._resolve_webspace(webspace_id)
        overlay_installed, has_overlay = self._read_overlay_installed(webspace)
        if not has_overlay:
            return WebDesktopInstalled(apps=[], widgets=[])
        return overlay_installed

    def get_pinned_widgets(self, webspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_pinned_widgets(webspace)
        if not has_overlay:
            return []
        return items

    async def get_pinned_widgets_async(self, webspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_pinned_widgets(webspace)
        if not has_overlay:
            return []
        return items

    def get_topbar(self, webspace_id: Optional[str] = None) -> List[Any]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_topbar(webspace)
        if not has_overlay:
            return []
        return items

    async def get_topbar_async(self, webspace_id: Optional[str] = None) -> List[Any]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_topbar(webspace)
        if not has_overlay:
            return []
        return items

    def get_page_schema(self, webspace_id: Optional[str] = None) -> Dict[str, Any]:
        webspace = self._resolve_webspace(webspace_id)
        page_schema, has_overlay = self._read_overlay_page_schema(webspace)
        if not has_overlay:
            return {}
        return page_schema

    async def get_page_schema_async(self, webspace_id: Optional[str] = None) -> Dict[str, Any]:
        webspace = self._resolve_webspace(webspace_id)
        page_schema, has_overlay = self._read_overlay_page_schema(webspace)
        if not has_overlay:
            return {}
        return page_schema

    def get_icon_order(self, webspace_id: Optional[str] = None) -> List[str]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_icon_order(webspace)
        if not has_overlay:
            return []
        return items

    async def get_icon_order_async(self, webspace_id: Optional[str] = None) -> List[str]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_icon_order(webspace)
        if not has_overlay:
            return []
        return items

    def get_widget_order(self, webspace_id: Optional[str] = None) -> List[str]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_widget_order(webspace)
        if not has_overlay:
            return []
        return items

    async def get_widget_order_async(self, webspace_id: Optional[str] = None) -> List[str]:
        webspace = self._resolve_webspace(webspace_id)
        items, has_overlay = self._read_overlay_widget_order(webspace)
        if not has_overlay:
            return []
        return items

    def get_snapshot(self, webspace_id: Optional[str] = None) -> WebDesktopSnapshot:
        webspace = self._resolve_webspace(webspace_id)
        installed = self.get_installed(webspace)
        pinned_widgets = self.get_pinned_widgets(webspace)
        topbar = self.get_topbar(webspace)
        page_schema = self.get_page_schema(webspace)
        icon_order = self.get_icon_order(webspace)
        widget_order = self.get_widget_order(webspace)
        try:
            with get_ydoc(webspace) as ydoc:
                return self._read_materialized_snapshot_from_doc(
                    ydoc,
                    installed=installed,
                    pinned_widgets=pinned_widgets,
                    topbar=topbar,
                    page_schema=page_schema,
                    icon_order=icon_order,
                    widget_order=widget_order,
                )
        except Exception:
            return WebDesktopSnapshot(
                installed=installed,
                pinned_widgets=pinned_widgets,
                topbar=topbar,
                page_schema=page_schema,
                icon_order=icon_order,
                widget_order=widget_order,
            )

    async def get_snapshot_async(self, webspace_id: Optional[str] = None) -> WebDesktopSnapshot:
        webspace = self._resolve_webspace(webspace_id)
        installed = await self.get_installed_async(webspace)
        pinned_widgets = await self.get_pinned_widgets_async(webspace)
        topbar = await self.get_topbar_async(webspace)
        page_schema = await self.get_page_schema_async(webspace)
        icon_order = await self.get_icon_order_async(webspace)
        widget_order = await self.get_widget_order_async(webspace)
        try:
            async with async_read_ydoc(webspace) as ydoc:
                return self._read_materialized_snapshot_from_doc(
                    ydoc,
                    installed=installed,
                    pinned_widgets=pinned_widgets,
                    topbar=topbar,
                    page_schema=page_schema,
                    icon_order=icon_order,
                    widget_order=widget_order,
                )
        except Exception:
            return WebDesktopSnapshot(
                installed=installed,
                pinned_widgets=pinned_widgets,
                topbar=topbar,
                page_schema=page_schema,
                icon_order=icon_order,
                widget_order=widget_order,
            )

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
        with _desktop_sync_write_meta():
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
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_installed_state(ydoc, txn, WebDesktopInstalled(apps=apps, widgets=widgets))
        _log.debug(
            "set installed (async) webspace=%s apps=%s widgets=%s",
            webspace,
            apps,
            widgets,
        )

    def set_pinned_widgets(self, pinned_widgets: List[Dict[str, Any]], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_pinned = _clone_pinned_widgets(pinned_widgets)
        self._persist_overlay_pinned_widgets(webspace, next_pinned)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_pinned_widgets_state(ydoc, txn, next_pinned)
        _log.debug(
            "set pinned widgets webspace=%s count=%s",
            webspace,
            len(next_pinned),
        )

    async def set_pinned_widgets_async(self, pinned_widgets: List[Dict[str, Any]], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_pinned = _clone_pinned_widgets(pinned_widgets)
        self._persist_overlay_pinned_widgets(webspace, next_pinned)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_pinned_widgets_state(ydoc, txn, next_pinned)
        _log.debug(
            "set pinned widgets (async) webspace=%s count=%s",
            webspace,
            len(next_pinned),
        )

    def set_topbar(self, topbar: List[Any], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_topbar = _clone_json_list(topbar)
        self._persist_overlay_topbar(webspace, next_topbar)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_topbar_state(ydoc, txn, next_topbar)
        _log.debug("set topbar webspace=%s count=%s", webspace, len(next_topbar))

    async def set_topbar_async(self, topbar: List[Any], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_topbar = _clone_json_list(topbar)
        self._persist_overlay_topbar(webspace, next_topbar)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_topbar_state(ydoc, txn, next_topbar)
        _log.debug("set topbar (async) webspace=%s count=%s", webspace, len(next_topbar))

    def set_page_schema(self, page_schema: Dict[str, Any], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_page_schema = _clone_json_dict(page_schema)
        self._persist_overlay_page_schema(webspace, next_page_schema)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_page_schema_state(ydoc, txn, next_page_schema)
        _log.debug(
            "set page schema webspace=%s widgets=%s",
            webspace,
            len(_clone_json_list(next_page_schema.get("widgets"))),
        )

    async def set_page_schema_async(self, page_schema: Dict[str, Any], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_page_schema = _clone_json_dict(page_schema)
        self._persist_overlay_page_schema(webspace, next_page_schema)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_page_schema_state(ydoc, txn, next_page_schema)
        _log.debug(
            "set page schema (async) webspace=%s widgets=%s",
            webspace,
            len(_clone_json_list(next_page_schema.get("widgets"))),
        )

    def set_icon_order(self, icon_order: List[str], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_icon_order = _clone_text_list(icon_order)
        self._persist_overlay_icon_order(webspace, next_icon_order)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_icon_order_state(ydoc, txn, next_icon_order)
        _log.debug("set icon order webspace=%s count=%s", webspace, len(next_icon_order))

    async def set_icon_order_async(self, icon_order: List[str], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_icon_order = _clone_text_list(icon_order)
        self._persist_overlay_icon_order(webspace, next_icon_order)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_icon_order_state(ydoc, txn, next_icon_order)
        _log.debug("set icon order (async) webspace=%s count=%s", webspace, len(next_icon_order))

    def set_widget_order(self, widget_order: List[str], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_widget_order = _clone_text_list(widget_order)
        self._persist_overlay_widget_order(webspace, next_widget_order)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_widget_order_state(ydoc, txn, next_widget_order)
        _log.debug("set widget order webspace=%s count=%s", webspace, len(next_widget_order))

    async def set_widget_order_async(self, widget_order: List[str], webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_widget_order = _clone_text_list(widget_order)
        self._persist_overlay_widget_order(webspace, next_widget_order)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_widget_order_state(ydoc, txn, next_widget_order)
        _log.debug("set widget order (async) webspace=%s count=%s", webspace, len(next_widget_order))

    def set_snapshot(self, snapshot: WebDesktopSnapshot, webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        self._persist_overlay_installed(webspace, snapshot.installed)
        self._persist_overlay_pinned_widgets(webspace, snapshot.pinned_widgets)
        self._persist_overlay_topbar(webspace, snapshot.topbar)
        self._persist_overlay_page_schema(webspace, snapshot.page_schema)
        self._persist_overlay_icon_order(webspace, snapshot.icon_order)
        self._persist_overlay_widget_order(webspace, snapshot.widget_order)
        with _desktop_sync_write_meta():
            with get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_snapshot_state(ydoc, txn, snapshot)
        _log.debug("set desktop snapshot webspace=%s", webspace)

    async def set_snapshot_async(self, snapshot: WebDesktopSnapshot, webspace_id: Optional[str] = None) -> None:
        webspace = self._resolve_webspace(webspace_id)
        self._persist_overlay_installed(webspace, snapshot.installed)
        self._persist_overlay_pinned_widgets(webspace, snapshot.pinned_widgets)
        self._persist_overlay_topbar(webspace, snapshot.topbar)
        self._persist_overlay_page_schema(webspace, snapshot.page_schema)
        self._persist_overlay_icon_order(webspace, snapshot.icon_order)
        self._persist_overlay_widget_order(webspace, snapshot.widget_order)
        async with _desktop_async_write_meta():
            async with async_get_ydoc(webspace) as ydoc:
                with ydoc.begin_transaction() as txn:
                    self._apply_snapshot_state(ydoc, txn, snapshot)
        _log.debug("set desktop snapshot (async) webspace=%s", webspace)

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

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
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

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
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

    def set_pinned_widgets_with_live_room(
        self,
        pinned_widgets: List[Dict[str, Any]],
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_pinned = _clone_pinned_widgets(pinned_widgets)
        self._persist_overlay_pinned_widgets(webspace, next_pinned)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_pinned_widgets_state(doc, txn, next_pinned)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_pinned_widgets webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_pinned_widgets_async(next_pinned, webspace))
        else:
            loop.create_task(
                self.set_pinned_widgets_async(next_pinned, webspace),
                name=f"web-desktop-set-pinned-{webspace}",
            )

    def set_topbar_with_live_room(
        self,
        topbar: List[Any],
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_topbar = _clone_json_list(topbar)
        self._persist_overlay_topbar(webspace, next_topbar)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_topbar_state(doc, txn, next_topbar)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_topbar webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_topbar_async(next_topbar, webspace))
        else:
            loop.create_task(
                self.set_topbar_async(next_topbar, webspace),
                name=f"web-desktop-set-topbar-{webspace}",
            )

    def set_page_schema_with_live_room(
        self,
        page_schema: Dict[str, Any],
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_page_schema = _clone_json_dict(page_schema)
        self._persist_overlay_page_schema(webspace, next_page_schema)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_page_schema_state(doc, txn, next_page_schema)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_page_schema webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_page_schema_async(next_page_schema, webspace))
        else:
            loop.create_task(
                self.set_page_schema_async(next_page_schema, webspace),
                name=f"web-desktop-set-page-schema-{webspace}",
            )

    def set_icon_order_with_live_room(
        self,
        icon_order: List[str],
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_icon_order = _clone_text_list(icon_order)
        self._persist_overlay_icon_order(webspace, next_icon_order)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_icon_order_state(doc, txn, next_icon_order)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_icon_order webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_icon_order_async(next_icon_order, webspace))
        else:
            loop.create_task(
                self.set_icon_order_async(next_icon_order, webspace),
                name=f"web-desktop-set-icon-order-{webspace}",
            )

    def set_widget_order_with_live_room(
        self,
        widget_order: List[str],
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        next_widget_order = _clone_text_list(widget_order)
        self._persist_overlay_widget_order(webspace, next_widget_order)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_widget_order_state(doc, txn, next_widget_order)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_widget_order webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_widget_order_async(next_widget_order, webspace))
        else:
            loop.create_task(
                self.set_widget_order_async(next_widget_order, webspace),
                name=f"web-desktop-set-widget-order-{webspace}",
            )

    def set_snapshot_with_live_room(
        self,
        snapshot: WebDesktopSnapshot,
        webspace_id: Optional[str] = None,
    ) -> None:
        webspace = self._resolve_webspace(webspace_id)
        self._persist_overlay_installed(webspace, snapshot.installed)
        self._persist_overlay_pinned_widgets(webspace, snapshot.pinned_widgets)
        self._persist_overlay_topbar(webspace, snapshot.topbar)
        self._persist_overlay_page_schema(webspace, snapshot.page_schema)
        self._persist_overlay_icon_order(webspace, snapshot.icon_order)
        self._persist_overlay_widget_order(webspace, snapshot.widget_order)

        def _mutator(doc: Any, txn: Any) -> None:
            self._apply_snapshot_state(doc, txn, snapshot)

        live_applied = mutate_live_room(
            webspace,
            _mutator,
            root_names=["data", "ui"],
            source="io_web.desktop",
            owner="core:desktop",
            channel="core.desktop.live_room",
        )
        if not live_applied:
            _log.debug("mutate_live_room skipped for set_snapshot webspace=%s", webspace)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_snapshot_async(snapshot, webspace))
        else:
            loop.create_task(
                self.set_snapshot_async(snapshot, webspace),
                name=f"web-desktop-set-snapshot-{webspace}",
            )
