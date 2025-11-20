"""Scenario management helpers exposed via the SDK."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from adaos.adapters.db import SqliteScenarioRegistry
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.sdk.core.decorators import tool
from adaos.services.scenario.manager import ScenarioManager

from .common import (
    SCHEMA_RESULT_ENVELOPE,
    _load_request,
    _require_cap,
    _result,
    _safe_ns_key,
    _save_request,
)

__all__ = [
    "create",
    "install",
    "uninstall",
    "pull",
    "push",
    "list_installed",
    "delete",
    "read_proto",
    "write_proto",
    "read_bindings",
    "write_bindings",
    "toggle",
    "set_binding",
]


def _manager(ctx: Any) -> ScenarioManager:
    repo = getattr(ctx, "scenarios_repo", None)
    if repo is None:
        repo = GitScenarioRepository(
            paths=ctx.paths,
            git=ctx.git,
            url=getattr(ctx.settings, "scenarios_monorepo_url", None),
            branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
        )
    registry = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=registry, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _repo(ctx: Any) -> GitScenarioRepository:
    repo = getattr(ctx, "scenarios_repo", None)
    if repo is not None:
        return repo
    return GitScenarioRepository(
        paths=ctx.paths,
        git=ctx.git,
        url=getattr(ctx.settings, "scenarios_monorepo_url", None),
        branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
    )


def _workspace_root(ctx: Any) -> Path:
    attr = getattr(ctx.paths, "scenarios_workspace_dir", None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "scenarios_dir")
        value = base() if callable(base) else base
    return Path(value)


def _cache_root(ctx: Any) -> Path:
    attr = getattr(ctx.paths, "scenarios_cache_dir", None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "scenarios_dir")
        value = base() if callable(base) else base
    return Path(value)


def _scenario_dir(ctx: Any, scenario_id: str) -> Path:
    return _workspace_root(ctx) / scenario_id


@tool(
    "manage.scenarios.create",
    summary="create a scenario from a template",
    stability="experimental",
    examples=["manage.scenarios.create('demo')"],
)
def create(scenario_id: str, template: str = "template") -> str:
    ctx = _require_cap("scenarios.manage")
    repo = _repo(ctx)
    repo.ensure()
    template_dir = ctx.paths.scenario_templates_dir() / template
    if not template_dir.exists():
        raise FileNotFoundError(f"template '{template}' not found")
    dest = _scenario_dir(ctx, scenario_id)
    if dest.exists():
        raise FileExistsError(f"scenario '{scenario_id}' already exists")
    shutil.copytree(template_dir, dest)
    return str(dest.resolve())


@tool(
    "manage.scenarios.install",
    summary="install scenario into workspace",
    stability="stable",
    examples=["manage.scenarios.install('greet_on_boot')"],
)
def install(scenario_id: str) -> str:
    ctx = _require_cap("scenarios.manage")
    mgr = _manager(ctx)
    meta = mgr.install(scenario_id)
    return str(getattr(meta, "path", _scenario_dir(ctx, scenario_id)))


@tool(
    "manage.scenarios.uninstall",
    summary="remove an installed scenario",
    stability="stable",
    examples=["manage.scenarios.uninstall('greet_on_boot')"],
)
def uninstall(scenario_id: str) -> str:
    ctx = _require_cap("scenarios.manage")
    mgr = _manager(ctx)
    mgr.remove(scenario_id)
    return scenario_id


@tool(
    "manage.scenarios.pull",
    summary="pull scenario sources",
    stability="experimental",
)
def pull(scenario_id: str) -> str:
    ctx = _require_cap("scenarios.manage")
    repo = _repo(ctx)
    repo.ensure()
    cache_root = _cache_root(ctx)
    if hasattr(ctx.git, "sparse_add"):
        try:
            ctx.git.sparse_add(str(cache_root), f"scenarios/{scenario_id}")
        except Exception:
            pass
    if hasattr(ctx.git, "pull"):
        ctx.git.pull(str(cache_root))
    return scenario_id


@tool(
    "manage.scenarios.push",
    summary="push local scenario changes",
    stability="experimental",
)
def push(scenario_id: str, message: Optional[str] = None, signoff: bool = False) -> str:
    ctx = _require_cap("scenarios.manage")
    mgr = _manager(ctx)
    msg = message or f"update scenario {scenario_id}"
    return mgr.push(scenario_id, msg, signoff=signoff)


@tool(
    "manage.scenarios.list",
    summary="list installed scenarios",
    stability="stable",
)
def list_installed() -> list[str]:
    ctx = _require_cap("scenarios.manage")
    mgr = _manager(ctx)
    records = mgr.list_installed()
    result: list[str] = []
    for rec in records or []:
        name = getattr(rec, "name", None)
        if name:
            result.append(name)
    return result


@tool(
    "manage.scenarios.delete",
    summary="delete scenario directory",
    stability="experimental",
)
def delete(scenario_id: str) -> bool:
    ctx = _require_cap("scenarios.manage")
    target = _scenario_dir(ctx, scenario_id)
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


@tool(
    "manage.scenarios.read_proto",
    summary="read scenario manifest",
    stability="experimental",
)
def read_proto(scenario_id: str) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    path = _scenario_dir(ctx, scenario_id) / "scenario.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@tool(
    "manage.scenarios.write_proto",
    summary="write scenario manifest",
    stability="experimental",
)
def write_proto(scenario_id: str, data: Mapping[str, Any]) -> str:
    ctx = _require_cap("scenarios.manage")
    dest = _scenario_dir(ctx, scenario_id) / "scenario.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return str(dest)


@tool(
    "manage.scenarios.read_bindings",
    summary="read scenario bindings for a user",
    stability="experimental",
)
def read_bindings(scenario_id: str, user: str) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    path = _scenario_dir(ctx, scenario_id) / "bindings" / f"{user}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@tool(
    "manage.scenarios.write_bindings",
    summary="write scenario bindings for a user",
    stability="experimental",
)
def write_bindings(scenario_id: str, user: str, data: Mapping[str, Any]) -> str:
    ctx = _require_cap("scenarios.manage")
    path = _scenario_dir(ctx, scenario_id) / "bindings" / f"{user}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


_TOGGLE_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "scenario": {"type": "string", "minLength": 1},
        "enabled": {"type": "boolean"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "scenario", "enabled"],
    "additionalProperties": True,
}

_BIND_INPUT = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "scenario": {"type": "string", "minLength": 1},
        "binding": {"type": "object"},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["request_id", "scenario", "binding"],
    "additionalProperties": True,
}


@tool(
    "manage.scenarios.toggle",
    summary="enable or disable a scenario",
    stability="stable",
    idempotent=True,
    examples=["manage.scenarios.toggle(request_id='req-10', scenario='onboarding', enabled=True)"],
    input_schema=_TOGGLE_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def toggle(request_id: str, scenario: str, *, enabled: bool, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    namespace = _safe_ns_key("scenarios", scenario, "toggle")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    if not dry_run:
        key = _safe_ns_key("scenarios", scenario, "enabled")
        ctx.kv.set(key, bool(enabled))
    envelope = _result(
        ctx,
        request_id,
        "dry-run" if dry_run else "ok",
        dry_run=dry_run,
        result={"scenario": scenario, "enabled": enabled},
    )
    return _save_request(ctx, namespace, request_id, envelope)


@tool(
    "manage.scenarios.bind.set",
    summary="set scenario binding metadata",
    stability="experimental",
    idempotent=True,
    examples=["manage.scenarios.bind.set(request_id='req-11', scenario='onboarding', binding={'skill': 'welcome'})"],
    input_schema=_BIND_INPUT,
    output_schema=SCHEMA_RESULT_ENVELOPE,
)
def set_binding(request_id: str, scenario: str, binding: Mapping[str, Any], *, dry_run: bool = False) -> Mapping[str, Any]:
    ctx = _require_cap("scenarios.manage")
    namespace = _safe_ns_key("scenarios", scenario, "binding")

    cached = _load_request(ctx, namespace, request_id)
    if cached is not None:
        return dict(cached)

    if not dry_run:
        key = _safe_ns_key("scenarios", scenario, "binding")
        ctx.kv.set(key, dict(binding))
    envelope = _result(
        ctx,
        request_id,
        "dry-run" if dry_run else "ok",
        dry_run=dry_run,
        result={"scenario": scenario, "binding": dict(binding)},
    )
    return _save_request(ctx, namespace, request_id, envelope)
