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
    if payload.get("suspicion_reason"):
        typer.echo(f"suspicion reason: {payload.get('suspicion_reason')}")
    if payload.get("rss_growth_bytes") is not None:
        typer.echo(
            "rss growth: "
            f"bytes={payload.get('rss_growth_bytes')} "
            f"per_min={payload.get('rss_growth_bytes_per_min') or 0}"
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


@app.command("memory-telemetry")
def memory_telemetry(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Tail sample count"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get(f"/api/supervisor/memory/telemetry?limit={int(limit)}", control=control)
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    typer.echo(f"telemetry samples: {payload.get('total') or 0}")
    if not items:
        typer.echo("telemetry: (empty)")
        return
    last = items[-1] if isinstance(items[-1], dict) else {}
    typer.echo(
        "last sample: "
        f"mode={last.get('profile_mode') or 'normal'} "
        f"suspicion={last.get('suspicion_state') or 'idle'} "
        f"family_rss={last.get('family_rss_bytes') or 0} "
        f"growth={last.get('rss_growth_bytes') or 0}"
    )


@app.command("memory-incidents")
def memory_incidents(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Incident count"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get(f"/api/supervisor/memory/incidents?limit={int(limit)}", control=control)
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    incidents = payload.get("incidents") if isinstance(payload.get("incidents"), list) else []
    typer.echo(f"incidents total: {payload.get('total') or 0}")
    if not incidents:
        typer.echo("incidents: (empty)")
        return
    for item in incidents[:20]:
        if not isinstance(item, dict):
            continue
        typer.echo(
            "incident: "
            f"id={item.get('session_id') or '-'} "
            f"state={item.get('session_state') or '-'} "
            f"mode={item.get('profile_mode') or '-'} "
            f"suspected={bool(item.get('suspected_leak'))} "
            f"retry_depth={item.get('retry_depth') or 0}"
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
    if session.get("retry_of_session_id"):
        typer.echo(
            "retry chain: "
            f"from={session.get('retry_of_session_id')} "
            f"root={session.get('retry_root_session_id') or session.get('retry_of_session_id')} "
            f"depth={session.get('retry_depth') or 0}"
        )
    if operations:
        typer.echo(f"operations: {len(operations)}")
        last = operations[-1] if isinstance(operations[-1], dict) else {}
        if last:
            typer.echo(
                "last operation: "
                f"event={last.get('event') or '-'} "
                f"seq={last.get('sequence') or '-'}"
            )
    telemetry = payload.get("telemetry") if isinstance(payload.get("telemetry"), list) else []
    if telemetry:
        typer.echo(f"telemetry: {len(telemetry)}")
    artifact_refs = session.get("artifact_refs") if isinstance(session.get("artifact_refs"), list) else []
    if artifact_refs:
        typer.echo(f"artifacts: {len(artifact_refs)}")
        first = artifact_refs[0] if isinstance(artifact_refs[0], dict) else {}
        if first:
            typer.echo(f"first artifact: {first.get('artifact_id') or '-'}")
    if session.get("published_ref"):
        typer.echo(f"published ref: {session.get('published_ref')}")


@app.command("memory-artifact")
def memory_artifact(
    session_id: str,
    artifact_id: str,
    max_bytes: int = typer.Option(256 * 1024, "--max-bytes", min=1, max=1024 * 1024, help="Maximum bytes to pull"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_get(
        f"/api/supervisor/memory/sessions/{session_id}/artifacts/{artifact_id}?offset=0&max_bytes={int(max_bytes)}",
        control=control,
    )
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    typer.echo(
        "artifact: "
        f"id={artifact.get('artifact_id') or '-'} "
        f"kind={artifact.get('kind') or '-'} "
        f"exists={bool(payload.get('exists'))}"
    )
    transfer = payload.get("transfer") if isinstance(payload.get("transfer"), dict) else {}
    if transfer:
        typer.echo(
            "transfer: "
            f"encoding={transfer.get('encoding') or '-'} "
            f"chunk={transfer.get('chunk_bytes') or 0} "
            f"remaining={transfer.get('remaining_bytes') or 0} "
            f"truncated={bool(transfer.get('truncated'))}"
        )
    content = payload.get("content")
    if isinstance(content, dict):
        typer.echo(f"content keys: {', '.join(sorted(str(key) for key in content.keys())[:8])}")
        return
    if isinstance(payload.get("text"), str):
        typer.echo(f"text chars: {len(payload.get('text') or '')}")
        return
    if isinstance(payload.get("content_base64"), str):
        typer.echo(f"base64 chars: {len(payload.get('content_base64') or '')}")


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


@app.command("memory-profile-retry")
def memory_profile_retry(
    session_id: str,
    reason: str = typer.Option("cli.runtime.memory_profile_retry", "--reason", help="Operator-visible reason"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    payload = _runtime_control_post(
        f"/api/supervisor/memory/profile/{session_id}/retry",
        body={"reason": str(reason or "cli.runtime.memory_profile_retry")},
        control=control,
    )
    if json_output:
        _print_json_or_lines(payload, json_output=True)
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    typer.echo(
        "memory profile retry: "
        f"from={payload.get('retry_of_session_id') or session_id} "
        f"to={session.get('session_id') or '-'} "
        f"state={session.get('session_state') or '-'} "
        f"mode={session.get('profile_mode') or '-'}"
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
    if session.get("published_ref"):
        typer.echo(f"published ref: {session.get('published_ref')}")
    if payload.get("control_mode"):
        typer.echo(f"control mode: {payload.get('control_mode')}")
