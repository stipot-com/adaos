# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations

import json
import os
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer

from adaos.sdk.data.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import RuntimeInstallResult, SkillManager
from adaos.services.skill.runtime import (
    SkillPrepError,
    SkillPrepMissingFunctionError,
    SkillPrepScriptNotFoundError,
    SkillRuntimeError,
    run_skill_handler_sync,
    run_skill_prep,
)
from adaos.services.skill.update import SkillUpdateService
from adaos.services.skill.validation import SkillValidationService
from adaos.services.skill.scaffold import create as scaffold_create
from adaos.adapters.db import SqliteSkillRegistry

app = typer.Typer(help=_("cli.help_skill"))


def _run_safe(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = ctx.skills_repo
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=getattr(ctx, "bus", None), caps=ctx.caps)


def _ensure_workspace_gitignore(workspace: Path) -> None:
    """Ensure that the shared workspace has a .gitignore with runtime exclusions."""

    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / ".gitignore"
    if target.exists():
        return

    entries = [
        "skills/.runtime",
        "skills/.devtime",
        "scenario/.runtime",
        "scenario/.devtime",
    ]
    target.write_text("\n".join(entries) + "\n", encoding="utf-8")


def _workspace_root() -> Path:
    ctx = get_ctx()
    attr = getattr(ctx.paths, "skills_workspace_dir", None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "skills_dir")
        value = base() if callable(base) else base
    root = Path(value).expanduser().resolve()
    workspace = root.parent if root.name.lower() == "skills" else root
    _ensure_workspace_gitignore(workspace)
    return root


def _resolve_skill_path(target: str) -> Path:
    candidate = Path(target).expanduser()
    if candidate.exists():
        return candidate.resolve()
    root = _workspace_root()
    candidate = (root / target).resolve()
    if candidate.exists():
        return candidate
    raise typer.BadParameter(_("cli.skill.push.not_found", name=target))


def _echo_runtime_install(result: RuntimeInstallResult) -> None:
    typer.secho(
        f"installed {result.name} v{result.version} into slot {result.slot}",
        fg=typer.colors.GREEN,
    )
    if result.tests:
        summary = ", ".join(f"{name}={out.status}" for name, out in result.tests.items())
        typer.echo(f"tests: {summary}")
    typer.echo(f"resolved manifest: {result.resolved_manifest}")


@_run_safe
@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
):
    """
    Список установленных навыков из реестра.
    JSON-формат: {"skills": [{"name": "...", "version": "..."}, ...]}
    """
    mgr = _mgr()
    rows = mgr.list_installed()  # SkillRecord[]

    if json_output:
        payload = {
            "skills": [
                {
                    "name": r.name,
                    # тестам важен только name, но version полезно оставить
                    "version": getattr(r, "active_version", None) or "unknown",
                }
                for r in rows
                # оставляем только действительно установленные (если поле есть)
                if bool(getattr(r, "installed", True))
            ]
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        typer.echo(_("skill.list.empty"))
    else:
        for r in rows:
            if not bool(getattr(r, "installed", True)):
                continue
            av = getattr(r, "active_version", None) or "unknown"
            typer.echo(_("cli.skill.list.item", name=r.name, version=av))

    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows if bool(getattr(r, "installed", True))}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(_("cli.skill.fs_missing", items=", ".join(sorted(missing))))
        if extra:
            typer.echo(_("cli.skill.fs_extra", items=", ".join(sorted(extra))))


@_run_safe
@app.command("sync")
def sync():
    """Deprecated: use ``adaos skill migrate`` instead."""
    typer.secho(
        "'skill sync' is deprecated. Use 'adaos skill migrate' to refresh skills.",
        fg=typer.colors.YELLOW,
    )


@_run_safe
@app.command("uninstall")
def uninstall(name: str):
    mgr = _mgr()
    mgr.uninstall(name)
    typer.echo(_("cli.skill.uninstall.done", name=name))


@_run_safe
@app.command("reconcile-fs-to-db")
def reconcile_fs_to_db():
    """Обходит {skills_dir} и проставляет installed=1 для найденных папок (кроме .git).
    Не трогает active_version/repo_url.
    """
    mgr = _mgr()
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    if not root.exists():
        typer.echo(_("cli.skill.reconcile.missing_root"))
        raise typer.Exit(1)
    found = []
    for name in os.listdir(root):
        if name == ".git":
            continue
        p = root / name
        if p.is_dir():
            mgr.reg.register(name)  # installed=1
            found.append(name)
    typer.echo(
        _(
            "cli.skill.reconcile.added",
            items=", ".join(found) if found else _("cli.skill.reconcile.empty"),
        )
    )


