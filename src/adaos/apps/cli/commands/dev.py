from __future__ import annotations

import json, os, traceback
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import asdict

import typer

from adaos.apps.cli.i18n import _
from adaos.apps.cli.commands.skill import _mgr
from adaos.apps.yjs.webspace import default_webspace_id
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import displayable_path
from adaos.services.root.service import (
    DeviceAuthorization,
    ArtifactDeleteResult,
    ArtifactListItem,
    ArtifactNotFoundError,
    ArtifactPublishResult,
    RootDeveloperService,
    RootInitResult,
    RootLoginResult,
    RootServiceError,
    TemplateResolutionError,
)
from adaos.services.skill.runtime import (
    SkillPrepError,
    SkillPrepMissingFunctionError,
    SkillPrepScriptNotFoundError,
    run_dev_skill_prep,
)
from adaos.services.root.client import RootHttpClient
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context, load_scenario

app = typer.Typer(help="Developer utilities for Root and Forge workflows.")
root_app = typer.Typer(help="Bootstrap and authenticate against the Root service.")
skill_app = typer.Typer(help="Manage owner skills in the local Forge workspace.")
scenario_app = typer.Typer(help="Manage owner scenarios in the local Forge workspace.")

app.add_typer(root_app, name="root")
app.add_typer(skill_app, name="skill")
app.add_typer(scenario_app, name="scenario")


