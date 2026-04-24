from __future__ import annotations

from typing import Any, List

from adaos.sdk.core.decorators import tool
from adaos.services.io_web.desktop import WebDesktopService
from adaos.services.scenario.webspace_runtime import (
    WebspaceInfo,
    WebspaceService,
    describe_webspace_operational_state,
    describe_webspace_validation_state,
    describe_webspace_overlay_state,
    describe_webspace_projection_state,
    ensure_dev_webspace_for_scenario,
    go_home_webspace,
)
from adaos.services.yjs.webspace import default_webspace_id


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
    "web.webspace.describe",
    summary="Return manifest plus live current-scenario state for a webspace.",
    stability="experimental",
    examples=["await web.webspace.describe()", "await web.webspace.describe('prompt-lab')"],
)
async def webspace_describe(webspace_id: str | None = None) -> dict[str, Any]:
    """
    Return the current operational state of a webspace, including
    ``home_scenario``, ``current_scenario``, ``kind``, ``source_mode``,
    and the current projection-layer target snapshot.
    """
    target = str(webspace_id or "").strip() or default_webspace_id()
    return {
        "webspace": (await describe_webspace_operational_state(target)).to_dict(),
        "validation": await describe_webspace_validation_state(target),
        "overlay": describe_webspace_overlay_state(target),
        "desktop": (await WebDesktopService().get_snapshot_async(target)).to_dict(),
        "projection": await describe_webspace_projection_state(target),
    }


@tool(
    "web.webspace.validate",
    summary="Return authoritative backend validation for a webspace scenario state.",
    stability="experimental",
    examples=["await web.webspace.validate()", "await web.webspace.validate('desktop')"],
)
async def webspace_validate(webspace_id: str | None = None) -> dict[str, Any]:
    target = str(webspace_id or "").strip() or default_webspace_id()
    return await describe_webspace_validation_state(target)


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


@tool(
    "web.webspace.set_home",
    summary="Persist the home scenario for a webspace.",
    stability="experimental",
    examples=["await web.webspace.set_home('prompt-lab', 'prompt_engineer_scenario')"],
)
async def webspace_set_home(webspace_id: str, scenario_id: str) -> WebspaceInfo | None:
    """
    Update the persistent ``home_scenario`` for a webspace.
    """
    svc = WebspaceService()
    return await svc.set_home_scenario(webspace_id, scenario_id)


@tool(
    "web.webspace.go_home",
    summary="Switch a webspace back to its persistent home scenario.",
    stability="experimental",
    examples=["await web.webspace.go_home()", "await web.webspace.go_home('prompt-lab')"],
)
async def webspace_go_home(webspace_id: str | None = None) -> dict[str, Any]:
    """
    Switch the given webspace back to its manifest-defined ``home_scenario``.
    """
    target = str(webspace_id or "").strip() or default_webspace_id()
    return await go_home_webspace(target)


@tool(
    "web.webspace.ensure_dev",
    summary="Ensure a dev webspace exists for a scenario and return its identity.",
    stability="experimental",
    examples=["await web.webspace.ensure_dev('prompt_engineer_scenario')"],
)
async def webspace_ensure_dev(
    scenario_id: str,
    *,
    webspace_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """
    Reuse or create a dev webspace for the given base scenario.
    """
    return await ensure_dev_webspace_for_scenario(
        scenario_id,
        requested_id=webspace_id,
        title=title,
    )
