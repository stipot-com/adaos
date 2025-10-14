from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import typer

from adaos.services.node_config import displayable_path
from adaos.services.root.service import (
    DeviceAuthorization,
    ArtifactDeleteResult,
    ArtifactListItem,
    ArtifactNotFoundError,
    RootDeveloperService,
    RootInitResult,
    RootLoginResult,
    RootServiceError,
    TemplateResolutionError,
)

app = typer.Typer(help="Developer utilities for Root and Forge workflows.")
root_app = typer.Typer(help="Bootstrap and authenticate against the Root service.")
skill_app = typer.Typer(help="Manage owner skills in the local Forge workspace.")
scenario_app = typer.Typer(help="Manage owner scenarios in the local Forge workspace.")

app.add_typer(root_app, name="root")
app.add_typer(skill_app, name="skill")
app.add_typer(scenario_app, name="scenario")


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
