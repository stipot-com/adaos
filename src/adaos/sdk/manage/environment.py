"""Environment preparation utilities for the CLI and SDK."""

from __future__ import annotations

import os
from pathlib import Path

from adaos.services.agent_context import get_ctx
from adaos.adapters.db import SqliteSkillRegistry
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.scenarios.git_repo import GitScenarioRepository

__all__ = ["prepare_environment"]


def prepare_environment() -> None:
    """Ensure base directories and registries exist for local usage."""

    ctx = get_ctx()

    skills_root = ctx.paths.skills_dir()
    scenarios_root = ctx.paths.scenarios_dir()
    skills_cache = Path(
        (ctx.paths.skills_cache_dir() if hasattr(ctx.paths, "skills_cache_dir") else ctx.paths.skills_dir())
    )
    scenarios_cache = Path(
        (ctx.paths.scenarios_cache_dir() if hasattr(ctx.paths, "scenarios_cache_dir") else ctx.paths.scenarios_dir())
    )
    state_root = ctx.paths.state_dir()
    cache_root = ctx.paths.cache_dir()
    logs_root = ctx.paths.logs_dir()

    for directory in (skills_root, scenarios_root, skills_cache, scenarios_cache, state_root, cache_root, logs_root):
        directory.mkdir(parents=True, exist_ok=True)

    SqliteSkillRegistry(ctx.sql)

    if os.getenv("ADAOS_TESTING") == "1":
        return

    if ctx.settings.skills_monorepo_url and not (skills_cache / ".git").exists():
        GitSkillRepository(
            paths=ctx.paths,
            git=ctx.git,
            monorepo_url=ctx.settings.skills_monorepo_url,
            monorepo_branch=ctx.settings.skills_monorepo_branch,
        ).ensure()

    if ctx.settings.scenarios_monorepo_url and not (scenarios_cache / ".git").exists():
        GitScenarioRepository(
            paths=ctx.paths,
            git=ctx.git,
            url=ctx.settings.scenarios_monorepo_url,
            branch=ctx.settings.scenarios_monorepo_branch,
        ).ensure()
