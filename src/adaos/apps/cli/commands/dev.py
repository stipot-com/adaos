from __future__ import annotations

import json, os, shutil, subprocess, sys, traceback
import functools
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

import typer

from adaos.apps.cli.i18n import _
from adaos.apps.cli.commands.skill import _mgr
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import displayable_path
from adaos.services.root.service import (
    DeviceAuthorization,
    ArtifactDeleteResult,
    ArtifactListItem,
    ArtifactNotFoundError,
    ArtifactPublishResult,
    ArtifactUpdateResult,
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
from adaos.services.root_mcp.codex_bridge import (
    CodexBridgeProfile,
    DEFAULT_CODEX_TARGET_CAPABILITIES,
    build_codex_stdio_command,
    default_profile_paths,
    load_codex_bridge_profile,
    serve_codex_stdio_bridge,
    write_codex_bridge_profile,
)
from adaos.services.root_mcp.client import RootMcpClient, RootMcpClientConfig
from adaos.services.nats_config import normalize_nats_ws_url
from adaos.services.node_runtime_state import save_nats_runtime_config
from adaos.services.subnet_alias import load_subnet_alias, save_subnet_alias
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context, load_scenario

app = typer.Typer(help="Developer utilities for Root and Forge workflows.")
root_app = typer.Typer(help="Bootstrap and authenticate against the Root service.")
mcp_app = typer.Typer(help="Root MCP bridge and Codex / VS Code integration utilities.")
skill_app = typer.Typer(help="Manage owner skills in the local Forge workspace.")
scenario_app = typer.Typer(help="Manage owner scenarios in the local Forge workspace.")

app.add_typer(root_app, name="root")
root_app.add_typer(mcp_app, name="mcp")
app.add_typer(skill_app, name="skill")
app.add_typer(scenario_app, name="scenario")


def _run_safe(func):
    @functools.wraps(func)
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


def _echo_update_result(kind_label: str, result: ArtifactUpdateResult, json_output: bool) -> None:
    if json_output:
        payload = {
            "name": result.name,
            "source": _display_path(result.source_path),
            "target": _display_path(result.target_path),
            "version": result.version,
            "updated_at": result.updated_at,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.secho(f"{kind_label} '{result.name}' updated from Root Forge.", fg=typer.colors.GREEN)
    typer.echo(f"Source: {_display_path(result.source_path)}")
    typer.echo(f"Target: {_display_path(result.target_path)}")
    if result.version:
        typer.echo(f"Version: {result.version}")
    if result.updated_at:
        typer.echo(f"Updated at: {result.updated_at}")


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
def root_login(
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print device authorization URL+code and exit (no polling).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="When used with --print-only, output JSON (useful for bootstrap scripts).",
    ),
) -> None:
    service = _service()

    def on_authorize(auth: DeviceAuthorization) -> None:
        typer.echo("To authorize this device:")
        if auth.verification_uri_complete:
            typer.echo(f"  Open: {auth.verification_uri_complete}")
        else:
            typer.echo(f"  Open: {auth.verification_uri}")
            typer.echo(f"  Enter code: {auth.user_code}")
        if auth.zone_id:
            typer.echo(f"  zone_id: {auth.zone_id}")
        typer.echo(f"Polling every {auth.interval} seconds (expires in {auth.expires_in // 60} minutes)…")

    try:
        if print_only:
            auth = service.device_authorize()
            if json_output:
                payload = {
                    "user_code": auth.user_code,
                    "verification_uri": auth.verification_uri,
                    "verification_uri_complete": auth.verification_uri_complete,
                    "zone_id": auth.zone_id,
                    "expires_in": auth.expires_in,
                    "interval": auth.interval,
                }
                typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
                raise typer.Exit(0)

            typer.secho("Owner browser pairing:", fg=typer.colors.GREEN)
            typer.echo(f"  user_code: {auth.user_code}")
            if auth.verification_uri_complete:
                typer.echo(f"  verification_uri_complete: {auth.verification_uri_complete}")
            if auth.zone_id:
                typer.echo(f"  zone_id: {auth.zone_id}")
            typer.echo(f"  verification_uri: {auth.verification_uri}")
            typer.echo(f"  expires_in: {auth.expires_in}")
            typer.echo(f"  interval: {auth.interval}")
            raise typer.Exit(0)

        result = service.login(on_authorize=on_authorize)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_login_result(result)


@root_app.command("logs")
@_run_safe
def root_logs(
    minutes: int = typer.Option(30, "--minutes", help="How many minutes back to fetch (1..720)."),
    limit: int = typer.Option(2000, "--limit", help="Max lines to return (1..50000)."),
    hub_id: str = typer.Option(
        None,
        "--hub-id",
        help="Filter only lines containing this hub/subnet id (default: current subnet). Pass an empty string to disable hub filtering.",
    ),
    all_hubs: bool = typer.Option(False, "--all-hubs", help="Disable hub filtering (PowerShell-friendly)."),
    contains: str = typer.Option(None, "--contains", help="Filter only lines containing this substring."),
    token: str = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for dev logs. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN environment variables.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON."),
    save_dir: str = typer.Option(".adaos/root_logs", "--save-dir", help="Save logs snapshot into this directory."),
    save: bool = typer.Option(False, "--save", help="Save logs snapshot to a file in --save-dir."),
) -> None:
    service = _service()
    cfg = get_ctx().config
    # Default hub filter only when option is not provided at all.
    # Passing `--hub-id ""` disables hub filtering and allows searching across all root logs.
    if all_hubs:
        hub_id = ""
    elif hub_id is None:
        try:
            hub_id = cfg.subnet_id
        except Exception:
            hub_id = None
    try:
        result = service.dev_logs(minutes=minutes, limit=limit, hub_id=hub_id, contains=contains, root_token=token)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        typer.echo("No logs returned.")
        return
    if save:
        try:
            from datetime import datetime
            from pathlib import Path

            out_dir = Path(save_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            hub_tag = (hub_id if hub_id is not None else "auto") or "all"
            fname = f"root_logs_{ts}_min{int(minutes)}_hub{hub_tag}.log"
            out_path = out_dir / fname
            with out_path.open("w", encoding="utf-8") as f:
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    ts0 = it.get("ts")
                    stream0 = it.get("stream")
                    line0 = it.get("line")
                    try:
                        ts_s = str(int(ts0)) if isinstance(ts0, (int, float)) else str(ts0 or "")
                    except Exception:
                        ts_s = str(ts0 or "")
                    f.write(f"{ts_s} {stream0}: {line0}\n")
            typer.echo(f"Saved: {out_path}")
        except Exception as exc:
            _print_error(f"failed to save logs: {exc}")
    for it in items:
        if not isinstance(it, dict):
            continue
        ts = it.get("ts")
        stream = it.get("stream")
        line = it.get("line")
        try:
            ts_s = str(int(ts)) if isinstance(ts, (int, float)) else str(ts or "")
        except Exception:
            ts_s = str(ts or "")
        typer.echo(f"{ts_s} {stream}: {line}")


@root_app.command("log-files")
@_run_safe
def root_log_files(
    contains: str = typer.Option(None, "--contains", help="Filter filenames containing this substring."),
    limit: int = typer.Option(500, "--limit", help="Max files to return (1..5000)."),
    token: str = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for dev logs. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN environment variables.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    service = _service()
    try:
        result = service.dev_log_files(contains=contains, limit=limit, root_token=token)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        typer.echo("No log files returned.")
        return
    for it in items:
        if not isinstance(it, dict):
            continue
        rel = it.get("rel") or it.get("name") or ""
        bytes_ = it.get("bytes")
        mtime_ms = it.get("mtime_ms")
        typer.echo(f"{rel} bytes={bytes_} mtime_ms={mtime_ms}")


@root_app.command("log-tail")
@_run_safe
def root_log_tail(
    file: str = typer.Option(..., "--file", help="Relative log file path from backend logs dir (see `log-files`)."),
    lines: int = typer.Option(200, "--lines", help="How many lines from the end (1..50000)."),
    max_bytes: int = typer.Option(2_000_000, "--max-bytes", help="Max bytes to read from the end."),
    token: str = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for dev logs. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN environment variables.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON."),
    save_dir: str = typer.Option(".adaos/root_logs", "--save-dir", help="Save log tail into this directory."),
    save: bool = typer.Option(False, "--save", help="Save log tail to a file in --save-dir."),
) -> None:
    service = _service()
    try:
        result = service.dev_log_tail(file=file, lines=lines, max_bytes=max_bytes, root_token=token)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    out_lines = result.get("lines") if isinstance(result, dict) else None
    rel = (result.get("rel") if isinstance(result, dict) else None) or file
    if not isinstance(out_lines, list) or not out_lines:
        typer.echo("No log lines returned.")
        return
    if save:
        try:
            from datetime import datetime
            from pathlib import Path

            out_dir = Path(save_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_rel = str(rel).replace("/", "_").replace("\\", "_")
            out_path = out_dir / f"log_tail_{ts}_{safe_rel}.log"
            with out_path.open("w", encoding="utf-8") as f:
                for ln in out_lines:
                    f.write(str(ln) + "\n")
            typer.echo(f"Saved: {out_path}")
        except Exception as exc:
            _print_error(f"failed to save log tail: {exc}")
    for ln in out_lines:
        typer.echo(str(ln))


def _echo_login_result(result: RootLoginResult) -> None:
    typer.secho(f"Owner {result.owner_id} authenticated.", fg=typer.colors.GREEN)
    if result.subnet_id:
        typer.echo(f"Subnet ID: {result.subnet_id}")
    typer.echo(f"Workspace: {_display_path(result.workspace_path)}")


def _repo_root_dir() -> Path:
    ctx = get_ctx()
    try:
        raw = ctx.paths.repo_root()
        return Path(raw() if callable(raw) else raw)
    except Exception:
        return Path.cwd()


def _dev_root_http_client(*, root_url: str | None = None) -> tuple[RootHttpClient, Any]:
    from adaos.services.node_config import load_config, _expand_path as _expand_path

    ctx = get_ctx()
    cfg = load_config(ctx=ctx)
    ca = _expand_path(cfg.root_settings.ca_cert, "keys/ca.cert")
    cert = _expand_path(cfg.subnet_settings.hub.cert, "keys/hub_cert.pem")
    key = _expand_path(cfg.subnet_settings.hub.key, "keys/hub_private.pem")
    verify: str | bool = True
    if os.getenv("ADAOS_ROOT_VERIFY_CA", "0") == "1":
        verify = str(ca) if ca.exists() else True
    cert_tuple = (str(cert), str(key)) if cert.exists() and key.exists() else None
    base = str(root_url or cfg.root_settings.base_url or ctx.settings.api_base or "").strip()
    return RootHttpClient(base_url=base, verify=verify, cert=cert_tuple), cfg


def _resolve_root_mcp_management_client(
    *,
    root_url: str | None = None,
    owner_token: str | None = None,
) -> tuple[RootMcpClient, Any, str]:
    from adaos.services.root.service import RootAuthService, RootAuthError

    http, cfg = _dev_root_http_client(root_url=root_url)
    explicit_owner_token = str(owner_token or os.getenv("ROOT_TOKEN") or os.getenv("ADAOS_ROOT_TOKEN") or "").strip() or None
    if explicit_owner_token:
        headers = {"X-Owner-Token": explicit_owner_token}
        managed_http = RootHttpClient(base_url=http.base_url, verify=http.verify, cert=http.cert, default_headers=headers)
        return (
            RootMcpClient(
                RootMcpClientConfig(root_url=http.base_url, extra_headers=headers),
                http=managed_http,
            ),
            cfg,
            "owner_token",
        )

    try:
        access_token = RootAuthService(http=http).get_access_token(cfg)
    except RootAuthError as exc:
        raise RootServiceError(f"{exc}. Run 'adaos dev root login' or pass --owner-token.") from exc
    managed_http = RootHttpClient(
        base_url=http.base_url,
        verify=http.verify,
        cert=http.cert,
        default_headers={"Authorization": f"Bearer {access_token}"},
    )
    return (
        RootMcpClient(
            RootMcpClientConfig(root_url=http.base_url, access_token=access_token),
            http=managed_http,
        ),
        cfg,
        "owner_bearer",
    )


def _coalesce_target_id(cfg: Any, *, target_id: str | None, subnet_id: str | None) -> str:
    explicit_target_id = str(target_id or "").strip()
    if explicit_target_id:
        return explicit_target_id
    token = str(subnet_id or getattr(cfg, "subnet_id", None) or "").strip()
    if token:
        return f"hub:{token}"
    raise RootServiceError("Unable to infer target_id. Pass --target-id or configure subnet_id.")


def _coalesce_zone(cfg: Any, *, zone: str | None) -> str | None:
    explicit_zone = str(zone or "").strip()
    if explicit_zone:
        return explicit_zone
    for candidate in (getattr(cfg, "zone_id", None), os.getenv("ADAOS_ZONE_ID"), os.getenv("ZONE_ID")):
        token = str(candidate or "").strip()
        if token:
            return token
    return None


@mcp_app.command("prepare-codex")
@_run_safe
def prepare_codex(
    name: str = typer.Option("adaos-test-hub", "--name", help="MCP server name to register in Codex."),
    target_id: str | None = typer.Option(None, "--target-id", help="Managed target id. Defaults to hub:<subnet_id>."),
    root_url: str | None = typer.Option(None, "--root-url", help="Explicit Root base URL."),
    subnet_id: str | None = typer.Option(None, "--subnet-id", help="Explicit subnet scope for the generated profile."),
    zone: str | None = typer.Option(None, "--zone", help="Explicit zone for the generated profile."),
    capability_profile: str = typer.Option("ProfileOpsRead", "--capability-profile", help="Named capability profile for MCP session lease bootstrap."),
    bootstrap_mode: str = typer.Option("mcp-session", "--bootstrap-mode", help="Bootstrap mode: mcp-session or access-token."),
    ttl_seconds: int = typer.Option(28_800, "--ttl-seconds", help="Issued MCP token TTL in seconds."),
    owner_token: str | None = typer.Option(None, "--owner-token", help="Owner token for direct root auth when device login is not configured."),
    ensure_target: bool = typer.Option(True, "--ensure-target/--no-ensure-target", help="Create a minimal test-hub target descriptor if it is missing."),
    apply_codex: bool = typer.Option(False, "--apply-codex", help="Run 'codex mcp add' after writing the profile and token files."),
    profile_file: str | None = typer.Option(None, "--profile-file", help="Override the generated profile file path."),
    token_file: str | None = typer.Option(None, "--token-file", help="Override the generated access-token file path."),
    capability: List[str] = typer.Option([], "--capability", help="Extra Root MCP capabilities to add to the issued Codex token."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable setup output."),
) -> None:
    client, cfg, auth_mode = _resolve_root_mcp_management_client(root_url=root_url, owner_token=owner_token)
    inferred_subnet_id = str(subnet_id or getattr(cfg, "subnet_id", None) or "").strip() or None
    effective_target_id = _coalesce_target_id(cfg, target_id=target_id, subnet_id=inferred_subnet_id)
    effective_zone = _coalesce_zone(cfg, zone=zone)

    target_payload: dict[str, Any] | None = None
    try:
        target_payload = client.get_managed_target(effective_target_id)
    except Exception:
        if ensure_target:
            minimal_target = {
                "target_id": effective_target_id,
                "title": f"Hub {inferred_subnet_id or effective_target_id}",
                "kind": "hub",
                "environment": "test",
                "status": "registered",
                "zone": effective_zone,
                "subnet_id": inferred_subnet_id,
                "transport": {"channel": "hub_root_protocol", "mode": "manual_setup"},
                "operational_surface": {
                    "published_by": "skill:infra_access_skill",
                    "enabled": False,
                    "availability": "planned",
                },
                "meta": {"registry_source": "codex_prepare"},
            }
            target_payload = client.upsert_managed_target(minimal_target)
        else:
            raise RootServiceError(f"Managed target '{effective_target_id}' is not registered on root.")

    target_block = dict(target_payload.get("target") or {}) if isinstance(target_payload, dict) else {}
    effective_subnet_id = inferred_subnet_id or str(target_block.get("subnet_id") or "").strip() or None
    effective_zone = effective_zone or str(target_block.get("zone") or "").strip() or None

    requested_capabilities = list(DEFAULT_CODEX_TARGET_CAPABILITIES)
    for item in capability:
        token = str(item or "").strip()
        if token and token not in requested_capabilities:
            requested_capabilities.append(token)

    bootstrap_token = str(bootstrap_mode or "").strip().lower().replace("_", "-")
    if bootstrap_token not in {"mcp-session", "access-token"}:
        raise RootServiceError("bootstrap_mode must be either 'mcp-session' or 'access-token'.")

    if bootstrap_token == "mcp-session":
        issued = client.issue_target_mcp_session(
            effective_target_id,
            audience="codex-vscode",
            ttl_seconds=ttl_seconds,
            capability_profile=capability_profile,
            capabilities=requested_capabilities,
            note="Codex VS Code MCP bridge",
        )
    else:
        issued = client.issue_target_access_token(
            effective_target_id,
            audience="codex-vscode",
            ttl_seconds=ttl_seconds,
            capabilities=requested_capabilities,
            note="Codex VS Code MCP bridge",
        )
    response = dict(issued.get("response") or {})
    result = dict(response.get("result") or {})
    access_token = str(result.get("access_token") or "").strip()
    if not access_token:
        raise RootServiceError("Root MCP did not return an access_token for Codex setup.")

    repo_root = _repo_root_dir()
    default_profile_file, default_token_file = default_profile_paths(repo_root, name)
    stored_profile = CodexBridgeProfile(
        root_url=str(root_url or getattr(cfg.root_settings, "base_url", None) or get_ctx().settings.api_base or "").strip(),
        target_id=effective_target_id,
        subnet_id=None if bootstrap_token == "mcp-session" else effective_subnet_id,
        zone=None if bootstrap_token == "mcp-session" else effective_zone,
        bootstrap_mode="mcp_session_lease" if bootstrap_token == "mcp-session" else "access_token",
        session_id=result.get("session_id") if bootstrap_token == "mcp-session" else None,
        capability_profile=capability_profile if bootstrap_token == "mcp-session" else None,
        server_name=name,
        audience="codex-vscode",
        generated_at=result.get("issued_at") if isinstance(result.get("issued_at"), str) else None,
        capabilities=list(result.get("capabilities") or requested_capabilities),
    )
    resolved_profile_file, resolved_token_file = write_codex_bridge_profile(
        profile_path=profile_file or default_profile_file,
        token_path=token_file or default_token_file,
        profile=stored_profile,
        access_token=access_token,
    )
    codex_add_cmd = build_codex_stdio_command(
        python_executable=sys.executable,
        profile_path=resolved_profile_file,
        server_name=name,
    )

    if apply_codex:
        if not shutil.which("codex"):
            raise RootServiceError("Codex CLI is not available on PATH.")
        subprocess.run(["codex", "mcp", "remove", name], check=False, capture_output=True, text=True)
        completed = subprocess.run(codex_add_cmd, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RootServiceError((completed.stderr or completed.stdout or "codex mcp add failed").strip())

    payload = {
        "ok": True,
        "auth_mode": auth_mode,
        "server_name": name,
        "bootstrap_mode": bootstrap_token,
        "root_url": stored_profile.root_url,
        "subnet_id": stored_profile.subnet_id,
        "zone": stored_profile.zone,
        "target_id": effective_target_id,
        "session_id": result.get("session_id"),
        "capability_profile": stored_profile.capability_profile,
        "profile_file": str(resolved_profile_file),
        "token_file": str(resolved_token_file),
        "token_id": result.get("token_id"),
        "expires_at": result.get("expires_at"),
        "capabilities": list(result.get("capabilities") or []),
        "codex_add_command": codex_add_cmd,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.secho("Codex test-hub MCP profile prepared.", fg=typer.colors.GREEN)
    typer.echo(f"Bootstrap mode: {payload['bootstrap_mode']}")
    typer.echo(f"Root URL: {payload['root_url']}")
    typer.echo(f"Target ID: {payload['target_id']}")
    if payload["subnet_id"]:
        typer.echo(f"Subnet ID: {payload['subnet_id']}")
    if payload["zone"]:
        typer.echo(f"Zone: {payload['zone']}")
    typer.echo(f"Profile: {payload['profile_file']}")
    typer.echo(f"Token file: {payload['token_file']}")
    typer.echo(f"Expires at: {payload['expires_at'] or 'unknown'}")
    typer.echo("")
    typer.echo("Add this MCP server to Codex:")
    typer.echo(f"  {subprocess.list2cmdline(codex_add_cmd)}")
    if apply_codex:
        typer.secho("Codex MCP server updated in ~/.codex/config.toml.", fg=typer.colors.GREEN)


@mcp_app.command("serve")
@_run_safe
def serve_codex(
    profile: str | None = typer.Option(None, "--profile", help="Path to the generated Codex MCP profile JSON."),
    root_url: str | None = typer.Option(None, "--root-url", help="Explicit Root base URL when not using a profile file."),
    target_id: str | None = typer.Option(None, "--target-id", help="Managed target id when not using a profile file."),
    subnet_id: str | None = typer.Option(None, "--subnet-id", help="Subnet scope when not using a profile file."),
    zone: str | None = typer.Option(None, "--zone", help="Zone when not using a profile file."),
    access_token: str | None = typer.Option(None, "--access-token", help="Inline Root MCP access token for manual testing."),
    access_token_file: str | None = typer.Option(None, "--access-token-file", help="Path to a Root MCP access token file."),
) -> None:
    try:
        if any([profile, root_url, target_id, subnet_id, zone, access_token, access_token_file]):
            fallback = load_codex_bridge_profile(profile) if profile else None
            effective_profile = CodexBridgeProfile(
                root_url=str(root_url or (fallback.root_url if fallback else "")).strip(),
                target_id=target_id or (fallback.target_id if fallback else None),
                subnet_id=subnet_id or (fallback.subnet_id if fallback else None),
                zone=zone or (fallback.zone if fallback else None),
                bootstrap_mode=(fallback.bootstrap_mode if fallback else "mcp_session_lease"),
                session_id=(fallback.session_id if fallback else None),
                capability_profile=(fallback.capability_profile if fallback else None),
                access_token=access_token or (fallback.access_token if fallback else None),
                access_token_file=access_token_file or (fallback.access_token_file if fallback else None),
                server_name=(fallback.server_name if fallback else "adaos-test-hub"),
                audience=(fallback.audience if fallback else "codex-vscode"),
                generated_at=(fallback.generated_at if fallback else None),
                capabilities=list((fallback.capabilities if fallback else DEFAULT_CODEX_TARGET_CAPABILITIES)),
            )
        else:
            effective_profile = load_codex_bridge_profile(profile)
        serve_codex_stdio_bridge(effective_profile)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command("telegram")
def dev_login(
    status: Optional[str] = typer.Option(None, "--status", help="Check pairing status for code."),
    revoke: Optional[str] = typer.Option(None, "--revoke", help="Revoke pairing code."),
    hub: Optional[str] = typer.Option(None, "--hub", help="Explicit hub id to bind (overrides defaults)."),
):
    ctx = get_ctx()

    # Build Root client using node.yaml mTLS materials (hub cert/key + CA),
    # because Settings may not include PKI fields.
    try:
        from adaos.services.node_config import load_config, _expand_path as _expand_path

        cfg = load_config(ctx=ctx)
        ca = _expand_path(cfg.root_settings.ca_cert, "keys/ca.cert")
        cert = _expand_path(cfg.subnet_settings.hub.cert, "keys/hub_cert.pem")
        key = _expand_path(cfg.subnet_settings.hub.key, "keys/hub_private.pem")
        verify = True
        # Keep system CA by default; allow pinning via ADAOS_ROOT_VERIFY_CA=1.
        if os.getenv("ADAOS_ROOT_VERIFY_CA", "0") == "1":
            verify = str(ca) if ca.exists() else True
        cert_tuple = (str(cert), str(key)) if cert.exists() and key.exists() else None
        base = cfg.root_settings.base_url or ctx.settings.api_base
        client = RootHttpClient(base_url=base, verify=verify, cert=cert_tuple)
    except Exception:
        base = ctx.settings.api_base
        client = RootHttpClient(base_url=base)
    # Prefer explicit CLI option; otherwise always use canonical subnet_id for pairing,
    # because backend stores WS tokens keyed by canonical hub id (not alias).
    try:
        hub_id = hub or (cfg.subnet_id if "cfg" in locals() and cfg else None) or ctx.settings.subnet_id or ctx.settings.default_hub or ctx.settings.owner_id
    except Exception:
        hub_id = hub or ctx.settings.subnet_id or ctx.settings.default_hub or ctx.settings.owner_id
    if status:
        data = client.request("GET", "/io/tg/pair/status", params={"code": status})
        typer.echo(data)
        raise typer.Exit(0)
    if revoke:
        data = client.request("POST", "/io/tg/pair/revoke", params={"code": revoke})
        typer.echo(data)
        raise typer.Exit(0)
    print("hub_log", hub_id)
    # Send hub id in JSON body using the expected key; avoid query param 'hub_id' which backend ignores
    data = client.request("POST", "/io/tg/pair/create", json={"code": "PING", "hub_id": hub_id})
    # TODO Перенести обработку ошибок из метода _request на уровень  логики.
    """ if resp.status_code != 200:
        _print_error(f"API error: {resp.status_code} {resp.text}")
        raise typer.Exit(1) """
    code = data.get("pair_code") or data.get("code")
    if not code:
        _print_error("No pair_code in response.")
        raise typer.Exit(1)
    deep_link = data.get("deep_link") or f"https://t.me/adaos_home_bot?start={code}"
    typer.secho("Telegram pairing:", fg=typer.colors.GREEN)
    typer.echo(f"  pair_code: {code}")
    typer.echo(f"  deep_link: {deep_link}")
    if data.get("expires_at"):
        typer.echo(f"  expires_at: {data['expires_at']}")

    # Persist NATS WS credentials into runtime state so hub can preconfigure WS
    hub_id_resp = data.get("hub_id") or hub_id
    hub_token = data.get("hub_nats_token")
    # Always use local canonical hub id for WS user to avoid alias-based mismatches
    local_hub_id = ctx.settings.subnet_id or hub_id_resp
    nats_user = (f"hub_{local_hub_id}" if local_hub_id else None) or data.get("nats_user") or (f"hub_{hub_id_resp}" if hub_id_resp else None)
    nats_ws_url = normalize_nats_ws_url(data.get("nats_ws_url"), fallback=None)
    if not hub_token:
        try:
            token_data = client.request("POST", "/v1/hub/nats/token")
            if isinstance(token_data, dict):
                hub_token = token_data.get("hub_nats_token") or hub_token
                hub_id_resp = token_data.get("hub_id") or hub_id_resp
                nats_ws_url = normalize_nats_ws_url(token_data.get("nats_ws_url") or nats_ws_url)
                if not nats_user:
                    nats_user = token_data.get("nats_user") or nats_user
        except Exception:
            pass
    if not nats_ws_url:
        nats_ws_url = normalize_nats_ws_url(None)
    if hub_id_resp and hub_token and nats_user:
        try:
            save_nats_runtime_config(
                ws_url=nats_ws_url,
                user=nats_user,
                password=hub_token,
            )
            effective_hub_id = str(local_hub_id or hub_id_resp or "").strip() or None
            if effective_hub_id and not load_subnet_alias(subnet_id=effective_hub_id):
                save_subnet_alias("hub", subnet_id=effective_hub_id)
            typer.echo("Saved NATS credentials to runtime state")

            # Ensure route rules exist so RouterService can route ui.notify to telegram.
            try:
                import yaml as _yaml
                from pathlib import Path as _Path

                base_dir = ctx.paths.base_dir()
                rules_path = _Path(getattr(ctx.settings, "route_rules_path", "") or (base_dir / "route_rules.yaml"))
                if not rules_path.is_absolute():
                    rules_path = (base_dir / rules_path).resolve()
                rules: dict = {}
                if rules_path.exists():
                    try:
                        rules = _yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
                    except Exception:
                        rules = {}
                if not isinstance(rules, dict):
                    rules = {}
                if "rules" not in rules or not isinstance(rules.get("rules"), list):
                    rules = {"version": 1, "rules": []}
                items: list = list(rules.get("rules") or [])
                have_tg = any(isinstance(r, dict) and ((r.get("target") or {}).get("io_type") == "telegram") for r in items)
                have_stdout = any(isinstance(r, dict) and ((r.get("target") or {}).get("io_type") == "stdout") for r in items)
                changed = False
                if not have_tg:
                    items.append({"match": {}, "target": {"node_id": "this", "kind": "io_type", "io_type": "telegram"}, "priority": 60})
                    changed = True
                if not have_stdout:
                    items.append({"match": {}, "target": {"node_id": "this", "kind": "io_type", "io_type": "stdout"}, "priority": 50})
                    changed = True
                if changed:
                    rules["rules"] = items
                    rules_path.write_text(_yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    typer.echo(f"Updated route rules at {rules_path}")
            except Exception as e:
                _print_error(f"Failed to create route_rules.yaml: {e}")
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


@skill_app.command("update")
def skill_update(
    name: str = typer.Argument(..., help="skill name in DEV space"),
    json_output: bool = typer.Option(False, "--json", help="Render output as JSON."),
) -> None:
    service = _service()
    try:
        result = service.update_skill(name)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_update_result("Skill", result, json_output)


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


@scenario_app.command("update")
def scenario_update(
    name: str = typer.Argument(..., help="scenario name in DEV space"),
    json_output: bool = typer.Option(False, "--json", help="Render output as JSON."),
) -> None:
    service = _service()
    try:
        result = service.update_scenario(name)
    except ArtifactNotFoundError as exc:
        _print_error(str(exc))
        raise typer.Exit(exc.exit_code)
    except RootServiceError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)
    _echo_update_result("Scenario", result, json_output)


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
