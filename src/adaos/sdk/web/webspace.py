from __future__ import annotations

from typing import List

from adaos.services.scenario.webspace_runtime import WebspaceInfo, WebspaceService


def webspace_list(mode: str = "mixed") -> List[WebspaceInfo]:
  """
  Return a list of known webspaces.

  mode:
    - "workspace" — only non-dev webspaces,
    - "dev"       — only dev webspaces,
    - "mixed"     — all (default).
  """
  svc = WebspaceService()
  return svc.list(mode=mode)


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
      generated from it using slugification + numeric suffix.
  title:
      Human-readable title. For dev webspaces a "DEV: " prefix is added
      automatically when `dev=True`.
  scenario_id:
      Desktop scenario used to seed the YDoc (default: "web_desktop").
  dev:
      When True, the webspace is marked as a dev space and its title is
      prefixed with "DEV: ". This flag can later be used to show dev-only
      workspaces in IDEs.
  """
  svc = WebspaceService()
  return await svc.create(webspace_id, title, scenario_id=scenario_id, dev=dev)


async def webspace_rename(webspace_id: str, title: str) -> WebspaceInfo | None:
  """
  Rename an existing webspace while preserving its dev/non-dev status.
  """
  svc = WebspaceService()
  return await svc.rename(webspace_id, title)


async def webspace_delete(webspace_id: str) -> bool:
  """
  Delete a webspace and its associated YDoc snapshot.
  """
  svc = WebspaceService()
  return await svc.delete(webspace_id)


async def webspace_refresh() -> None:
  """
  Re-sync the `data.webspaces` listing across all webspaces.
  """
  svc = WebspaceService()
  await svc.refresh()

