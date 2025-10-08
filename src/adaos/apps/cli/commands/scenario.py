"""Typer commands for managing and executing scenarios."""

from __future__ import annotations

import json
import os
import subprocess
import traceback
from pathlib import Path
from typing import Optional

import typer

from adaos.adapters.db import SqliteScenarioRegistry
from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.scaffold import create as scaffold_create
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context, load_scenario

app = typer.Typer(help=_("cli.help_scenario"))


def _run_safe(func):
    """Wrap Typer callbacks to surface tracebacks when ADAOS_CLI_DEBUG=1."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _mgr() -> ScenarioManager:
    ctx = get_ctx()
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _workspace_root() -> Path:
    ctx = get_ctx()
    attr = getattr(ctx.paths, "scenarios_workspace_dir", None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "scenarios_dir")
        value = base() if callable(base) else base
    return Path(value).expanduser().resolve()


@_run_safe
@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
):
    """List installed scenarios from the registry."""

    mgr = _mgr()
    rows = mgr.list_installed()

    if json_output:
        payload = {
            "scenarios": [
                {
                    "name": r.name,
                    "version": getattr(r, "active_version", None) or "unknown",
                }
                for r in rows
                if bool(getattr(r, "installed", True))
            ]
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        typer.echo(_("cli.scenario.list.empty"))
    else:
        for r in rows:
            if not bool(getattr(r, "installed", True)):
                continue
            version = getattr(r, "active_version", None) or "unknown"
            typer.echo(_("cli.scenario.list.item", name=r.name, version=version))

    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows if bool(getattr(r, "installed", True))}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(_("cli.scenario.fs_missing", items=", ".join(sorted(missing))))
        if extra:
            typer.echo(_("cli.scenario.fs_extra", items=", ".join(sorted(extra))))


@_run_safe
@app.command("sync")
def sync_cmd():
    """Apply sparse checkout for scenarios and pull the repository."""

    mgr = _mgr()
    mgr.sync()
    typer.echo(_("cli.scenario.sync.done"))


@_run_safe
@app.command("install")
def install_cmd(
    name: str = typer.Argument(..., help=_("cli.scenario.install.name_help")),
    pin: Optional[str] = typer.Option(None, "--pin", help=_("cli.scenario.install.pin_help")),
):
    """Install a scenario into the workspace monorepo."""

    mgr = _mgr()
    meta = mgr.install(name, pin=pin)
    typer.echo(_("cli.scenario.install.done", name=meta.id.value, version=meta.version, path=meta.path))


@_run_safe
@app.command("create")
def create_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.create.name_help")),
    template: str = typer.Option("template", "--template", "-t", help=_("cli.scenario.create.template_help")),
):
    """Create a new scenario scaffold from a template."""

    path = scaffold_create(scenario_id, template=template)
    typer.echo(_("cli.scenario.create.created", path=path))


@_run_safe
@app.command("uninstall")
def uninstall_cmd(name: str = typer.Argument(..., help=_("cli.scenario.uninstall.name_help"))):
    """Uninstall a scenario by removing it from registry and sparse checkout."""

    mgr = _mgr()
    mgr.uninstall(name)
    typer.echo(_("cli.scenario.uninstall.done", name=name))


@_run_safe
@app.command("push")
def push_command(
    scenario_name: str = typer.Argument(..., help=_("cli.scenario.push.name_help")),
    message: Optional[str] = typer.Option(None, "--message", "-m", help=_("cli.commit_message.help")),
    signoff: bool = typer.Option(False, "--signoff", help=_("cli.option.signoff")),
):
    """Commit changes inside a scenario directory and push to remote."""

    if message is None:
        typer.secho(
            "Root publishing via 'adaos scenario push' has moved to 'adaos dev scenario push'.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Use --message/-m to push commits or run 'adaos dev scenario push <name>'.")
        raise typer.Exit(1)

    mgr = _mgr()
    result = mgr.push(scenario_name, message, signoff=signoff)
    if result in {"nothing-to-push", "nothing-to-commit"}:
        typer.echo(_("cli.scenario.push.nothing"))
    else:
        typer.echo(_("cli.scenario.push.done", name=scenario_name, revision=result))

@_run_safe
@app.command("run")
def run_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.run.name_help")),
    path: Optional[str] = typer.Option(None, "--path", help=_("cli.scenario.run.path_help")),
) -> None:
    scenario_path = _scenario_path(scenario_id, path)
    ensure_runtime_context(_base_dir_for(scenario_path))
    runtime = ScenarioRuntime()
    result = runtime.run_from_file(str(scenario_path))
    meta = result.get("meta") or {}
    log_file = meta.get("log_file")
    typer.secho(_("cli.scenario.run.success", scenario_id=scenario_id), fg=typer.colors.GREEN)
    if log_file:
        typer.echo(_("cli.scenario.run.log", path=log_file))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@_run_safe
@app.command("validate")
def validate_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.validate.name_help")),
    path: Optional[str] = typer.Option(None, "--path", help=_("cli.scenario.validate.path_help")),
) -> None:
    scenario_path = _scenario_path(scenario_id, path)
    model = load_scenario(scenario_path)
    runtime = ScenarioRuntime()
    errors = runtime.validate(model)
    if errors:
        typer.secho(_("cli.scenario.validate.errors"), fg=typer.colors.RED)
        for err in errors:
            typer.echo(_("cli.scenario.validate.error_item", error=str(err)))
        raise typer.Exit(code=1)
    typer.secho(_("cli.scenario.validate.success", scenario_id=scenario_id), fg=typer.colors.GREEN)


def _collect_scenario_tests(scenario_id: Optional[str]) -> list[Path]:
    root = _scenario_root()
    tests: list[Path] = []
    if not root.exists():
        return tests
    if scenario_id:
        candidates = [root / scenario_id / "tests"]
    else:
        candidates = [p / "tests" for p in root.iterdir() if p.is_dir()]
    for tests_dir in candidates:
        if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
            tests.append(tests_dir)
    return tests


@_run_safe
@app.command("test")
def test_cmd(
    scenario_id: Optional[str] = typer.Argument(None, help=_("cli.scenario.test.name_help")),
    extra: Optional[str] = typer.Option(None, "--pytest-args", help=_("cli.scenario.test.extra_help")),
) -> None:
    tests = _collect_scenario_tests(scenario_id)
    if not tests:
        typer.secho(_("cli.scenario.test.none"), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    args = ["pytest", "-q", *[str(p) for p in tests]]
    if extra:
        args.extend(extra.split())

    command = " ".join(args)
    typer.echo(_("cli.scenario.test.running", command=command))
    result = subprocess.run(args, text=True)
    raise typer.Exit(code=result.returncode)


__all__ = ["app"]
