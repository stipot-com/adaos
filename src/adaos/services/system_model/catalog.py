from __future__ import annotations

from typing import Any

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.skill.manager import SkillManager
from adaos.services.system_model.mappers import canonical_object_from_scenario_item, canonical_object_from_skill_status


def _ctx(ctx: AgentContext | None = None) -> AgentContext:
    return ctx or get_ctx()


def _skill_manager(ctx: AgentContext | None = None) -> SkillManager:
    runtime = _ctx(ctx)
    return SkillManager(
        repo=runtime.skills_repo,
        registry=SqliteSkillRegistry(runtime.sql),
        git=runtime.git,
        paths=runtime.paths,
        bus=getattr(runtime, "bus", None),
        caps=runtime.caps,
        settings=runtime.settings,
    )


def _scenario_manager(ctx: AgentContext | None = None) -> ScenarioManager:
    runtime = _ctx(ctx)
    return ScenarioManager(
        repo=runtime.scenarios_repo,
        registry=SqliteScenarioRegistry(runtime.sql),
        git=runtime.git,
        paths=runtime.paths,
        bus=getattr(runtime, "bus", None),
        caps=runtime.caps,
    )


def skill_object(name: str, *, ctx: AgentContext | None = None):
    mgr = _skill_manager(ctx)
    meta = mgr.get(name)
    slot = ""
    try:
        state = mgr.runtime_status(name)
        if isinstance(state, dict):
            slot = str(state.get("active_slot") or "").strip()
    except Exception:
        slot = ""
    version = str(getattr(meta, "version", None) or "").strip() if meta is not None else ""
    payload: dict[str, Any] = {
        "name": name,
        "version": version or None,
        "slot": slot or None,
        "update_available": False,
    }
    return canonical_object_from_skill_status(payload)


def installed_skill_objects(*, ctx: AgentContext | None = None) -> list[Any]:
    mgr = _skill_manager(ctx)
    objects: list[Any] = []
    for row in list(mgr.list_installed() or []):
        if not bool(getattr(row, "installed", True)):
            continue
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue
        objects.append(skill_object(name, ctx=ctx))
    return objects


def scenario_object(name: str, *, ctx: AgentContext | None = None):
    mgr = _scenario_manager(ctx)
    rows = list(mgr.list_installed() or [])
    for row in rows:
        row_name = str(getattr(row, "name", "") or "").strip()
        if row_name == name:
            return canonical_object_from_scenario_item(
                {
                    "name": name,
                    "version": getattr(row, "version", None),
                    "path": getattr(row, "path", None),
                }
            )
    return canonical_object_from_scenario_item({"name": name})


def installed_scenario_objects(*, ctx: AgentContext | None = None) -> list[Any]:
    mgr = _scenario_manager(ctx)
    objects: list[Any] = []
    for row in list(mgr.list_installed() or []):
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue
        objects.append(
            canonical_object_from_scenario_item(
                {
                    "name": name,
                    "version": getattr(row, "version", None),
                    "path": getattr(row, "path", None),
                }
            )
        )
    return objects


__all__ = [
    "installed_scenario_objects",
    "installed_skill_objects",
    "scenario_object",
    "skill_object",
]
