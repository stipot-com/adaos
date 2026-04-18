from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import requests
import typer
from rich import print

from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token
from adaos.services.agent_context import get_ctx
from adaos.domain import ProcessSpec


app = typer.Typer(help="Runtime diagnostics and local process helpers.")


def _print_json_or_lines(payload: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(str(payload))


def _runtime_control_get(path: str, *, control: str | None = None) -> dict[str, Any]:
    base = resolve_control_base_url(explicit=control, prefer_local=True).rstrip("/")
    token = resolve_control_token(base_url=base)
    headers = {"X-AdaOS-Token": token}
    response = requests.get(base + path, headers=headers, timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON payload for {path}")
    return payload


def _runtime_control_post(path: str, *, body: dict[str, Any], control: str | None = None) -> dict[str, Any]:
    base = resolve_control_base_url(explicit=control, prefer_local=True).rstrip("/")
    token = resolve_control_token(base_url=base)
    headers = {"X-AdaOS-Token": token}
    response = requests.post(base + path, headers=headers, json=body, timeout=15.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON payload for {path}")
    return payload


@app.command("logs")
def show_logs() -> None:
    """Show runtime log file if present."""
    log_file = Path("runtime/runtime.log")
    if log_file.exists():
        print(f"[bold cyan]{log_file.read_text(encoding='utf-8')}[/bold cyan]")
    else:
        print("[yellow]No runtime logs yet.[/yellow]")


@app.command("restart")
def restart_runtime() -> None:
    """Restart runtime (MVP placeholder)."""
    print("[cyan]Runtime restarted (MVP placeholder)[/cyan]")


@app.command("start-cmd")
def start_cmd(*cmd: str) -> None:
    """Start an external command as an AdaOS-managed process."""
    if not cmd:
        raise typer.BadParameter("Specify a command to run")
    ctx = get_ctx()
    handle = asyncio.run(ctx.proc.start(ProcessSpec(name="user-cmd", cmd=list(cmd))))
    typer.echo(f"started: {handle}")


@app.command("start-demo")
def start_demo() -> None:
    """Start a demo coroutine for two seconds."""

    async def demo() -> None:
        await asyncio.sleep(2)

    ctx = get_ctx()
    handle = asyncio.run(ctx.proc.start(ProcessSpec(name="demo", entrypoint=demo)))
    typer.echo(f"started: {handle}")


@app.command("stop")
def stop(handle: str, timeout: float = 3.0) -> None:
    ctx = get_ctx()
    asyncio.run(ctx.proc.stop(handle, timeout_s=timeout))
    typer.echo("stopped")


@app.command("status")
def status(handle: str) -> None:
    ctx = get_ctx()
    s = asyncio.run(ctx.proc.status(handle))
    typer.echo(s)


@app.command("memory-status")
def memory_status(
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get("/api/supervisor/memory/status", control=control)
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    typer.echo(
        "memory: "
        f"mode={payload.get('current_profile_mode') or 'normal'} "
        f"control={payload.get('profile_control_mode') or '-'} "
        f"suspicion={payload.get('suspicion_state') or 'idle'} "
        f"sessions={payload.get('sessions_total') or 0}"
    )
    if payload.get("requested_profile_mode"):
        typer.echo(
            "requested: "
            f"mode={payload.get('requested_profile_mode')} "
            f"session={payload.get('requested_session_id') or '-'}"
        )
    if payload.get("last_session_id"):
        typer.echo(f"last session: {payload.get('last_session_id')}")
    if payload.get("selected_profiler_adapter"):
        typer.echo(f"adapter: {payload.get('selected_profiler_adapter')}")


@app.command("memory-sessions")
def memory_sessions(
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get("/api/supervisor/memory/sessions", control=control)
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
    typer.echo(f"sessions total: {payload.get('total') or 0}")
    if not sessions:
        typer.echo("sessions: (empty)")
        return
    for item in sessions[:20]:
        if not isinstance(item, dict):
            continue
        typer.echo(
            "session: "
            f"id={item.get('session_id') or '-'} "
            f"state={item.get('session_state') or '-'} "
            f"mode={item.get('profile_mode') or '-'} "
            f"publish={item.get('publish_state') or '-'}"
        )


@app.command("memory-session")
def memory_session(
    session_id: str,
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get(f"/api/supervisor/memory/sessions/{session_id}", control=control)
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    operations = payload.get("operations") if isinstance(payload.get("operations"), list) else []
    typer.echo(
        "session: "
        f"id={session.get('session_id') or '-'} "
        f"state={session.get('session_state') or '-'} "
        f"mode={session.get('profile_mode') or '-'} "
        f"publish={session.get('publish_state') or '-'}"
    )
    if session.get("trigger_reason"):
        typer.echo(f"trigger: {session.get('trigger_reason')}")
    if operations:
        typer.echo(f"operations: {len(operations)}")
        last = operations[-1] if isinstance(operations[-1], dict) else {}
        if last:
            typer.echo(
                "last operation: "
                f"event={last.get('event') or '-'} "
                f"seq={last.get('sequence') or '-'}"
            )


@app.command("memory-profile-start")
def memory_profile_start(
    profile_mode: str = typer.Option("sampled_profile", "--profile-mode", help="sampled_profile|trace_profile"),
    reason: str = typer.Option("cli.runtime.memory_profile_start", "--reason", help="Operator-visible reason"),
    trigger_source: str = typer.Option("operator", "--trigger-source", help="Intent source label"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_post(
        "/api/supervisor/memory/profile/start",
        body={
            "profile_mode": str(profile_mode or "sampled_profile"),
            "reason": str(reason or "cli.runtime.memory_profile_start"),
            "trigger_source": str(trigger_source or "operator"),
        },
        control=control,
    )
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    typer.echo(
        "memory profile start: "
        f"id={session.get('session_id') or '-'} "
        f"state={session.get('session_state') or '-'} "
        f"mode={session.get('profile_mode') or '-'}"
    )
    if payload.get("control_mode"):
        typer.echo(f"control mode: {payload.get('control_mode')}")


@app.command("memory-profile-stop")
def memory_profile_stop(
    session_id: str,
    reason: str = typer.Option("cli.runtime.memory_profile_stop", "--reason", help="Operator-visible reason"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_post(
        f"/api/supervisor/memory/profile/{session_id}/stop",
        body={"reason": str(reason or "cli.runtime.memory_profile_stop")},
        control=control,
    )
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    typer.echo(
        "memory profile stop: "
        f"id={session.get('session_id') or '-'} "
        f"state={session.get('session_state') or '-'}"
    )
    if payload.get("control_mode"):
        typer.echo(f"control mode: {payload.get('control_mode')}")


@app.command("memory-publish")
def memory_publish(
    session_id: str,
    reason: str = typer.Option("cli.runtime.memory_publish", "--reason", help="Operator-visible reason"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_post(
        "/api/supervisor/memory/publish",
        body={
            "session_id": str(session_id or "").strip(),
            "reason": str(reason or "cli.runtime.memory_publish"),
        },
        control=control,
    )
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    typer.echo(
        "memory publish: "
        f"id={session.get('session_id') or '-'} "
        f"publish={session.get('publish_state') or '-'}"
    )
    if payload.get("control_mode"):
        typer.echo(f"control mode: {payload.get('control_mode')}")
