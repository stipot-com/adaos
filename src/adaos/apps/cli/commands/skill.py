# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations

import json
import os
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer
import requests

from adaos.sdk.data.i18n import _
from adaos.apps.cli.git_status import (
    compute_path_status,
    ensure_remote,
    fetch_remote,
    render_diff,
    resolve_base_ref,
    render_noindex_diff,
    unzip_b64_to_dir,
)
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.root.client import RootHttpClient
from adaos.services.root.service import create_zip_bytes
from adaos.services.skill.manager import RuntimeInstallResult, SkillManager
from adaos.services.skill.runtime import (
    SkillRuntimeError,
    run_skill_handler_sync,
)
from adaos.services.skill.update import SkillUpdateService
from adaos.services.skill.validation import SkillValidationService
from adaos.services.skill.scaffold import create as scaffold_create
from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.yjs.webspace import default_webspace_id

app = typer.Typer(help=_("cli.help_skill"))
service_app = typer.Typer(help="Manage service-type skills (start/stop/restart/status).")
app.add_typer(service_app, name="service")


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


def _hub_base_url() -> str:
    conf = load_config()
    url = getattr(conf, "hub_url", None) or os.getenv("ADAOS_HUB_URL") or "http://127.0.0.1:8778"
    return str(url).rstrip("/")


def _hub_headers() -> dict[str, str]:
    conf = load_config()
    token = getattr(conf, "token", None) or os.getenv("ADAOS_TOKEN") or "dev-local-token"
    return {"X-AdaOS-Token": str(token)}


def _hub_get(path: str, *, params: dict | None = None) -> dict:
    url = _hub_base_url() + path
    resp = requests.get(url, headers=_hub_headers(), params=params or {}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _hub_post(path: str, *, body: dict | None = None) -> dict:
    url = _hub_base_url() + path
    resp = requests.post(url, headers=_hub_headers(), json=body or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


@_run_safe
@service_app.command("list")
def service_list(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    check_health: bool = typer.Option(False, "--health", help="Also call each service /health endpoint."),
):
    data = _hub_get("/api/services", params={"check_health": check_health})
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False))
        return
    services = data.get("services") or []
    if not services:
        typer.echo("no service skills discovered")
        return
    for s in services:
        if not isinstance(s, dict):
            continue
        name = s.get("name") or "<unknown>"
        running = "running" if s.get("running") else "stopped"
        base = s.get("base_url") or ""
        extra = ""
        if check_health and "health_ok" in s:
            extra = " health=ok" if s.get("health_ok") else " health=fail"
        typer.echo(f"{name}: {running} {base}{extra}")


@_run_safe
@service_app.command("status")
def service_status(
    name: str = typer.Argument(..., help="Service skill name (folder name in skills workspace)."),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    check_health: bool = typer.Option(False, "--health", help="Also call the service /health endpoint."),
):
    data = _hub_get(f"/api/services/{name}", params={"check_health": check_health})
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False))
        return
    svc = data.get("service") or {}
    typer.echo(json.dumps(svc, ensure_ascii=False, indent=2))


