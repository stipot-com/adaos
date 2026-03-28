from __future__ import annotations

from typing import Any, Optional

from adaos.sdk.core.decorators import tool
from adaos.services.io_web.desktop import WebDesktopService, WebDesktopInstalled, WebDesktopSnapshot


@tool(
    "web.desktop.toggle_install",
    summary="Install or uninstall a desktop catalog item for a webspace.",
    stability="stable",
    examples=["web.desktop.toggle_install('app', 'scenario:prompt_engineer_scenario')"],
)
def desktop_toggle_install(
    item_type: str,
    item_id: str,
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    """
    Toggle installation of a desktop catalog item for a webspace.

    Parameters
    ----------
    item_type:
        Logical type of the item: ``"app"`` or ``"widget"``.
    item_id:
        Identifier of the app or widget in ``data.catalog``.
    webspace_id:
        Target webspace identifier; if omitted or empty, the default
        webspace is used.
    live:
        When ``True`` (default), the helper also updates an in-memory
        YDoc room if one is attached so that connected browsers see an
        immediate change. When ``False``, only the underlying YStore is
        updated.
    """
    svc = WebDesktopService()
    if live:
        svc.toggle_install_with_live_room(item_type, item_id, webspace_id)
    else:
        svc.toggle_install(item_type, item_id, webspace_id)


@tool(
    "web.desktop.toggle_app",
    summary="Pin or remove a desktop application icon.",
    stability="stable",
    examples=["web.desktop.toggle_app('scenario:prompt_engineer_scenario')"],
)
def desktop_toggle_app(app_id: str, webspace_id: Optional[str] = None, *, live: bool = True) -> None:
    """
    Convenience wrapper for toggling installation of an app icon.
    """
    desktop_toggle_install("app", app_id, webspace_id, live=live)


@tool(
    "web.desktop.toggle_widget",
    summary="Pin or remove a desktop widget.",
    stability="stable",
    examples=["web.desktop.toggle_widget('weather')"],
)
def desktop_toggle_widget(widget_id: str, webspace_id: Optional[str] = None, *, live: bool = True) -> None:
    """
    Convenience wrapper for toggling installation of a desktop widget.
    """
    desktop_toggle_install("widget", widget_id, webspace_id, live=live)


@tool(
    "web.desktop.get_installed",
    summary="Return installed desktop apps/widgets for a webspace.",
    stability="stable",
    examples=["web.desktop.get_installed()", "web.desktop.get_installed('t1')"],
)
def desktop_get_installed(webspace_id: Optional[str] = None) -> dict:
    """
    Read the current set of installed desktop items for a webspace.

    Returns a mapping with ``apps`` and ``widgets`` lists.
    """
    svc = WebDesktopService()
    installed = svc.get_installed(webspace_id)
    return installed.to_dict()


@tool(
    "web.desktop.get_snapshot",
    summary="Return materialized desktop state for a webspace.",
    stability="experimental",
    examples=["web.desktop.get_snapshot()", "web.desktop.get_snapshot('default')"],
)
def desktop_get_snapshot(webspace_id: Optional[str] = None) -> dict:
    svc = WebDesktopService()
    return svc.get_snapshot(webspace_id).to_dict()


async def desktop_get_installed_async(webspace_id: Optional[str] = None) -> dict:
    """
    Async helper for reading installed desktop items for a webspace.

    This mirrors :func:`desktop_get_installed` but is safe to call from
    within an active event loop by delegating to WebDesktopService.
    """
    svc = WebDesktopService()
    installed = await svc.get_installed_async(webspace_id)
    return installed.to_dict()


async def desktop_get_snapshot_async(webspace_id: Optional[str] = None) -> dict:
    svc = WebDesktopService()
    return (await svc.get_snapshot_async(webspace_id)).to_dict()


@tool(
    "web.desktop.get_pinned_widgets",
    summary="Return pinned desktop widgets for a webspace.",
    stability="experimental",
    examples=["web.desktop.get_pinned_widgets()", "web.desktop.get_pinned_widgets('default')"],
)
def desktop_get_pinned_widgets(webspace_id: Optional[str] = None) -> list[dict[str, Any]]:
    svc = WebDesktopService()
    return svc.get_pinned_widgets(webspace_id)


async def desktop_get_pinned_widgets_async(webspace_id: Optional[str] = None) -> list[dict[str, Any]]:
    svc = WebDesktopService()
    return await svc.get_pinned_widgets_async(webspace_id)


@tool(
    "web.desktop.get_topbar",
    summary="Return the persistent/customized desktop topbar for a webspace.",
    stability="experimental",
    examples=["web.desktop.get_topbar()", "web.desktop.get_topbar('default')"],
)
def desktop_get_topbar(webspace_id: Optional[str] = None) -> list[Any]:
    svc = WebDesktopService()
    return svc.get_topbar(webspace_id)


async def desktop_get_topbar_async(webspace_id: Optional[str] = None) -> list[Any]:
    svc = WebDesktopService()
    return await svc.get_topbar_async(webspace_id)


@tool(
    "web.desktop.get_page_schema",
    summary="Return the persistent/customized desktop pageSchema for a webspace.",
    stability="experimental",
    examples=["web.desktop.get_page_schema()", "web.desktop.get_page_schema('default')"],
)
def desktop_get_page_schema(webspace_id: Optional[str] = None) -> dict[str, Any]:
    svc = WebDesktopService()
    return svc.get_page_schema(webspace_id)


async def desktop_get_page_schema_async(webspace_id: Optional[str] = None) -> dict[str, Any]:
    svc = WebDesktopService()
    return await svc.get_page_schema_async(webspace_id)


@tool(
    "web.desktop.set_installed",
    summary="Replace installed desktop apps/widgets for a webspace.",
    stability="experimental",
    examples=["web.desktop.set_installed(['scenario:prompt_engineer_scenario'], ['weather'])"],
)
def desktop_set_installed(
    app_ids: list[str],
    widget_ids: list[str],
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    """
    Replace the installed desktop apps/widgets set for a webspace.

    Intended for restoration flows (for example, after YJS reload) where
    the desired installed set is known ahead of time.
    """
    installed = WebDesktopInstalled(apps=list(app_ids or []), widgets=list(widget_ids or []))
    svc = WebDesktopService()
    if live:
        svc.set_installed_with_live_room(installed, webspace_id)
    else:
        svc.set_installed(installed, webspace_id)


@tool(
    "web.desktop.set_pinned_widgets",
    summary="Replace pinned desktop widgets for a webspace.",
    stability="experimental",
    examples=["web.desktop.set_pinned_widgets([{'id': 'infra-status', 'type': 'visual.metricTile'}])"],
)
def desktop_set_pinned_widgets(
    pinned_widgets: list[dict[str, Any]],
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    svc = WebDesktopService()
    if live:
        svc.set_pinned_widgets_with_live_room(list(pinned_widgets or []), webspace_id)
    else:
        svc.set_pinned_widgets(list(pinned_widgets or []), webspace_id)


@tool(
    "web.desktop.set_topbar",
    summary="Replace desktop topbar items for a webspace.",
    stability="experimental",
    examples=["web.desktop.set_topbar([{'id':'home','label':'Home'}])"],
)
def desktop_set_topbar(
    topbar: list[Any],
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    svc = WebDesktopService()
    if live:
        svc.set_topbar_with_live_room(list(topbar or []), webspace_id)
    else:
        svc.set_topbar(list(topbar or []), webspace_id)


@tool(
    "web.desktop.set_page_schema",
    summary="Replace desktop pageSchema for a webspace.",
    stability="experimental",
    examples=["web.desktop.set_page_schema({'id':'desktop','layout':{'type':'single','areas':[{'id':'main','role':'main'}]},'widgets':[]})"],
)
def desktop_set_page_schema(
    page_schema: dict[str, Any],
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    svc = WebDesktopService()
    if live:
        svc.set_page_schema_with_live_room(dict(page_schema or {}), webspace_id)
    else:
        svc.set_page_schema(dict(page_schema or {}), webspace_id)


@tool(
    "web.desktop.set_snapshot",
    summary="Replace materialized desktop customization state for a webspace.",
    stability="experimental",
    examples=["web.desktop.set_snapshot({'installed': {'apps': [], 'widgets': []}, 'pinnedWidgets': [], 'topbar': [], 'pageSchema': {}})"],
)
def desktop_set_snapshot(
    snapshot: dict[str, Any],
    webspace_id: Optional[str] = None,
    *,
    live: bool = True,
) -> None:
    payload = dict(snapshot or {})
    installed_raw = payload.get("installed") if isinstance(payload.get("installed"), dict) else {}
    next_snapshot = WebDesktopSnapshot(
        installed=WebDesktopInstalled(
            apps=list(installed_raw.get("apps") or []),
            widgets=list(installed_raw.get("widgets") or []),
        ),
        pinned_widgets=list(payload.get("pinnedWidgets") or []),
        topbar=list(payload.get("topbar") or []),
        page_schema=dict(payload.get("pageSchema") or {}),
    )
    svc = WebDesktopService()
    if live:
        svc.set_snapshot_with_live_room(next_snapshot, webspace_id)
    else:
        svc.set_snapshot(next_snapshot, webspace_id)
