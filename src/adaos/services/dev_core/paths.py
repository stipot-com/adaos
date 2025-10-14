"""Helpers for resolving developer workspace paths."""

from __future__ import annotations

import os
from pathlib import Path
from adaos.services.agent_context import get_ctx
from adaos.services.fs.safe_io import ensure_dir
from adaos.services.node_config import NodeConfig, load_node

from .types import DevContext, Kind


def _ctx_node_config() -> NodeConfig:
    return load_node()


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def dev_root(ctx: DevContext) -> str:
    cfg = _ctx_node_config()
    subnet = ctx.subnet_id or cfg.subnet_id_value
    base = cfg.dev_settings.workspace or "~/.adaos/dev"
    root = _expand(base) / subnet
    ensure_tree(str(root))
    return str(root)


def dev_path(ctx: DevContext, kind: Kind, name: str) -> str:
    root = Path(dev_root(ctx))
    target = root / f"{kind}s" / name
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