@_run_safe
@app.command("push")
def push_command(
    skill_name: str = typer.Argument(..., help=_("cli.skill.push.name_help")),
    message: Optional[str] = typer.Option(None, "--message", "-m", help=_("cli.commit_message.help")),
    signoff: bool = typer.Option(False, "--signoff", help=_("cli.option.signoff")),
):
    """
    Закоммитить изменения ТОЛЬКО внутри подпапки навыка и выполнить git push.
    Защищён политиками: skills.manage + git.write + net.git.
    """
    if message is None:
        typer.secho(
            "Root publishing via 'adaos skill push' has moved to 'adaos dev skill push'.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Use --message/-m to push commits or run 'adaos dev skill push <name>'.")
        raise typer.Exit(1)

    mgr = _mgr()
    res = mgr.push(skill_name, message, signoff=signoff)
    if res in {"nothing-to-push", "nothing-to-commit"}:
        typer.echo(_("cli.skill.push.nothing"))
    else:
        typer.echo(_("cli.skill.push.done", name=skill_name, revision=res))


@_run_safe
@app.command("create")
def cmd_create(name: str, template: str = typer.Option("demo_skill", "--template", "-t")):
    p = scaffold_create(name, template=template)
    typer.echo(_("cli.skill.create.created", path=p))
    typer.echo(_("cli.skill.create.hint_push", name=name))


@_run_safe
@app.command("scaffold")
def cmd_scaffold(name: str, template: str = typer.Option("demo_skill", "--template", help="skill template name")):
    path = scaffold_create(name, template=template)
    typer.secho(f"scaffold created at {path}", fg=typer.colors.GREEN)


