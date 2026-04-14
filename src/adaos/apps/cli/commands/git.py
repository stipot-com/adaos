from __future__ import annotations

import json
from pathlib import Path

import typer

from adaos.services.agent_context import get_ctx
from adaos.services.git.availability import autodetect_git, get_git_availability, set_git_disabled, set_git_enabled

app = typer.Typer(help="Git availability and archive fallback (local capacity projection io:git).")


def _echo(av, *, json_output: bool) -> None:
    base_dir = get_ctx().paths.base_dir()
    base_dir = Path(base_dir() if callable(base_dir) else base_dir).expanduser().resolve()
    payload = {
        "enabled": bool(av.enabled),
        "git_path": av.git_path,
        "mode": av.mode,
        "reason": av.reason,
        "source": av.source,
        "base_dir": str(base_dir),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(f"enabled: {payload['enabled']}")
    if payload.get("git_path"):
        typer.echo(f"git_path: {payload['git_path']}")
    if payload.get("mode"):
        typer.echo(f"mode: {payload['mode']}")
    if payload.get("reason"):
        typer.echo(f"reason: {payload['reason']}")
    if payload.get("source"):
        typer.echo(f"source: {payload['source']}")
    typer.echo(f"base_dir: {payload['base_dir']}")


@app.command("status")
def status(json_output: bool = typer.Option(False, "--json", help="JSON output")) -> None:
    ctx = get_ctx()
    av = get_git_availability(base_dir=ctx.settings.base_dir)
    _echo(av, json_output=json_output)


@app.command("autodetect")
def autodetect(json_output: bool = typer.Option(False, "--json", help="JSON output")) -> None:
    ctx = get_ctx()
    av = autodetect_git(base_dir=ctx.settings.base_dir)
    _echo(av, json_output=json_output)


@app.command("enable")
def enable(json_output: bool = typer.Option(False, "--json", help="JSON output")) -> None:
    ctx = get_ctx()
    av = set_git_enabled(base_dir=ctx.settings.base_dir)
    _echo(av, json_output=json_output)
    if not av.enabled:
        raise typer.Exit(1)


@app.command("disable")
def disable(
    reason: str = typer.Option("disabled by operator", "--reason", help="Reason stored in local capacity projection"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    ctx = get_ctx()
    av = set_git_disabled(base_dir=ctx.settings.base_dir, reason=reason)
    _echo(av, json_output=json_output)

