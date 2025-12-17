from __future__ import annotations

import asyncio
import json
import os
import traceback
from functools import wraps
from typing import Optional

import typer

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.autostart import default_spec as default_autostart_spec
from adaos.services.autostart import disable as autostart_disable
from adaos.services.autostart import enable as autostart_enable
from adaos.services.autostart import status as autostart_status
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import WebspaceScenarioRuntime
from adaos.services.setup.presets import get_preset
from adaos.services.skill.manager import SkillManager
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.webspace import default_webspace_id
from adaos.adapters.git.workspace import SparseWorkspace
from adaos.services.git.workspace_guard import ensure_clean


def _run_safe(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _scenario_mgr() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _skill_mgr() -> SkillManager:
    ctx = get_ctx()
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=ctx.skills_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)

def _installed_names(rows: list[object]) -> list[str]:
    names: list[str] = []
    for row in rows:
        if not bool(getattr(row, "installed", True)):
            continue
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name:
            continue
        names.append(str(name))
    return sorted(set(names))


def _sync_workspace_sparse_to_registry(ctx) -> dict:
    """
    Skills and scenarios share the same workspace monorepo checkout; sparse
    patterns must be applied as a union, otherwise one sync overwrites the other.
    """
    skill_rows = SqliteSkillRegistry(ctx.sql).list()
    scenario_rows = SqliteScenarioRegistry(ctx.sql).list()
    skills = _installed_names(skill_rows)
    scenarios = _installed_names(scenario_rows)
    desired = [*(f"skills/{n}" for n in skills), *(f"scenarios/{n}" for n in scenarios)]

    workspace_root = ctx.paths.workspace_dir()
    sparse = SparseWorkspace(ctx.git, workspace_root)
    current = sparse.read_patterns()
    to_remove = [p for p in current if p not in desired]

    ensure_clean(ctx.git, str(workspace_root), desired)
    sparse.update(add=desired, remove=to_remove)
    try:
        ctx.git.pull(str(workspace_root))
    except Exception as exc:
        return {"ok": False, "skills": skills, "scenarios": scenarios, "error": str(exc), "patterns": desired}

    return {"ok": True, "skills": skills, "scenarios": scenarios, "patterns": desired}