def _run_safe(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _service() -> RootDeveloperService:
    return RootDeveloperService()


def _display_path(path: Path | None) -> str:
    if path is None:
        return "—"
    rendered = displayable_path(path)
    return rendered if rendered is not None else str(path)


def _print_error(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED)


def _parse_metadata(pairs: List[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise typer.BadParameter("Metadata must be in key=value format")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise typer.BadParameter("Metadata key must not be empty")
        result[key] = value
    return result


def _echo_artifact_list(items: List[ArtifactListItem], json_output: bool) -> None:
    if json_output:
        payload = [
            {
                "name": item.name,
                "version": item.version,
                "updated_at": item.updated_at,
            }
            for item in items
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not items:
        typer.echo("No artifacts found.")
        return

    headers = ["Name", "Version", "Updated"]
    rows = [
        [
            item.name,
            item.version or "—",
            item.updated_at or "—",
        ]
        for item in items
    ]
    widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    header_line = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    typer.echo(header_line)
    typer.echo(separator)
    for row in rows:
        typer.echo("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))


def _echo_delete_result(kind_label: str, result: ArtifactDeleteResult) -> None:
    typer.secho(f"{kind_label} '{result.name}' deleted.", fg=typer.colors.GREEN)
    typer.echo(f"Location: {_display_path(result.path)}")
    if result.version:
        typer.echo(f"Last version: {result.version}")
    if result.updated_at:
        typer.echo(f"Last updated: {result.updated_at}")


def _echo_publish_result(kind_label: str, result: ArtifactPublishResult) -> None:
    if result.dry_run:
        typer.secho(
            f"Dry run: would publish {kind_label.lower()} '{result.name}' to the registry.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho(f"{kind_label} '{result.name}' published to the registry.", fg=typer.colors.GREEN)
    typer.echo(f"Source: {_display_path(result.source_path)}")
    typer.echo(f"Target: {_display_path(result.target_path)}")
    typer.echo(f"Version: {result.version}")
    if result.previous_version:
        typer.echo(f"Previous version: {result.previous_version}")
    typer.echo(f"Updated at: {result.updated_at}")
    if result.warnings:
        typer.secho("Warnings:", fg=typer.colors.YELLOW)
        for warning in result.warnings:
            typer.echo(f"  - {warning}")


@root_app.command("init")
def root_init(
    token: str = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for bootstrap. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN environment variables.",
    ),
    metadata: List[str] = typer.Option(
        [],
        "--meta",
        help="Additional bootstrap metadata entries in key=value format.",
    ),
) -> None:
    service = _service()
    try:
        meta_payload = _parse_metadata(metadata) if metadata else None
        result = service.init(root_token=token, metadata=meta_payload)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_init_result(result)


def _echo_init_result(result: RootInitResult) -> None:
    typer.secho("Root subnet initialized.", fg=typer.colors.GREEN)
    typer.echo(f"Subnet ID: {result.subnet_id}")
    typer.echo(f"Hub private key: {_display_path(result.hub_key_path)}")
    typer.echo(f"Hub certificate: {_display_path(result.hub_cert_path)}")
    if result.ca_cert_path:
        typer.echo(f"CA certificate: {_display_path(result.ca_cert_path)}")
    typer.echo(f"Workspace: {_display_path(result.workspace_path)}")
    typer.echo(f"reused: {str(result.reused).lower()}")


@root_app.command("login")
def root_login() -> None:
    service = _service()

    def on_authorize(auth: DeviceAuthorization) -> None:
        typer.echo("To authorize this device:")
        if auth.verification_uri_complete:
            typer.echo(f"  Open: {auth.verification_uri_complete}")
        else:
            typer.echo(f"  Open: {auth.verification_uri}")
            typer.echo(f"  Enter code: {auth.user_code}")
        typer.echo(f"Polling every {auth.interval} seconds (expires in {auth.expires_in // 60} minutes)…")

    try:
        result = service.login(on_authorize=on_authorize)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_login_result(result)


def _echo_login_result(result: RootLoginResult) -> None:
    typer.secho(f"Owner {result.owner_id} authenticated.", fg=typer.colors.GREEN)
    if result.subnet_id:
        typer.echo(f"Subnet ID: {result.subnet_id}")
    typer.echo(f"Workspace: {_display_path(result.workspace_path)}")


@app.command("telegram")
def dev_login(
    status: Optional[str] = typer.Option(None, "--status", help="Check pairing status for code."),
    revoke: Optional[str] = typer.Option(None, "--revoke", help="Revoke pairing code."),
    hub: Optional[str] = typer.Option(None, "--hub", help="Explicit hub id to bind (overrides defaults)."),
):
    ctx = get_ctx()

    client = RootHttpClient.from_settings(
        ctx.settings,
    )
    base = ctx.settings.api_base
    # Prefer explicit CLI option; otherwise always use canonical subnet_id for pairing,
    # because backend stores WS tokens keyed by canonical hub id (not alias).
    hub_id = hub or ctx.settings.subnet_id or ctx.settings.default_hub or ctx.settings.owner_id
    if status:
        data = client.request("GET", f"{base}/io/tg/pair/status", params={"code": status})
        typer.echo(data)
        raise typer.Exit(0)
    if revoke:
        data = client.request("GET", f"{base}/io/tg/pair/revoke", params={"code": revoke})
        typer.echo(data)
        raise typer.Exit(0)
    print("hub_log", hub_id)
    # Send hub id in JSON body using the expected key; avoid query param 'hub_id' which backend ignores
    data = client.request("POST", f"{base}/io/tg/pair/create", json={"code": "PING", "hub_id": hub_id})
    # TODO Перенести обработку ошибок из метода _request на уровень  логики.
    """ if resp.status_code != 200:
        _print_error(f"API error: {resp.status_code} {resp.text}")
        raise typer.Exit(1) """
    code = data.get("pair_code") or data.get("code")
    if not code:
        _print_error("No pair_code in response.")
        raise typer.Exit(1)
    deep_link = data.get("deep_link") or f"https://t.me/adaos_bot?start={code}"
    typer.secho("Telegram pairing:", fg=typer.colors.GREEN)
    typer.echo(f"  pair_code: {code}")
    typer.echo(f"  deep_link: {deep_link}")
    if data.get("expires_at"):
        typer.echo(f"  expires_at: {data['expires_at']}")

    # Save NATS WS credentials into node.yaml so hub can preconfigure WS
    hub_id_resp = data.get("hub_id") or hub_id
    hub_token = data.get("hub_nats_token")
    # Always use local canonical hub id for WS user to avoid alias-based mismatches
    local_hub_id = ctx.settings.subnet_id or hub_id_resp
    nats_user = (f"hub_{local_hub_id}" if local_hub_id else None) or data.get("nats_user") or (f"hub_{hub_id_resp}" if hub_id_resp else None)
    # Pin to dedicated NATS WS domain regardless of API suggestion
    nats_ws_url = "wss://nats.inimatic.com"
    if hub_id_resp and hub_token and nats_user:
        try:
            from adaos.services.capacity import _load_node_yaml as _load_node, _save_node_yaml as _save_node

            data_yaml = _load_node()
            nats_cfg = data_yaml.get("nats") or {}
            nats_cfg["ws_url"] = nats_ws_url
            nats_cfg["user"] = nats_user
            nats_cfg["pass"] = hub_token
            # Seed a human-friendly alias in node.yaml for UX (kept in sync by backend commands)
            # Default to 'hub' for the first binding; can be changed later via /alias in Telegram
            if not nats_cfg.get("alias"):
                nats_cfg["alias"] = "hub"
            data_yaml["nats"] = nats_cfg
            _save_node(data_yaml)
            typer.echo("Saved NATS WS credentials to node.yaml")
        except Exception as e:
            _print_error(f"Failed to save NATS creds: {e}")


@skill_app.command("create")
def skill_create(
    name: str,
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help="Skill template name. Defaults to the built-in skill_default template.",
    ),
) -> None:
    service = _service()
    try:
        result = service.create_skill(name, template=template)
    except TemplateResolutionError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    typer.secho(f"Skill '{result.name}' created for owner {result.owner_id}.", fg=typer.colors.GREEN)
    typer.echo(f"Location: {_display_path(result.path)}")


@skill_app.command("push")
def skill_push(name: str) -> None:
    service = _service()
    try:
        result = service.push_skill(name)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    typer.secho(f"Skill '{name}' uploaded to Forge.", fg=typer.colors.GREEN)
    typer.echo(f"Stored path: {result.stored_path}")
    typer.echo(f"SHA256: {result.sha256}")
    typer.echo(f"Bytes uploaded: {result.bytes_uploaded}")


@skill_app.command("list")
def skill_list(json_output: bool = typer.Option(False, "--json", help="Render output as JSON.")) -> None:
    service = _service()
    try:
        items = service.list_skills()
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_artifact_list(items, json_output)


@skill_app.command("delete")
def skill_delete(
    name: str,
    yes: bool = typer.Option(False, "--yes", help="Delete without confirmation."),
) -> None:
    if not yes:
        confirm = typer.confirm(f"Delete skill '{name}' from the dev workspace?", default=False)
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    service = _service()
    try:
        result = service.delete_skill(name)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_delete_result("Skill", result)


@skill_app.command("publish")
def skill_publish(
    name: str,
    bump: str = typer.Option(
        "patch",
        "--bump",
        help="Which semantic version component to increment (patch, minor, major).",
        show_default=True,
    ),
    force: bool = typer.Option(False, "--force", help="Ignore manifest metadata differences."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned changes without modifying files."),
    signoff: bool = typer.Option(False, "--signoff", help="Add Signed-off-by"),
) -> None:
    bump_normalized = bump.lower()
    if bump_normalized not in {"patch", "minor", "major"}:
        raise typer.BadParameter("--bump must be one of patch, minor, or major")

    service = _service()
    try:
        result = service.publish_skill(name, bump=bump_normalized, force=force, dry_run=dry_run)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_publish_result("Skill", result)


@scenario_app.command("publish")
def scenario_publish(
    name: str,
    bump: str = typer.Option(
        "patch",
        "--bump",
        help="Which semantic version component to increment (patch, minor, major).",
        show_default=True,
    ),
    force: bool = typer.Option(False, "--force", help="Ignore manifest metadata differences."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned changes without modifying files."),
) -> None:
    bump_normalized = bump.lower()
    if bump_normalized not in {"patch", "minor", "major"}:
        raise typer.BadParameter("--bump must be one of patch, minor, or major")

    service = _service()
    try:
        result = service.publish_scenario(name, bump=bump_normalized, force=force, dry_run=dry_run)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_publish_result("Scenario", result)


@scenario_app.command("create")
def scenario_create(
    name: str,
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help="Scenario template name. Defaults to the built-in scenario_default template.",
    ),
) -> None:
    service = _service()
    try:
        result = service.create_scenario(name, template=template)
    except TemplateResolutionError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    typer.secho(f"Scenario '{result.name}' created for owner {result.owner_id}.", fg=typer.colors.GREEN)
    typer.echo(f"Location: {_display_path(result.path)}")


@scenario_app.command("push")
def scenario_push(name: str) -> None:
    service = _service()
    try:
        result = service.push_scenario(name)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    typer.secho(f"Scenario '{name}' uploaded to Forge.", fg=typer.colors.GREEN)
    typer.echo(f"Stored path: {result.stored_path}")
    typer.echo(f"SHA256: {result.sha256}")
    typer.echo(f"Bytes uploaded: {result.bytes_uploaded}")


@scenario_app.command("list")
def scenario_list(json_output: bool = typer.Option(False, "--json", help="Render output as JSON.")) -> None:
    service = _service()
    try:
        items = service.list_scenarios()
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_artifact_list(items, json_output)


@scenario_app.command("delete")
def scenario_delete(
    name: str,
    yes: bool = typer.Option(False, "--yes", help="Delete without confirmation."),
) -> None:
    if not yes:
        confirm = typer.confirm(f"Delete scenario '{name}' from the dev workspace?", default=False)
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    service = _service()
    try:
        result = service.delete_scenario(name)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_delete_result("Scenario", result)


def _resolve_dev_scenario_file(name: str, base: Path) -> Path | None:
    base = base.expanduser().resolve()
    if base.is_file():
        return base if base.exists() else None

    search_roots: list[Path] = []
    if base.is_dir():
        search_roots.extend([base / name, base])

    for root in search_roots:
        if not root.exists():
            continue
        for filename in ("scenario.yaml", "scenario.yml", "scenario.json"):
            candidate = root / filename
            if candidate.exists():
                return candidate
    return None


@_run_safe
@scenario_app.command("run")
def scenario_run(
    name: str = typer.Argument(..., help="scenario name in DEV space"),
) -> None:
    ctx = get_ctx()
    scenario_folder = ctx.paths.dev_scenarios_dir() / name
    if scenario_folder is None or not scenario_folder.exists():
        typer.secho(f"Scenario file not found: {name}", fg=typer.colors.RED)
        raise typer.Exit(1)

    runtime = ScenarioRuntime()
    result = runtime.run_from_file(str(scenario_folder))
    meta = result.get("meta") or {}
    log_file = meta.get("log_file")
    typer.secho(f"Scenario '{name}' executed.", fg=typer.colors.GREEN)
    if log_file:
        typer.echo(f"Log: {log_file}")
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@_run_safe
@scenario_app.command("validate")
def scenario_validate(
    name: str = typer.Argument(..., help="scenario name in DEV space"),
    path: Optional[Path] = typer.Option(None, "--path", help="override scenario location (dir or file)"),
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
) -> None:
    ctx = get_ctx()
    base = Path(path).expanduser().resolve() if path is not None else ctx.paths.dev_scenarios_dir()
    scenario_file = _resolve_dev_scenario_file(name, base)

    if scenario_file is None or not scenario_file.exists():
        target = scenario_file if scenario_file is not None else (path or name)
        typer.secho(f"Scenario file not found: {target}", fg=typer.colors.RED)
        raise typer.Exit(1)

    model = load_scenario(scenario_file)
    runtime = ScenarioRuntime()
    errors = runtime.validate(model)

    if json_output:
        payload = {"ok": not bool(errors), "errors": errors, "scenario_id": model.id}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if not errors else 1)

    if errors:
        typer.secho("Validation failed:", fg=typer.colors.RED)
        for err in errors:
            typer.echo(f"- {err}")
        raise typer.Exit(1)

    typer.secho(f"Scenario '{model.id}' is valid.", fg=typer.colors.GREEN)


@_run_safe
@skill_app.command("validate")
def dev_skill_validate(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="treat warnings as errors"),
    probe_tools: bool = typer.Option(False, "--probe-tools", help="import handlers to verify tool exports"),
    path: Path = typer.Option(None, "--path", exists=True, file_okay=False, dir_okay=True, readable=True, help="validate skill at explicit folder path (overrides DEV lookup)"),
):
    """
    Validate a skill from the DEV space (or explicit --path).
    """
    mgr = _mgr()
    try:
        report = mgr.validate_skill(
            name,
            strict=strict,
            probe_tools=probe_tools,
            source="dev",
            path=path,
        )
    except FileNotFoundError as exc:
        typer.secho(f"validate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
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
        location = f" ({issue.where})" if getattr(issue, "where", None) else ""
        typer.echo(f"[{issue.level}] {issue.code}: {issue.message}{location}")
    raise typer.Exit(1)


@_run_safe
@skill_app.command("lint")
def dev_skill_lint(
    target: str = typer.Argument(".", help="skill path or DEV skill name"),
):
    """
    Run relaxed validation (lint) for a DEV skill resolving via PathProvider.
    """

    mgr = _mgr()
    candidate = Path(target).expanduser()

    try:
        if candidate.exists():
            resolved = candidate.resolve()
            report = mgr.validate_skill(
                resolved.name,
                strict=False,
                probe_tools=False,
                path=resolved,
            )
        else:
            report = mgr.validate_skill(
                target,
                strict=False,
                probe_tools=False,
                source="dev",
            )
    except FileNotFoundError as exc:
        typer.secho(f"lint failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except Exception as exc:
        typer.secho(f"lint failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if report.ok:
        typer.secho("lint passed", fg=typer.colors.GREEN)
        return

    for issue in report.issues:
        location = f" ({issue.where})" if getattr(issue, "where", None) else ""
        typer.echo(f"[{issue.level}] {issue.code}: {issue.message}{location}")
    raise typer.Exit(1)


@_run_safe
@skill_app.command("prep")
def dev_skill_prep(skill_name: str) -> None:
    """Execute the DEV skill preparation helper (prep/prepare.py)."""

    try:
        result = run_dev_skill_prep(skill_name)
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
        return

    reason = result.get("reason", "unknown")
    print(f"[red]{_('skill.prep.failed', reason=reason)}[/red]")
    raise typer.Exit(code=1)


@_run_safe
@skill_app.command("test", help=_("cli.skill.test.help"))
def dev_skill_test(
    name: str = typer.Argument(..., help=_("cli.skill.test.name_help")),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    runtime: bool = typer.Option(False, "--runtime", help="run tests from the DEV runtime slot instead of source tree"),
) -> None:
    """Execute DEV skill tests either from source tree or the prepared runtime slot."""

    mgr = _mgr()
    try:
        if runtime:
            results = mgr.run_skill_tests(name, source="dev")
        else:
            results = mgr.run_dev_skill_tests(name)
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
        detail = f" ({result.detail})" if getattr(result, "detail", None) else ""
        typer.echo(f"{test_name}: {result.status}{detail}")
        if result.status != "passed":
            failed = True

    if failed:
        log_path: Path | None = None
        if runtime:
            for result in results.values():
                detail = getattr(result, "detail", None)
                if detail and "log:" in detail:
                    hint = detail.split("log:", 1)[1].strip()
                    if hint.endswith(")"):
                        hint = hint[:-1].rstrip()
                    candidate = Path(hint)
                    if candidate.exists():
                        log_path = candidate
                        break
        else:
            log_path = Path(mgr.ctx.paths.dev_skills_dir()) / name / "logs" / "tests.dev.log"

        if log_path and log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="ignore")
                tail = "\n".join(text.splitlines()[-80:])
                typer.echo("\n--- tests log tail ---")
                typer.echo(tail)
                typer.echo(f"--- end (full log: {log_path}) ---")
            except Exception:
                pass
        raise typer.Exit(1)

    typer.secho("tests passed", fg=typer.colors.GREEN)


@_run_safe
@skill_app.command("setup")
def dev_skill_setup(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
) -> None:
    mgr = _mgr()
    try:
        result = mgr.dev_setup_skill(name)
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
@skill_app.command("run")
def dev_skill_run(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    tool: Optional[str] = typer.Argument(None, help="tool name to run (defaults to default_tool)"),
    payload: str = typer.Option("{}", "--json", help="JSON payload for the tool call"),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="tool execution timeout"),
    slot: Optional[str] = typer.Option(None, "--slot", help="run against specific slot (A/B)"),
) -> None:
    try:
        payload_obj = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        typer.secho(f"invalid payload: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    mgr = _mgr()
    try:
        result = mgr.run_dev_tool(name, tool, payload_obj, timeout=timeout, slot=slot)
    except Exception as exc:
        typer.secho(f"run failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(json.dumps(result, ensure_ascii=False))


@_run_safe
@skill_app.command("activate")
def dev_skill_activate(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    slot: Optional[str] = typer.Option(None, "--slot", help="activate specific slot (A/B)"),
    version: Optional[str] = typer.Option(None, "--version", help="activate specific version (defaults to manifest or 'dev')"),
) -> None:
    """Activate the DEV skill runtime under .adaos/dev/<subnet>/skills/<name>."""
    mgr = _mgr()
    try:
        target = mgr.activate_for_space(name, version=version, slot=slot, space="dev", webspace_id=default_webspace_id())
    except Exception as exc:
        typer.secho(f"activate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"skill {name} now active on slot {target}", fg=typer.colors.GREEN)


@_run_safe
@skill_app.command("status")
def dev_skill_status(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
) -> None:
    mgr = _mgr()
    try:
        state = mgr.dev_runtime_status(name)
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
            f"slot {hint_slot} is prepared but inactive. run 'adaos dev skill activate {name}{activation_hint}'",
            fg=typer.colors.YELLOW,
        )
    tests = state.get("tests") or {}
    if tests:
        typer.echo("tests: " + ", ".join(f"{k}={v}" for k, v in tests.items()))
    default_tool = state.get("default_tool")
    if default_tool:
        typer.echo(f"default tool: {default_tool}")


@_run_safe
@skill_app.command("rollback")
def dev_skill_rollback(name: str = typer.Argument(..., help="skill name in DEV space")) -> None:
    mgr = _mgr()
    try:
        slot = mgr.rollback_for_space(name, space="dev", webspace_id=default_webspace_id())
    except Exception as exc:
        typer.secho(f"rollback failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"rolled back {name} to slot {slot}", fg=typer.colors.YELLOW)