@_run_safe
@app.command("validate")
def cmd_validate(
    name: str,
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="treat warnings as errors"),
    probe_tools: bool = typer.Option(False, "--probe-tools", help="import handlers to verify tool exports"),
):
    mgr = _mgr()
    try:
        report = mgr.validate_skill(name, strict=strict, probe_tools=probe_tools)
    except Exception as exc:
        typer.secho(f"validate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    issues = [asdict(issue) for issue in report.issues]
    if json_output:
        typer.echo(json.dumps({"ok": report.ok, "issues": issues}, ensure_ascii=False, indent=2))
        if not report.ok:
            raise typer.Exit(1)
        return

    if report.ok:
        typer.secho("validation passed", fg=typer.colors.GREEN)
        return

    for issue in report.issues:
        location = f" ({issue.where})" if issue.where else ""
        typer.echo(f"[{issue.level}] {issue.code}: {issue.message}{location}")
    raise typer.Exit(1)


@_run_safe
@app.command("install", help=_("cli.skill.install.help"))
def cmd_install(
    name: str,
    test: bool = typer.Option(False, "--test", help=_("cli.skill.install.option.test")),
    slot: Optional[str] = typer.Option(None, "--slot", help=_("cli.skill.install.option.slot")),
    silent: bool = typer.Option(False, "--silent", help=_("cli.skill.install.option.silent")),
):
    mgr = _mgr()
    try:
        result = mgr.install(name, validate=False)
    except Exception as exc:
        typer.secho(f"install failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if isinstance(result, tuple):
        meta, report = result
    elif hasattr(result, "id"):
        meta, report = result, None
    else:
        typer.echo(str(result))
        return

    if report is not None and hasattr(report, "ok") and not report.ok:
        typer.secho(str(report), fg=typer.colors.YELLOW)

    skill_name = meta.id.value if meta and hasattr(meta, "id") else name
    try:
        runtime = mgr.prepare_runtime(skill_name, run_tests=test, preferred_slot=slot)
    except Exception as exc:
        typer.secho(f"runtime preparation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    _echo_runtime_install(runtime)

    if silent:
        return

    try:
        setup_result = mgr.setup_skill(skill_name)
    except RuntimeError as exc:
        message = str(exc)
        if "setup not supported" in message.lower():
            typer.secho(_("cli.skill.install.setup_not_supported"), fg=typer.colors.YELLOW)
            return
        typer.secho(_("cli.skill.install.setup_failed", error=message), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except Exception as exc:
        typer.secho(_("cli.skill.install.setup_failed", error=str(exc)), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if isinstance(setup_result, dict):
        ok = setup_result.get("ok")
        if ok is False:
            detail = setup_result.get("error") or setup_result.get("message") or ""
            typer.secho(
                _("cli.skill.install.setup_report_failed", detail=str(detail)),
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        detail = setup_result.get("message") or setup_result.get("detail")
        if detail:
            typer.echo(_("cli.skill.install.setup_success_with_detail", detail=str(detail)))
            return

    typer.echo(_("cli.skill.install.setup_success"))


@_run_safe
@app.command("test", help=_("cli.skill.test.help"))
def cmd_test(
    name: str,
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    try:
        results = mgr.run_skill_tests(name)
    except Exception as exc:
        typer.secho(f"test failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(json.dumps({k: asdict(v) for k, v in results.items()}, ensure_ascii=False, indent=2))
        if any(res.status != "passed" for res in results.values()):
            raise typer.Exit(1)
        return

    if not results:
        typer.echo("no tests discovered")
        return

    failed = False
    for test_name, result in results.items():
        detail = f" ({result.detail})" if result.detail else ""
        typer.echo(f"{test_name}: {result.status}{detail}")
        if result.status != "passed":
            failed = True

    if failed:
        raise typer.Exit(1)

    typer.secho("tests passed", fg=typer.colors.GREEN)


@app.command("run-handler")
def run_handler(
    skill: str = typer.Argument(..., help=_("cli.skill.run.name_help")),
    topic: str = typer.Option("nlp.intent.weather.get", "--topic", "-t", help=_("cli.skill.run.topic_help")),
    payload: str = typer.Option("{}", "--payload", "-p", help=_("cli.skill.run.payload_help")),
):
    """Execute a skill handler locally using the configured workspace."""

    try:
        payload_obj = json.loads(payload) if payload else {}
        if not isinstance(payload_obj, dict):
            raise ValueError(_("cli.skill.run.payload_type_error"))
    except Exception as exc:
        raise typer.BadParameter(_("cli.skill.run.payload_invalid", error=str(exc)))

    try:
        result = run_skill_handler_sync(skill, topic, payload_obj)
    except SkillRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(_("cli.skill.run.success", result=repr(result)))


@app.command("run", help=_("cli.skill.run.help"))
def run_tool(
    name: str,
    tool: Optional[str] = typer.Argument(None, help=_("cli.skill.run.tool_help")),
    payload: str = typer.Option("{}", "--json", help=_("cli.skill.run.payload_cli_help")),
    timeout: Optional[float] = typer.Option(None, "--timeout", help=_("cli.skill.run.timeout_help")),
):
    try:
        payload_obj = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        typer.secho(f"invalid payload: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    mgr = _mgr()
    try:
        result = mgr.run_tool(name, tool, payload_obj, timeout=timeout)
    except Exception as exc:
        typer.secho(f"run failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(json.dumps(result, ensure_ascii=False))


@_run_safe
@app.command("setup", help=_("cli.skill.setup.help"))
def cmd_setup(
    name: str,
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    try:
        result = mgr.setup_skill(name)
    except Exception as exc:
        typer.secho(f"setup failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if isinstance(result, dict):
        payload = json.dumps(result, ensure_ascii=False)
        typer.echo(payload)
        if not result.get("ok", True):
            raise typer.Exit(1)
        return

    if json_output:
        typer.echo(json.dumps({"result": result}, ensure_ascii=False))
    elif result is None:
        typer.secho("setup completed", fg=typer.colors.GREEN)
    else:
        typer.echo(str(result))

@_run_safe
@app.command("activate")
def activate(name: str, slot: Optional[str] = typer.Option(None, "--slot"), version: Optional[str] = typer.Option(None, "--version")):
    mgr = _mgr()
    try:
        target = mgr.activate_runtime(name, version=version, slot=slot)
    except Exception as exc:
        typer.secho(f"activate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"skill {name} now active on slot {target}", fg=typer.colors.GREEN)


@_run_safe
@app.command("rollback")
def rollback(name: str):
    mgr = _mgr()
    try:
        slot = mgr.rollback_runtime(name)
    except Exception as exc:
        typer.secho(f"rollback failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"rolled back {name} to slot {slot}", fg=typer.colors.YELLOW)


@_run_safe
@app.command("status")
def status(name: str, json_output: bool = typer.Option(False, "--json", help=_("cli.option.json"))):
    mgr = _mgr()
    try:
        state = mgr.runtime_status(name)
    except Exception as exc:
        typer.secho(f"status failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(json.dumps(state, ensure_ascii=False, indent=2))
        return

    typer.echo(f"skill: {state['name']}")
    typer.echo(f"version: {state['version']}")
    typer.echo(f"active slot: {state['active_slot']}")
    if state.get("ready", True):
        typer.echo(f"resolved manifest: {state['resolved_manifest']}")
    else:
        typer.echo("resolved manifest: (not activated)")
        pending_slot = state.get("pending_slot")
        hint_slot = pending_slot or state.get("active_slot")
        activation_hint = f" --slot {pending_slot}" if pending_slot else ""
        typer.secho(
            f"slot {hint_slot} is prepared but inactive. run 'adaos skill activate {name}{activation_hint}'",
            fg=typer.colors.YELLOW,
        )
    tests = state.get("tests") or {}
    if tests:
        typer.echo("tests: " + ", ".join(f"{k}={v}" for k, v in tests.items()))
    default_tool = state.get("default_tool")
    if default_tool:
        typer.echo(f"default tool: {default_tool}")


@_run_safe
@app.command("gc")
def gc(name: Optional[str] = typer.Option(None, "--name", help="skill to clean")):
    mgr = _mgr()
    cleaned = mgr.gc_runtime(name)
    for skill, versions in cleaned.items():
        removed = ", ".join(versions) if versions else "nothing"
        typer.echo(f"gc {skill}: removed {removed}")


@_run_safe
@app.command("doctor")
def doctor(name: str):
    mgr = _mgr()
    try:
        info = mgr.doctor_runtime(name)
    except Exception as exc:
        typer.secho(f"doctor failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(info, ensure_ascii=False, indent=2))


@_run_safe
@app.command("migrate")
def migrate(
    name: str = typer.Argument(..., help="skill to migrate"),
    dry_run: bool = typer.Option(False, "--dry-run", help="report without applying changes"),
):
    service = SkillUpdateService(get_ctx())
    try:
        result = service.request_update(name, dry_run=dry_run)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{name}: {'updated' if result.updated else 'up-to-date'}"
        + (f" (version {result.version})" if result.version else "")
    )


@_run_safe
@app.command("lint")
def lint(path: str = typer.Argument(".", help="path to skill directory")):
    try:
        target = _resolve_skill_path(path)
    except typer.BadParameter:
        target = Path(path).expanduser().resolve()
    if not target.exists():
        typer.secho(f"path not found: {target}", fg=typer.colors.RED)
        raise typer.Exit(1)

    ctx = get_ctx()
    previous = ctx.skill_ctx.get()
    try:
        if not ctx.skill_ctx.set(target.name, target):
            ctx.skill_ctx.set(target.name, target)
        report = SkillValidationService(ctx).validate(
            skill_name=target.name,
            strict=False,
            install_mode=False,
            probe_tools=False,
        )
    finally:
        if previous is None:
            ctx.skill_ctx.clear()
        else:
            ctx.skill_ctx.set(previous.name, Path(previous.path))

    if report.ok:
        typer.secho("lint passed", fg=typer.colors.GREEN)
        return

    for issue in report.issues:
        location = f" ({issue.where})" if issue.where else ""
        typer.echo(f"[{issue.level}] {issue.code}: {issue.message}{location}")
    raise typer.Exit(1)


@app.command("prep")
def prep_command(skill_name: str):
    """Запуск стадии подготовки (discover) для навыка"""
    try:
        result = run_skill_prep(skill_name)
    except SkillPrepScriptNotFoundError:
        print(f"[red]{_('skill.prep.not_found', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)
    except SkillPrepMissingFunctionError:
        print(f"[red]{_('skill.prep.missing_func', skill_name=skill_name)}[/red]")
        raise typer.Exit(code=1)
    except SkillPrepError as exc:
        print(f"[red]{_('skill.prep.failed', reason=str(exc))}[/red]")
        raise typer.Exit(code=1)

    if result.get("status") == "ok":
        print(f"[green]{_('skill.prep.success', skill_name=skill_name)}[/green]")
    else:
        reason = result.get("reason", "unknown")
        print(f"[red]{_('skill.prep.failed', reason=reason)}[/red]")
