from __future__ import annotations

from pathlib import Path
from typing import Any

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.adapters.git.workspace import SparseWorkspace
from adaos.services.git.workspace_guard import ensure_clean
from adaos.services.workspace_registry import registry_pattern_set, rebuild_workspace_registry


def installed_names(rows: list[object]) -> list[str]:
    names: list[str] = []
    for row in rows:
        if not bool(getattr(row, "installed", True)):
            continue
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name:
            continue
        names.append(str(name))
    return sorted(set(names))


def workspace_kind_names(ctx, workspace_root: Path, kind: str) -> list[str]:
    names: set[str] = set()
    prefix = f"{kind}/"
    workspace_root = workspace_root.resolve()

    try:
        sparse = SparseWorkspace(ctx.git, workspace_root)
        for pattern in sparse.read_patterns():
            value = str(pattern or "").strip()
            if not value.startswith(prefix):
                continue
            tail = value[len(prefix) :].strip().strip("/")
            if tail:
                names.add(tail)
    except Exception:
        pass

    try:
        kind_root = workspace_root / kind
        if kind_root.exists():
            for child in kind_root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    names.add(child.name)
    except Exception:
        pass

    return sorted(names)


def effective_registry_names(ctx, registry_names: list[str], workspace_root: Path, kind: str) -> tuple[list[str], bool]:
    names = sorted(set(str(name) for name in (registry_names or []) if str(name).strip()))
    if names:
        return names, False
    fallback = workspace_kind_names(ctx, workspace_root, kind)
    if fallback:
        return fallback, True
    return [], False


def reconcile_workspace_db_to_materialized(ctx) -> dict[str, Any]:
    workspace_root = Path(ctx.paths.workspace_dir())
    payload = rebuild_workspace_registry(workspace_root)

    skill_entries = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    scenario_entries = payload.get("scenarios") if isinstance(payload.get("scenarios"), list) else []

    skill_registry = SqliteSkillRegistry(ctx.sql)
    scenario_registry = SqliteScenarioRegistry(ctx.sql)

    current_skills = {str(row.name or "").strip(): row for row in skill_registry.list() if str(getattr(row, "name", "") or "").strip()}
    current_scenarios = {
        str(row.name or "").strip(): row
        for row in scenario_registry.list()
        if str(getattr(row, "name", "") or "").strip()
    }

    materialized_skills: dict[str, dict[str, Any]] = {}
    for entry in skill_entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or "").strip()
        if name:
            materialized_skills[name] = dict(entry)

    materialized_scenarios: dict[str, dict[str, Any]] = {}
    for entry in scenario_entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or "").strip()
        if name:
            materialized_scenarios[name] = dict(entry)

    for name, entry in materialized_skills.items():
        skill_registry.register(name, active_version=str(entry.get("version") or "").strip() or None)
    for name in sorted(set(current_skills) - set(materialized_skills)):
        skill_registry.unregister(name)

    for name, entry in materialized_scenarios.items():
        scenario_registry.register(name, active_version=str(entry.get("version") or "").strip() or None)
    for name in sorted(set(current_scenarios) - set(materialized_scenarios)):
        scenario_registry.unregister(name)

    return {
        "ok": True,
        "skills": sorted(materialized_skills),
        "scenarios": sorted(materialized_scenarios),
        "skills_removed": sorted(set(current_skills) - set(materialized_skills)),
        "scenarios_removed": sorted(set(current_scenarios) - set(materialized_scenarios)),
        "registry_updated_at": payload.get("updated_at"),
    }


def sync_workspace_sparse_to_registry(ctx) -> dict[str, Any]:
    """
    Skills and scenarios share the same workspace monorepo checkout; sparse
    patterns must be applied as a union, otherwise one sync overwrites the other.
    """

    workspace_root = ctx.paths.workspace_dir()
    skill_rows = SqliteSkillRegistry(ctx.sql).list()
    scenario_rows = SqliteScenarioRegistry(ctx.sql).list()
    registry_skills = installed_names(skill_rows)
    registry_scenarios = installed_names(scenario_rows)
    skills, skills_fallback = effective_registry_names(ctx, registry_skills, workspace_root, "skills")
    scenarios, scenarios_fallback = effective_registry_names(ctx, registry_scenarios, workspace_root, "scenarios")
    desired = registry_pattern_set([*(f"skills/{n}" for n in skills), *(f"scenarios/{n}" for n in scenarios)])
    fallback_used: dict[str, list[str]] = {}
    if skills_fallback:
        fallback_used["skills"] = skills
    if scenarios_fallback:
        fallback_used["scenarios"] = scenarios

    try:
        from adaos.services.git.availability import get_git_availability

        av = get_git_availability(base_dir=ctx.settings.base_dir)
    except Exception:
        av = None

    if av is not None and not av.enabled:
        errors: list[str] = []
        for name in skills:
            try:
                ctx.skills_repo.install(name)
            except Exception as exc:
                errors.append(f"skills/{name}: {exc}")
        for name in scenarios:
            try:
                ctx.scenarios_repo.install(name)
            except Exception as exc:
                errors.append(f"scenarios/{name}: {exc}")
        return {
            "ok": len(errors) == 0,
            "mode": "archive",
            "skills": skills,
            "scenarios": scenarios,
            "registry_skills": registry_skills,
            "registry_scenarios": registry_scenarios,
            "fallback_used": fallback_used,
            "errors": errors,
            "patterns": desired,
        }

    sparse = SparseWorkspace(ctx.git, workspace_root)
    current = sparse.read_patterns()
    to_remove = [pattern for pattern in current if pattern not in desired]

    ensure_clean(ctx.git, str(workspace_root), desired)
    sparse.update(add=desired, remove=to_remove)
    try:
        ctx.git.pull(str(workspace_root))
    except Exception as exc:
        return {
            "ok": False,
            "skills": skills,
            "scenarios": scenarios,
            "registry_skills": registry_skills,
            "registry_scenarios": registry_scenarios,
            "fallback_used": fallback_used,
            "error": str(exc),
            "patterns": desired,
        }

    return {
        "ok": True,
        "skills": skills,
        "scenarios": scenarios,
        "registry_skills": registry_skills,
        "registry_scenarios": registry_scenarios,
        "fallback_used": fallback_used,
        "patterns": desired,
    }


__all__ = [
    "effective_registry_names",
    "installed_names",
    "reconcile_workspace_db_to_materialized",
    "sync_workspace_sparse_to_registry",
    "workspace_kind_names",
]
