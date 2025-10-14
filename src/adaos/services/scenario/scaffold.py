from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit

_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-/]+$")


def _safe_subdir(root: Path, name: str) -> Path:
    """Ensure the target directory lives inside the scenarios root."""

    target = (root / name).resolve()
    root = root.resolve()
    try:
        target.relative_to(root)
    except Exception as exc:  # pragma: no cover - defensive path traversal guard
        raise ValueError("unsafe path traversal") from exc
    return target


def _resolve_template_dir(template: str) -> Path:
    """Locate a scenario template shipped with AdaOS or alongside sources."""

    try:
        import importlib.resources as ir
        import adaos.scenario_templates as templates_pkg

        res = ir.files(templates_pkg) / template
        with ir.as_file(res) as fp:
            template_path = Path(fp)
            if template_path.exists():
                return template_path
    except Exception:
        pass

    here = Path(__file__).resolve()
    candidates: list[Path] = []
    if len(here.parents) >= 3:
        candidates.append(here.parents[2] / "scenario_templates" / template)
    if len(here.parents) >= 4:
        candidates.append(here.parents[3] / "src" / "adaos" / "scenario_templates" / template)
    candidates.append(Path.cwd() / "src" / "adaos" / "scenario_templates" / template)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"template '{template}' not found; tried package resource and: "
        + ", ".join(str(c) for c in candidates if c)
    )


def _write_default_manifest(target: Path, scenario_id: str, version: str) -> None:
    manifest = target / "scenario.yaml"
    if manifest.exists():
        return

    content = "\n".join(
        [
            "# yaml-language-server: $schema=../../../src/adaos/abi/scenario.schema.json",
            f"id: {scenario_id}",
            f"version: \"{version}\"",
            "trigger: manual",
            "vars: {}",
            "steps: []",
            "",
        ]
    )
    manifest.write_text(content, encoding="utf-8")


def _patch_json_manifest(target: Path, scenario_id: str, version: str) -> None:
    manifest = target / "scenario.json"
    if not manifest.exists():
        return

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return

    payload["id"] = scenario_id
    payload.setdefault("version", version)
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


def _maybe_init_repo(root: Path, url: Optional[str], branch: Optional[str]) -> bool:
    ctx = get_ctx()
    if not url or (root / ".git").exists():
        return (root / ".git").exists()
    git = getattr(ctx, "git", None)
    if git is None or not hasattr(git, "ensure_repo"):
        return False
    try:
        git.ensure_repo(str(root), url, branch=branch)
    except Exception as exc:  # pragma: no cover - logging via event bus
        emit(
            ctx.bus,
            "scenario.warn",
            {"warn": f"ensure_repo_failed: {exc!r}", "id": url},
            "scenario.scaffold",
        )
        return False
    return (root / ".git").exists()


def create(
    name: str,
    template: str = "template",
    *,
    register: bool = True,
    push: bool = False,
    version: str = "0.1.0",
) -> Path:
    """
    Deprecated. Moved to adaos dev skill create
    Create a scenario skeleton from a template inside the monorepo.
    """

    if not _NAME_RE.match(name):
        raise ValueError("invalid scenario name")

    ctx = get_ctx()
    scenarios_root = Path(ctx.paths.scenarios_dir())
    scenarios_root.mkdir(parents=True, exist_ok=True)

    repo_ready = _maybe_init_repo(
        scenarios_root, getattr(ctx.settings, "scenarios_monorepo_url", None), getattr(ctx.settings, "scenarios_monorepo_branch", None)
    )

    target = _safe_subdir(scenarios_root, name)
    if target.exists():
        raise FileExistsError(f"scenario '{name}' already exists at {target}")

    template_root = _resolve_template_dir(template)
    shutil.copytree(template_root, target)

    _write_default_manifest(target, name, version)
    _patch_json_manifest(target, name, version)

    registry: Optional[SqliteScenarioRegistry] = None
    if register:
        try:
            registry = SqliteScenarioRegistry(ctx.sql)
            if not registry.get(name):
                registry.register(name)
        except Exception:  # pragma: no cover - registry is optional in early setups
            registry = None

    emit(ctx.bus, "scenario.created", {"name": name, "template": template}, "scenario.scaffold")

    is_testing = os.getenv("ADAOS_TESTING") == "1"
    if repo_ready and not is_testing and registry is not None:
        git = getattr(ctx, "git", None)
        if git and hasattr(git, "sparse_init") and hasattr(git, "sparse_set"):
            try:
                names = [r.name for r in registry.list()]
                if name not in names:
                    names.append(name)
                git.sparse_init(str(scenarios_root), cone=False)
                if names:
                    git.sparse_set(str(scenarios_root), names, no_cone=True)
            except Exception as exc:  # pragma: no cover
                emit(
                    ctx.bus,
                    "scenario.warn",
                    {"warn": f"sparse_set_failed: {exc!r}", "id": name},
                    "scenario.scaffold",
                )

    if push and repo_ready and not is_testing:
        git = getattr(ctx, "git", None)
        if git and hasattr(git, "commit_subpath"):
            message = f"feat(scenario): init {name} from template {template}"
            try:
                sha = git.commit_subpath(
                    str(scenarios_root),
                    subpath=name,
                    message=message,
                    author_name=ctx.settings.git_author_name,
                    author_email=ctx.settings.git_author_email,
                    signoff=False,
                )
                if sha != "nothing-to-commit" and hasattr(git, "push"):
                    git.push(str(scenarios_root))
                emit(ctx.bus, "scenario.pushed", {"name": name, "sha": sha}, "scenario.scaffold")
            except Exception as exc:  # pragma: no cover
                emit(
                    ctx.bus,
                    "scenario.warn",
                    {"warn": f"push_failed: {exc!r}", "id": name},
                    "scenario.scaffold",
                )

    return target


__all__ = ["create"]
