from __future__ import annotations

from typing import Optional

from adaos.sdk.core.decorators import tool
from adaos.services.io_web.desktop import WebDesktopService, WebDesktopInstalled


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