@_run_safe
@service_app.command("start")
def service_start(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/start")
    typer.secho(f"started {name}", fg=typer.colors.GREEN)


@_run_safe
@service_app.command("stop")
def service_stop(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/stop")
    typer.secho(f"stopped {name}", fg=typer.colors.GREEN)


@_run_safe
@service_app.command("restart")
def service_restart(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/restart")
    typer.secho(f"restarted {name}", fg=typer.colors.GREEN)


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
def uninstall(
    name: str,
    safe: bool = typer.Option(False, "--safe", help=_("cli.skill.uninstall.option.safe")),
):
    mgr = _mgr()
    mgr.uninstall(name, safe=safe)
    typer.echo(_("cli.skill.uninstall.done", name=name))


@_run_safe
@app.command("reconcile-fs-to-db")
def reconcile_fs_to_db():
    """Обходит {skills_dir} и проставляет installed=1 для найденных папок (кроме .git).
    Не трогает active_version/repo_url.
    """
    mgr = _mgr()
    ctx = get_ctx()
    root = ctx.paths.skills_dir()
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

    _resolve_skill_path(skill_name)
    mgr = _mgr()
    res = mgr.push(skill_name, message, signoff=signoff)
    if res in {"nothing-to-push", "nothing-to-commit"}:
        typer.echo(_("cli.skill.push.nothing"))
    else:
        typer.echo(_("cli.skill.push.done", name=skill_name, revision=res))


@_run_safe
@app.command("create")
def cmd_create(name: str, template: str = typer.Option("skill_default", "--template", "-t")):
    p = scaffold_create(name, template=template)
    typer.echo(_("cli.skill.create.created", path=p))
    typer.echo(_("cli.skill.create.hint_push", name=name))


@_run_safe
@app.command("scaffold")
def cmd_scaffold(name: str, template: str = typer.Option("skill_default", "--template", help="skill template name")):
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
    safe: bool = typer.Option(False, "--safe", help=_("cli.skill.install.option.safe")),
):
    mgr = _mgr()
    try:
        result = mgr.install(name, validate=False, safe=safe)
    except Exception as exc:
        message = str(exc)
        typer.secho(f"install failed: {message}", fg=typer.colors.RED)
        # Provide an explicit hint when Git reports unresolved merges.
        if "git pull" in message and "unmerged files" in message.lower():
            try:
                ctx = get_ctx()
                workspace_root = ctx.paths.workspace_dir()
                typer.echo(f"Skills workspace Git repo: {workspace_root}")
                typer.echo(
                    f"Run 'git -C \"{workspace_root}\" status' to inspect conflicted files, "
                    f"resolve them, then re-run 'adaos skill install {name}'."
                )
            except Exception:
                # Best-effort hint; ignore failures in helper diagnostics.
                pass
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

    try:
        activated_slot = mgr.activate_for_space(
            skill_name,
            version=runtime.version,
            slot=runtime.slot,
            space="default",
            webspace_id=default_webspace_id(),
        )
    except Exception as exc:
        typer.secho(f"activation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.secho(f"skill {skill_name} now active on slot {activated_slot}", fg=typer.colors.GREEN)

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
        target = mgr.activate_for_space(
            name,
            version=version,
            slot=slot,
            space="default",
            webspace_id=default_webspace_id(),
        )
    except Exception as exc:
        typer.secho(f"activate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    # Best-effort: уведомить живой hub через HTTP API, чтобы
    # skills.activated отработал в его процессе и web_desktop_skill
    # сразу обновил каталог без перезапуска, не трогая ещё раз runtime.
    try:
        ctx = get_ctx()
        conf = getattr(ctx, "config", None)
        base = None
        if conf is not None and getattr(conf, "hub_url", None):
            base = conf.hub_url
        if not base:
            base = os.getenv("ADAOS_SELF_BASE_URL") or os.getenv("ADAOS_BASE") or os.getenv("ADAOS_API_BASE") or "http://127.0.0.1:8777"
        url = str(base).rstrip("/") + "/api/skills/runtime/notify-activated"
        payload = {
            "name": name,
            "space": "default",
            "webspace_id": default_webspace_id(),
        }
        headers = {}
        token = os.getenv("ADAOS_TOKEN")
        if token:
            headers["X-AdaOS-Token"] = token
        # Таймаут маленький и любые ошибки игнорируем, чтобы CLI
        # оставался работоспособен, даже когда API ещё не поднят.
        try:
            requests.post(url, json=payload, headers=headers, timeout=2.0)
        except Exception:
            pass
    except Exception:
        pass

    typer.secho(f"skill {name} now active on slot {target}", fg=typer.colors.GREEN)


@_run_safe
@app.command("rollback")
def rollback(name: str):
    mgr = _mgr()
    try:
        slot = mgr.rollback_for_space(name, space="default", webspace_id=default_webspace_id())
    except Exception as exc:
        typer.secho(f"rollback failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"rolled back {name} to slot {slot}", fg=typer.colors.YELLOW)


@_run_safe
@app.command("status")
def status(
    name: Optional[str] = typer.Argument(None, help="skill name (omit to report for all installed skills)"),
    space: str = typer.Option("workspace", "--space", help="workspace | dev"),
    remote: str = typer.Option("origin", "--remote", help="git remote name for comparison"),
    ref: Optional[str] = typer.Option(None, "--ref", help="base git ref (default: <remote>/HEAD or @{u})"),
    fetch: bool = typer.Option(False, "--fetch/--no-fetch", help="git fetch before comparing"),
    diff: bool = typer.Option(False, "--diff", help="print git diff vs base ref (requires NAME)"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    ctx = get_ctx()
    space = (space or "workspace").strip().lower()
    if space not in {"workspace", "dev"}:
        typer.secho("--space must be 'workspace' or 'dev'", fg=typer.colors.RED)
        raise typer.Exit(2)

    workspace_root = ctx.paths.workspace_dir()
    skills_root = ctx.paths.skills_workspace_dir()
    dev_skills_root = ctx.paths.dev_skills_dir()
    dev_skills_root = dev_skills_root() if callable(dev_skills_root) else dev_skills_root

    if diff and not name:
        typer.secho("--diff requires a specific skill name", fg=typer.colors.RED)
        raise typer.Exit(2)

    # Workspace: compare against main registry repo.
    REGISTRY_URL = os.getenv("ADAOS_WORKSPACE_REGISTRY_REPO", "https://github.com/stipot-com/adaos-registry.git")
    REGISTRY_REMOTE = os.getenv("ADAOS_WORKSPACE_REGISTRY_REMOTE", "registry")
    REGISTRY_BRANCH = os.getenv("ADAOS_WORKSPACE_REGISTRY_BRANCH", "main")

    if space == "workspace":
        # Ensure expected remote exists; allow user override via --remote/--ref.
        if remote == "origin" and not ref:
            ensure_remote(workspace_root, name=REGISTRY_REMOTE, url=REGISTRY_URL)
            remote = REGISTRY_REMOTE
            ref = f"{REGISTRY_REMOTE}/{REGISTRY_BRANCH}"
        if fetch:
            err = fetch_remote(workspace_root, remote=remote)
            if err:
                typer.secho(f"git fetch failed: {err}", fg=typer.colors.YELLOW)

        base_ref = (ref or "").strip() or resolve_base_ref(workspace_root, remote=remote)
    else:
        # Dev: compare local dev folder with the Root backend draft state (API).
        base_ref = None

    if name:
        names = [name]
    else:
        if space == "dev":
            # In dev space, registry may not reflect local dev folders; prefer filesystem.
            root = Path(dev_skills_root)
            names = []
            if root.exists():
                for child in root.iterdir():
                    if child.is_dir():
                        names.append(child.name)
            names = sorted(set(names))
        else:
            try:
                rows = SqliteSkillRegistry(ctx.sql).list()
            except Exception:
                rows = []
            names = []
            for row in rows:
                n = getattr(row, "name", None) or getattr(row, "id", None)
                if not n or not bool(getattr(row, "installed", True)):
                    continue
                names.append(str(n))
            names = sorted(set(names))

    results: list[dict] = []
    for skill_name in names:
        runtime_state = None
        runtime_error = None
        if space == "workspace":
            try:
                runtime_state = mgr.runtime_status(skill_name)
            except Exception as exc:
                runtime_error = str(exc)

        if space == "workspace":
            path_status = compute_path_status(
                workdir=workspace_root,
                path=(Path(skills_root) / skill_name),
                base_ref=base_ref,
            )
            entry = {
                "name": skill_name,
                "space": space,
                "runtime": runtime_state,
                "runtime_error": runtime_error,
                "git": {
                    "path": path_status.path,
                    "exists": path_status.exists,
                    "dirty": path_status.dirty,
                    "base_ref": path_status.base_ref,
                    "changed_vs_base": path_status.changed_vs_base,
                    "local_last_commit": (
                        {
                            "sha": path_status.local_last_commit.sha,
                            "timestamp": path_status.local_last_commit.timestamp,
                            "iso": path_status.local_last_commit.iso,
                            "subject": path_status.local_last_commit.subject,
                        }
                        if path_status.local_last_commit
                        else None
                    ),
                    "base_last_commit": (
                        {
                            "sha": path_status.base_last_commit.sha,
                            "timestamp": path_status.base_last_commit.timestamp,
                            "iso": path_status.base_last_commit.iso,
                            "subject": path_status.base_last_commit.subject,
                        }
                        if path_status.base_last_commit
                        else None
                    ),
                    "error": path_status.error,
                },
            }
        else:
            cfg = load_config()
            base_url = getattr(getattr(cfg, "root_settings", None), "base_url", None) or "https://api.inimatic.com"
            node_id = getattr(getattr(cfg, "node_settings", None), "id", None) or getattr(cfg, "node_id", None) or "hub"
            ca_path = cfg.ca_cert_path()
            cert_path = cfg.hub_cert_path()
            key_path = cfg.hub_key_path()
            verify: str | bool = str(ca_path) if ca_path.exists() else True
            cert = (str(cert_path), str(key_path)) if cert_path.exists() and key_path.exists() else None

            client = RootHttpClient(base_url=base_url)
            local_dir = Path(dev_skills_root) / skill_name
            local_sha256 = None
            try:
                import hashlib

                local_bytes = create_zip_bytes(local_dir)
                local_sha256 = hashlib.sha256(local_bytes).hexdigest()
            except Exception:
                local_sha256 = None

            remote_meta = None
            remote_error = None
            try:
                remote_meta = client.get_skill_draft_info(name=skill_name, node_id=str(node_id), verify=verify, cert=cert)
            except Exception as exc:
                remote_error = str(exc)

            remote_sha256 = None
            if isinstance(remote_meta, dict):
                remote_sha256 = remote_meta.get("sha256")

            changed_vs_base = None
            if local_sha256 and remote_sha256:
                changed_vs_base = str(local_sha256) != str(remote_sha256)

            diff_text = None
            if diff and name == skill_name:
                try:
                    arch = client.get_skill_draft_archive(name=skill_name, node_id=str(node_id), verify=verify, cert=cert)
                    b64 = str(arch.get("archive_b64") or "")
                    if b64:
                        import tempfile

                        with tempfile.TemporaryDirectory() as tmp:
                            remote_dir = Path(tmp) / "remote"
                            remote_dir.mkdir(parents=True, exist_ok=True)
                            unzip_b64_to_dir(archive_b64=b64, dest=remote_dir)
                            _changed, diff_out = render_noindex_diff(left=remote_dir, right=local_dir)
                            diff_text = diff_out
                except Exception:
                    diff_text = None

            entry = {
                "name": skill_name,
                "space": space,
                "dev_compare": {
                    "node_id": str(node_id),
                    "base_url": base_url,
                    "local_path": local_dir.as_posix(),
                    "local_sha256": local_sha256,
                    "remote": remote_meta,
                    "remote_error": remote_error,
                    "changed_vs_base": changed_vs_base,
                    "diff": diff_text,
                },
            }
        results.append(entry)

    if json_output:
        payload = {"skills": results}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if name:
        entry = results[0] if results else {}
        st = entry.get("runtime") or {}
        g = entry.get("git") or {}
        typer.echo(f"skill: {entry.get('name')}")
        typer.echo(f"space: {entry.get('space')}")
        if entry.get("runtime_error"):
            typer.secho(f"runtime: error: {entry.get('runtime_error')}", fg=typer.colors.YELLOW)
        elif space == "workspace":
            typer.echo(f"version: {st.get('version')}")
            typer.echo(f"active slot: {st.get('active_slot')}")
            if st.get("ready", True):
                typer.echo(f"resolved manifest: {st.get('resolved_manifest')}")
            else:
                typer.echo("resolved manifest: (not activated)")
                pending_slot = st.get("pending_slot")
                hint_slot = pending_slot or st.get("active_slot")
                activation_hint = f" --slot {pending_slot}" if pending_slot else ""
                typer.secho(
                    f"slot {hint_slot} is prepared but inactive. run 'adaos skill activate {name}{activation_hint}'",
                    fg=typer.colors.YELLOW,
                )
            tests = st.get("tests") or {}
            if tests:
                typer.echo("tests: " + ", ".join(f"{k}={v}" for k, v in tests.items()))
            default_tool = st.get("default_tool")
            if default_tool:
                typer.echo(f"default tool: {default_tool}")

        if space == "workspace":
            typer.echo(f"git path: {g.get('path')}")
            typer.echo(f"git base: {g.get('base_ref') or '(none)'}")
            if g.get("error"):
                typer.secho(f"git: {g.get('error')}", fg=typer.colors.YELLOW)
            else:
                flags: list[str] = []
                if g.get("dirty"):
                    flags.append("dirty")
                if g.get("changed_vs_base"):
                    flags.append("diff")
                typer.echo("git status: " + (", ".join(flags) if flags else "clean"))
                if g.get("local_last_commit"):
                    lc = g["local_last_commit"]
                    typer.echo(f"last local: {lc.get('sha')} {lc.get('iso') or lc.get('timestamp')} {lc.get('subject')}")
                if g.get("base_last_commit"):
                    bc = g["base_last_commit"]
                    typer.echo(f"last base:  {bc.get('sha')} {bc.get('iso') or bc.get('timestamp')} {bc.get('subject')}")

            if diff:
                if not base_ref:
                    typer.secho("cannot diff: base ref is not available", fg=typer.colors.YELLOW)
                else:
                    try:
                        typer.echo(render_diff(workspace_root, base_ref=base_ref, path=str(g.get("path") or "")))
                    except Exception as exc:
                        typer.secho(f"diff failed: {exc}", fg=typer.colors.RED)
                        raise typer.Exit(1) from exc
        else:
            dc = entry.get("dev_compare") or {}
            typer.echo(f"root base: {dc.get('base_url')}")
            typer.echo(f"node_id: {dc.get('node_id')}")
            typer.echo(f"local path: {dc.get('local_path')}")
            if dc.get("changed_vs_base") is True:
                typer.secho("status: diff", fg=typer.colors.YELLOW)
            elif dc.get("changed_vs_base") is False:
                typer.echo("status: clean")
            else:
                typer.secho("status: unknown", fg=typer.colors.YELLOW)
            if diff and dc.get("diff"):
                typer.echo(dc.get("diff") or "")
        return

    # Summary for all skills
    for entry in results:
        st = entry.get("runtime") or {}
        g = entry.get("git") or {}
        flags: list[str] = []
        if entry.get("runtime_error"):
            flags.append("runtime-error")
        if space == "workspace":
            if g.get("dirty"):
                flags.append("dirty")
            if g.get("changed_vs_base"):
                flags.append("diff")
        else:
            dc = entry.get("dev_compare") or {}
            if dc.get("changed_vs_base"):
                flags.append("diff")
        version = st.get("version") or ("n/a" if space == "dev" else "unknown")
        slot = st.get("active_slot") or ("n/a" if space == "dev" else "n/a")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        typer.echo(f"{entry.get('name')}: v{version} slot={slot}{suffix}")


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
    typer.echo(f"{name}: {'updated' if result.updated else 'up-to-date'}" + (f" (version {result.version})" if result.version else ""))
