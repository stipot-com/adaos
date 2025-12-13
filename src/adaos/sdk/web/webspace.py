from __future__ import annotations

from typing import List

from adaos.sdk.core.decorators import tool
from adaos.services.scenario.webspace_runtime import WebspaceInfo, WebspaceService


@tool(
    "web.webspace.list",
    summary="Return desktop webspaces filtered by mode (workspace/dev/mixed).",
    stability="stable",
    examples=["web.webspace.list()", "web.webspace.list(mode='dev')"],
)
def webspace_list(mode: str = "mixed") -> List[WebspaceInfo]:
    """
    Return a list of known webspaces filtered by mode.

    mode options:
      - ``"workspace"`` - only non-dev webspaces,
      - ``"dev"``       - only dev webspaces,
      - ``"mixed"``     - all (default).
    """
    svc = WebspaceService()
    return svc.list(mode=mode)


@tool(
    "web.webspace.create",
    summary="Create a desktop webspace seeded from a scenario.",
    stability="stable",
    examples=["await web.webspace.create('testws', 'Test WS', dev=True)"],
)
async def webspace_create(
    webspace_id: str | None = None,
    title: str | None = None,
    *,
    scenario_id: str = "web_desktop",
    dev: bool = False,
) -> WebspaceInfo:
    """
    Create a new webspace seeded from a scenario.

    Parameters
    ----------
    webspace_id:
        Preferred identifier. If omitted or already taken, a unique ID is
        generated from it via slugification + numeric suffix.
    title:
        Human-readable title. For dev webspaces a ``DEV: `` prefix is added
        automatically when ``dev=True``.
    scenario_id:
        Desktop scenario used to seed the YDoc (default: ``"web_desktop"``).
    dev:
        When True, the webspace is marked as a dev space so IDEs can filter it.
    """
    svc = WebspaceService()
    return await svc.create(webspace_id, title, scenario_id=scenario_id, dev=dev)


@tool(
    "web.webspace.rename",
    summary="Rename a webspace without changing its dev/workspace type.",
    stability="stable",
    examples=["await web.webspace.rename('testws', 'New Title')"],
)
async def webspace_rename(webspace_id: str, title: str) -> WebspaceInfo | None:
    """
    Rename an existing webspace while preserving its dev/non-dev status.
    """
    svc = WebspaceService()
    return await svc.rename(webspace_id, title)


@tool(
    "web.webspace.delete",
    summary="Delete a webspace together with its stored YDoc snapshot.",
    stability="experimental",
    examples=["await web.webspace.delete('oldws')"],
)
async def webspace_delete(webspace_id: str) -> bool:
    """
    Delete a webspace and its associated YDoc snapshot.
    """
    svc = WebspaceService()
    return await svc.delete(webspace_id)


@tool(
    "web.webspace.refresh",
    summary="Rebuild the shared ``data.webspaces`` listing across rooms.",
    stability="stable",
    examples=["await web.webspace.refresh()"],
)
async def webspace_refresh() -> None:
    """
    Re-sync the ``data.webspaces`` listing across all webspaces.
    """
    svc = WebspaceService()
    await svc.refresh()