@_run_safe
def install(
    preset: str = typer.Option("default", "--preset", help="default | base"),
    webspace_id: Optional[str] = typer.Option(None, "--webspace", help="target webspace id (default: 'default')"),
    setup_skills: bool = typer.Option(False, "--setup", help="run skill setup hooks (may prompt / require IO)"),
    autostart: bool = typer.Option(False, "--autostart", help="enable OS autostart after install"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
) -> None:
    """
    Install default scenarios/skills into the local workspace.

    Assumes the runtime environment is already bootstrapped (e.g. via tools/bootstrap_uv.ps1).
    """
    ctx = get_ctx()
    target_webspace = webspace_id or default_webspace_id()
    chosen = get_preset(preset)

    scenario_mgr = _scenario_mgr()
    skill_mgr = _skill_mgr()

    installed = {"scenarios": [], "skills": [], "warnings": []}

    for scenario_id in chosen.scenarios:
        try:
            meta = scenario_mgr.install_with_deps(scenario_id, webspace_id=target_webspace)
            installed["scenarios"].append({"id": meta.id.value, "version": getattr(meta, "version", None)})
        except Exception as exc:
            installed["warnings"].append(f"scenario {scenario_id}: {exc}")

    for skill_id in chosen.skills:
        try:
            skill_mgr.install(skill_id, validate=False)
            runtime = None
            try:
                runtime = skill_mgr.prepare_runtime(skill_id, run_tests=False)
            except Exception:
                runtime = None
            version = getattr(runtime, "version", None) if runtime else None
            slot = getattr(runtime, "slot", None) if runtime else None
            skill_mgr.activate_for_space(skill_id, version=version, slot=slot, space="default", webspace_id=target_webspace)
            if setup_skills:
                try:
                    skill_mgr.setup_skill(skill_id)
                except Exception as exc:
                    installed["warnings"].append(f"skill setup {skill_id}: {exc}")
            installed["skills"].append({"id": skill_id, "version": version, "slot": slot})
        except Exception as exc:
            installed["warnings"].append(f"skill {skill_id}: {exc}")

    # Ensure the webspace has at least some UI/application seeded.
    try:
        default_scenario = chosen.scenarios[0] if chosen.scenarios else "web_desktop"
        ystore = get_ystore_for_webspace(target_webspace)
        asyncio.run(ensure_webspace_seeded_from_scenario(ystore, target_webspace, default_scenario_id=default_scenario))
    except Exception as exc:
        installed["warnings"].append(f"webspace seed: {exc}")
    # CLI does not necessarily have runtime event subscribers loaded; rebuild
    # effective UI explicitly so ui.application/data.catalog are populated.
    try:
        asyncio.run(WebspaceScenarioRuntime(ctx).rebuild_webspace_async(target_webspace))
    except Exception as exc:
        installed["warnings"].append(f"webspace rebuild: {exc}")

    if autostart:
        try:
            spec = default_autostart_spec(ctx)
            res = autostart_enable(ctx, spec)
            installed["autostart"] = res
        except Exception as exc:
            installed["warnings"].append(f"autostart enable: {exc}")

    if json_output:
        typer.echo(json.dumps(installed, ensure_ascii=False, indent=2))
        return

    typer.secho(f"[AdaOS] installed preset '{chosen.name}' into webspace '{target_webspace}'", fg=typer.colors.GREEN)
    for item in installed["scenarios"]:
        typer.echo(f"scenario: {item['id']} ({item.get('version') or 'unknown'})")
    for item in installed["skills"]:
        slot = item.get("slot") or "n/a"
        typer.echo(f"skill: {item['id']} (slot {slot})")
    if installed["warnings"]:
        typer.secho("warnings:", fg=typer.colors.YELLOW)
        for w in installed["warnings"]:
            typer.echo(f"  - {w}")


@_run_safe
def update(
    pull: bool = typer.Option(True, "--pull/--no-pull", help="pull latest scenario/skill sources from git"),
    sync_yjs: bool = typer.Option(True, "--sync-yjs/--no-sync-yjs", help="re-project installed scenarios into Yjs webspace"),
    webspace_id: Optional[str] = typer.Option(None, "--webspace", help="target webspace id (default: 'default')"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
) -> None:
    """Update installed scenarios/skills and refresh runtime slots from workspace sources."""
    ctx = get_ctx()
    target_webspace = webspace_id or default_webspace_id()
    scenario_mgr = _scenario_mgr()
    skill_mgr = _skill_mgr()
    out: dict = {"pulled": {}, "runtime_updated": [], "yjs_synced": [], "warnings": []}

    if pull:
        try:
            # Apply sparse-checkout union once, then pull once.
            res = _sync_workspace_sparse_to_registry(ctx)
            out["pulled"]["workspace"] = bool(res.get("ok"))
            out["pulled"]["skills"] = bool(res.get("ok"))
            out["pulled"]["scenarios"] = bool(res.get("ok"))
            if not res.get("ok"):
                out["warnings"].append(f"workspace pull: {res.get('error')}")
        except Exception as exc:
            out["warnings"].append(f"workspace pull: {exc}")
            out["pulled"]["workspace"] = False
            out["pulled"]["skills"] = False
            out["pulled"]["scenarios"] = False

    # Refresh runtime slots (keeps slot/version, syncs files + tools).
    try:
        skill_rows = SqliteSkillRegistry(ctx.sql).list()
    except Exception:
        skill_rows = []
    for row in skill_rows:
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name or not bool(getattr(row, "installed", True)):
            continue
        try:
            res = skill_mgr.runtime_update(str(name), space="workspace")
            out["runtime_updated"].append({"skill": str(name), "ok": True, "result": res})
        except Exception as exc:
            out["runtime_updated"].append({"skill": str(name), "ok": False, "error": str(exc)})

    if sync_yjs:
        try:
            scenario_rows = SqliteScenarioRegistry(ctx.sql).list()
        except Exception:
            scenario_rows = []
        for row in scenario_rows:
            name = getattr(row, "name", None) or getattr(row, "id", None)
            if not name or not bool(getattr(row, "installed", True)):
                continue
            try:
                scenario_mgr.sync_to_yjs(str(name), webspace_id=target_webspace)
                out["yjs_synced"].append({"scenario": str(name), "ok": True})
            except Exception as exc:
                out["yjs_synced"].append({"scenario": str(name), "ok": False, "error": str(exc)})
        # Same as install(): do not rely on event bus subscriptions in CLI.
        try:
            asyncio.run(WebspaceScenarioRuntime(ctx).rebuild_webspace_async(target_webspace))
        except Exception as exc:
            out["warnings"].append(f"webspace rebuild: {exc}")

    if json_output:
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if not out["warnings"] else 1)

    typer.secho("[AdaOS] update complete", fg=typer.colors.GREEN)
    if out["warnings"]:
        typer.secho("warnings:", fg=typer.colors.YELLOW)
        for w in out["warnings"]:
            typer.echo(f"  - {w}")


autostart_app = typer.Typer(help="OS autostart integration (Windows Task / systemd --user / launchd)")


@autostart_app.command("status")
@_run_safe
def autostart_status_cmd(json_output: bool = typer.Option(False, "--json", help=_("cli.option.json"))):
    ctx = get_ctx()
    s = autostart_status(ctx)
    if json_output:
        typer.echo(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        enabled = s.get("enabled")
        active = s.get("active")
        typer.echo(f"enabled: {enabled}" + (f", active: {active}" if active is not None else ""))
        if "service" in s:
            typer.echo(f"service: {s['service']}")
        if "task" in s:
            typer.echo(f"task: {s['task']}")
        if "plist" in s:
            typer.echo(f"plist: {s['plist']}")
        if "wrapper" in s:
            typer.echo(f"wrapper: {s['wrapper']}")


@autostart_app.command("enable")
@_run_safe
def autostart_enable_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    token: Optional[str] = typer.Option(None, "--token", help="X-AdaOS-Token (stored in service environment)"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    ctx = get_ctx()
    spec = default_autostart_spec(ctx, host=host, port=port, token=token)
    res = autostart_enable(ctx, spec)
    if json_output:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        typer.secho("[AdaOS] autostart enabled", fg=typer.colors.GREEN)
        for k in ("task", "service", "plist", "wrapper"):
            if k in res:
                typer.echo(f"{k}: {res[k]}")


@autostart_app.command("disable")
@_run_safe
def autostart_disable_cmd(json_output: bool = typer.Option(False, "--json", help=_("cli.option.json"))):
    ctx = get_ctx()
    res = autostart_disable(ctx)
    if json_output:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        typer.secho("[AdaOS] autostart disabled", fg=typer.colors.GREEN)
