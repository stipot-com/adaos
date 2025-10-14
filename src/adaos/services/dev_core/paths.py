"""Helpers for resolving developer workspace paths."""

from __future__ import annotations

from pathlib import Path

from adaos.services.agent_context import get_ctx
from adaos.services.fs.safe_io import ensure_dir
from adaos.services.node_config import load_node

from .types import DevContext, Kind


def _resolve_subnet(ctx: DevContext) -> str:
    if ctx.subnet_id:
        return ctx.subnet_id
    node = load_node()
    return node.subnet_id_value


def dev_root(ctx: DevContext) -> str:
    agent = get_ctx()
    subnet = _resolve_subnet(ctx)
    root = agent.paths.dev_root_dir(subnet)
    ensure_tree(str(root))
    return str(root)


def dev_kind_root(ctx: DevContext, kind: Kind) -> str:
    agent = get_ctx()
    subnet = _resolve_subnet(ctx)
    if kind == "skill":
        root = agent.paths.dev_skills_dir(subnet)
    else:
        root = agent.paths.dev_scenarios_dir(subnet)
    ensure_tree(str(root))
    return str(root)


def dev_path(ctx: DevContext, kind: Kind, name: str) -> str:
    agent = get_ctx()
    subnet = _resolve_subnet(ctx)
    if kind == "skill":
        target = agent.paths.dev_skill_path(subnet, name)
    else:
        target = agent.paths.dev_scenario_path(subnet, name)
    ensure_tree(str(target.parent))
    return str(target)


def installed_root(kind: Kind) -> str:
    ctx = get_ctx()
    paths = ctx.paths
    if kind == "skill":
        return str(Path(paths.skills_workspace_dir()).resolve())
    return str(Path(paths.scenarios_workspace_dir()).resolve())


def builtin_templates_root(kind: Kind) -> str:
    ctx = get_ctx()
    paths = ctx.paths
    if kind == "skill":
        return str(Path(paths.skill_templates_dir()).resolve())
    return str(Path(paths.scenario_templates_dir()).resolve())


def ensure_tree(path: str) -> str:
    ctx = get_ctx()
    ensure_dir(path, ctx.fs)
    return path
