from __future__ import annotations

import json
import os
import re
import ssl
from pathlib import Path
from typing import Any

import requests
import typer

from adaos.services.agent_context import get_ctx
from adaos.apps.cli.active_control import probe_control_api, resolve_control_base_url, resolve_control_token
from adaos.services.join_codes import create as create_join_code
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root.service import RootAuthError, RootAuthService

app = typer.Typer(help="Hub operations.")
join_code_app = typer.Typer(help="Join-code management.")
app.add_typer(join_code_app, name="join-code")
root_link_app = typer.Typer(help="Hub-root link diagnostics and control.")
app.add_typer(root_link_app, name="root")
sidecar_app = typer.Typer(help="Realtime sidecar diagnostics and control.")
root_link_app.add_typer(sidecar_app, name="sidecar")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict) and "code" in data:
            typer.echo(str(data["code"]))
        else:
            typer.echo(str(data))


def _local_api_base() -> str:
    # Backward-compatible helper used by legacy code; prefer env-configured active server.
    return resolve_control_base_url()


def _local_control_token(base_url: str) -> str:
    try:
        return resolve_control_token(base_url=base_url)
    except TypeError:
        # Test doubles may still expose the older one-argument signature.
        return resolve_control_token()


def _root_verify_from_conf(conf: Any) -> str | bool | ssl.SSLContext:
    verify: str | bool | ssl.SSLContext = True
    try:
        ca_path = conf.ca_cert_path()
        if isinstance(ca_path, Path) and ca_path.exists():
            try:
                import certifi  # type: ignore

                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            try:
                ctx.load_verify_locations(cafile=str(ca_path))
            except Exception:
                pass
            verify = ctx
    except Exception:
        verify = True
    return verify


def _resolve_root_base_url(conf: Any, explicit_root: str | None = None) -> str:
    base_url = (
        explicit_root
        or getattr(getattr(conf, "root_settings", None), "base_url", None)
        or "https://api.inimatic.com"
    ).rstrip("/")
    zone_id = (
        str(os.getenv("ADAOS_ZONE_ID") or getattr(conf, "zone_id", None) or "")
        .strip()
        .lower()
    )
    if not re.fullmatch(r"[a-z]{2}", zone_id or ""):
        zone_id = ""
    if zone_id == "ru" and base_url in {"https://api.inimatic.com", "http://api.inimatic.com"}:
        return f"https://{zone_id}.api.inimatic.com"
    return base_url


def _local_memory_artifact_pull(
    *,
    session_id: str,
    artifact_id: str,
    source_api_path: str | None = None,
    max_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    base = resolve_control_base_url()
    token = _local_control_token(base)
    path = str(source_api_path or f"/api/supervisor/memory/sessions/{session_id}/artifacts/{artifact_id}").strip()
    if not path.startswith("/"):
        path = "/" + path
    separator = "&" if "?" in path else "?"
    response = requests.get(
        base.rstrip("/") + path + f"{separator}offset=0&max_bytes={int(max_bytes)}",
        headers={"X-AdaOS-Token": token},
        timeout=10.0,
    )
    response.raise_for_status()
    return dict(response.json() or {})


@root_link_app.command("status")
def hub_root_status(json_output: bool = typer.Option(False, "--json", help="JSON output")) -> None:
    """
    Print current hub-root health snapshot (derived from /api/node/reliability).
    """
    import requests

    base = resolve_control_base_url()
    token = _local_control_token(base)
    url = base + "/api/node/reliability"
    headers = {"X-AdaOS-Token": token}
    r = requests.get(url, headers=headers, timeout=5.0)
    r.raise_for_status()
    data = r.json()
    if json_output:
        _print(data, json_output=True)
        return
    runtime = (data or {}).get("runtime") if isinstance((data or {}).get("runtime"), dict) else {}
    overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    hub_member = runtime.get("hub_member_channels") if isinstance(runtime.get("hub_member_channels"), dict) else {}
    hub_member_connection_state = runtime.get("hub_member_connection_state") if isinstance(runtime.get("hub_member_connection_state"), dict) else {}
    sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    media_runtime = runtime.get("media_runtime") if isinstance(runtime.get("media_runtime"), dict) else {}
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
    coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
    root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
    route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
    root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
    route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
    tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
    llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
    route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
    route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
    route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control_lifecycle_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
        ),
        {},
    )
    core_update_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
        ),
        {},
    )
    hub_member_channels = hub_member.get("channels") if isinstance(hub_member.get("channels"), dict) else {}
    member_command = hub_member_channels.get("hub_member.command") if isinstance(hub_member_channels.get("hub_member.command"), dict) else {}
    member_sync = hub_member_channels.get("hub_member.sync") if isinstance(hub_member_channels.get("hub_member.sync"), dict) else {}
    member_links = hub_member_connection_state.get("members") if isinstance(hub_member_connection_state.get("members"), list) else []
    rollout = hub_member_connection_state.get("update_rollout") if isinstance(hub_member_connection_state.get("update_rollout"), dict) else {}
    rollout_counts = rollout.get("rollout_counts") if isinstance(rollout.get("rollout_counts"), dict) else {}
    snapshot_counts = rollout.get("snapshot_counts") if isinstance(rollout.get("snapshot_counts"), dict) else {}
    member_labels = [
        str(item.get("label") or item.get("node_id") or "member")
        for item in member_links[:4]
        if isinstance(item, dict)
    ]
    member_runtime = [
        str(item.get("snapshot_runtime_git_short_commit") or item.get("snapshot_runtime_version") or "-")
        for item in member_links[:4]
        if isinstance(item, dict)
    ]
    member_update = [
        str(item.get("snapshot_update_state") or item.get("last_hub_core_update_state") or "-")
        for item in member_links[:4]
        if isinstance(item, dict)
    ]
    sync_assessment = sync_runtime.get("assessment") if isinstance(sync_runtime.get("assessment"), dict) else {}
    sync_transport = sync_runtime.get("transport") if isinstance(sync_runtime.get("transport"), dict) else {}
    sync_webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    sync_selected = str(sync_runtime.get("selected_webspace_id") or "").strip() or "default"
    selected_sync = sync_webspaces.get(sync_selected) if isinstance(sync_webspaces.get(sync_selected), dict) else {}
    sync_action_overrides = sync_runtime.get("action_overrides") if isinstance(sync_runtime.get("action_overrides"), dict) else {}
    sync_recovery_playbook = sync_runtime.get("recovery_playbook") if isinstance(sync_runtime.get("recovery_playbook"), dict) else {}
    sync_recovery_guidance = sync_runtime.get("recovery_guidance") if isinstance(sync_runtime.get("recovery_guidance"), dict) else {}
    sync_selected_webspace = sync_runtime.get("selected_webspace") if isinstance(sync_runtime.get("selected_webspace"), dict) else {}
    sync_webspace_guidance = sync_runtime.get("webspace_guidance") if isinstance(sync_runtime.get("webspace_guidance"), dict) else {}
    media_assessment = media_runtime.get("assessment") if isinstance(media_runtime.get("assessment"), dict) else {}
    media_transport = media_runtime.get("transport") if isinstance(media_runtime.get("transport"), dict) else {}
    media_counts = media_runtime.get("counts") if isinstance(media_runtime.get("counts"), dict) else {}
    media_paths = media_runtime.get("paths") if isinstance(media_runtime.get("paths"), dict) else {}
    media_direct = media_paths.get("direct_local_http") if isinstance(media_paths.get("direct_local_http"), dict) else {}
    media_routed = media_paths.get("root_routed_http") if isinstance(media_paths.get("root_routed_http"), dict) else {}
    media_broadcast = media_paths.get("webrtc_tracks") if isinstance(media_paths.get("webrtc_tracks"), dict) else {}
    reload_override = sync_action_overrides.get("reload") if isinstance(sync_action_overrides.get("reload"), dict) else {}
    restore_override = sync_action_overrides.get("restore") if isinstance(sync_action_overrides.get("restore"), dict) else {}
    go_home_override = sync_action_overrides.get("go_home") if isinstance(sync_action_overrides.get("go_home"), dict) else {}
    set_home_current_override = (
        sync_action_overrides.get("set_home_current")
        if isinstance(sync_action_overrides.get("set_home_current"), dict)
        else {}
    )
    recovery_order = sync_recovery_playbook.get("action_order") if isinstance(sync_recovery_playbook.get("action_order"), list) else []
    recommended_action = str(sync_recovery_guidance.get("recommended_action") or "").strip() or "-"
    recommended_webspace_action = str(sync_webspace_guidance.get("recommended_action") or "").strip() or "-"
    sync_rebuild = sync_selected_webspace.get("rebuild") if isinstance(sync_selected_webspace.get("rebuild"), dict) else {}
    typer.echo(
        f"hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} | "
        f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} | "
        f"transport={strategy.get('effective_transport') or '-'} "
        f"state={strategy_assessment.get('state') or 'unknown'} "
        f"server={strategy.get('selected_server') or '-'} | "
        f"protocol={protocol_assessment.get('state') or 'unknown'} "
        f"route_backlog={route_runtime.get('pending_events') or 0} "
        f"route_reset={route_runtime.get('last_reset_reason') or '-'}:"
        f"{route_runtime.get('last_reset_ago_s') if route_runtime.get('last_reset_ago_s') is not None else '-'} "
        f"reset_total={route_runtime.get('reset_total') or 0} "
        f"route_ctrl={route_control_flow.get('state') or '-'} "
        f"route_frame={route_frame_flow.get('state') or '-'} "
        f"tg_outbox={tg_outbox.get('size') or 0} "
        f"tg_durable={'yes' if tg_outbox.get('durable_store') else 'no'} "
        f"tg_persisted={tg_outbox.get('persisted_size') or 0} "
        f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} "
        f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} "
        f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} "
        f"coverage={coverage.get('covered_flows') or 0}/{coverage.get('total_flows') or 0} "
        f"pending_acks={protocol.get('pending_ack_streams') or 0} "
        f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/{control_lifecycle_stream.get('last_issued_cursor') or 0} "
        f"control_auth={control_authority.get('state') or '-'} "
        f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} "
        f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0} | "
        f"member_cmd={member_command.get('active_path') or '-'}:{member_command.get('state') or '-'} "
        f"member_sync={member_sync.get('active_path') or '-'}:{member_sync.get('state') or '-'} "
        f"member_link_state={(hub_member_connection_state.get('assessment') if isinstance(hub_member_connection_state.get('assessment'), dict) else {}).get('state') or '-'} "
        f"member_links={hub_member_connection_state.get('member_total') or 0} "
        f"member_known={hub_member_connection_state.get('known_total') or 0} "
        f"member_linkless={hub_member_connection_state.get('linkless_total') or 0} "
        f"rollout={rollout.get('state') or '-'} "
        f"fresh={snapshot_counts.get('fresh') or 0} "
        f"pending={snapshot_counts.get('pending') or 0} "
        f"stale={snapshot_counts.get('stale') or 0} "
        f"in_progress={rollout_counts.get('in_progress') or 0} "
        f"failed={rollout_counts.get('failed') or 0} "
        f"member_names={','.join(member_labels) if member_labels else '-'} "
        f"member_runtime={','.join(member_runtime) if member_runtime else '-'} "
        f"member_update={','.join(member_update) if member_update else '-'} | "
        f"sync_runtime={sync_assessment.get('state') or '-'} "
        f"webspaces={sync_runtime.get('webspace_total') or 0} "
        f"active={sync_runtime.get('active_webspace_total') or 0} "
        f"yws={sync_transport.get('active_yws_connections') or 0} "
        f"selected={sync_selected}:{selected_sync.get('log_mode') or '-'}:{selected_sync.get('update_log_entries') or 0}/{selected_sync.get('max_update_log_entries') or 0} "
        f"snapshot={'yes' if selected_sync.get('snapshot_file_exists') else 'no'} "
        f"ws_mode={sync_selected_webspace.get('source_mode') or '-'} "
        f"ws_home={sync_selected_webspace.get('home_scenario') or '-'} "
        f"ws_proj_scenario={sync_selected_webspace.get('projection_active_scenario') or '-'} "
        f"ws_proj={'match' if sync_selected_webspace.get('projection_matches_home') is True else 'drift' if sync_selected_webspace.get('projection_matches_home') is False else 'unknown'} "
        f"ws_rebuild={sync_rebuild.get('status') or '-'} "
        f"reload={reload_override.get('source_of_truth') or 'scenario'} "
        f"restore={'yes' if restore_override.get('enabled') else 'no'}:{restore_override.get('source_of_truth') or 'snapshot'} "
        f"set_home_current={'yes' if set_home_current_override.get('enabled') else 'no'} "
        f"policy={'>'.join(str(item) for item in recovery_order) if recovery_order else '-'} "
        f"next={recommended_action} "
        f"go_home={'yes' if go_home_override.get('enabled') else 'no'} "
        f"ws_next={recommended_webspace_action} | "
        f"media_runtime={media_assessment.get('state') or '-'} "
        f"media_files={media_counts.get('file_total') or 0} "
        f"media_live={media_counts.get('live_connected_peers') or 0}/{media_counts.get('live_peer_total') or 0} "
        f"media_tracks={media_counts.get('incoming_audio_tracks') or 0}a/{media_counts.get('incoming_video_tracks') or 0}v "
        f"media_loopback={media_counts.get('loopback_audio_tracks') or 0}a/{media_counts.get('loopback_video_tracks') or 0}v "
        f"media_direct={'yes' if media_direct.get('ready') else 'no'} "
        f"media_routed={'yes' if media_routed.get('ready') else 'no'}:{media_routed.get('playback') or '-'} "
        f"media_broadcast={'yes' if media_broadcast.get('ready') else 'no'} "
        f"media_impact={media_transport.get('control_readiness_impact') or '-'} | "
        f"sidecar={sidecar.get('status') or ('disabled' if not sidecar.get('enabled') else 'unknown')}/"
        f"{sidecar.get('control_ready') or '-'} "
        f"transport={sidecar.get('local_listener_state') or '-'}/{sidecar.get('remote_session_state') or '-'} "
        f"pid={(sidecar.get('process') or {}).get('listener_pid') if isinstance(sidecar.get('process'), dict) else '-'} | "
        f"root_incident={root_diag.get('last_incident_class') or '-'} "
        f"route_incident={route_diag.get('last_incident_class') or '-'} "
        f"last={strategy.get('last_event') or '-'}"
    )


@root_link_app.command("watch")
def hub_root_watch(
    interval_s: float = typer.Option(1.0, "--interval", min=0.2),
) -> None:
    """
    Continuously poll /api/node/reliability and print hub-root link state.
    """
    import requests, time as _time

    base = resolve_control_base_url()
    token = _local_control_token(base)
    url = base + "/api/node/reliability"
    headers = {"X-AdaOS-Token": token}
    while True:
        try:
            r = requests.get(url, headers=headers, timeout=5.0)
            r.raise_for_status()
            data = r.json()
            runtime = (data or {}).get("runtime") if isinstance((data or {}).get("runtime"), dict) else {}
            overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
            diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
            strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
            protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
            hub_member = runtime.get("hub_member_channels") if isinstance(runtime.get("hub_member_channels"), dict) else {}
            sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
            sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
            strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
            protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
            coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
            root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
            route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
            root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
            route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
            route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
            outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
            control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
            tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
            llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
            route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
            route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
            route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
            streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
            control_lifecycle_stream = next(
                (
                    entry
                    for entry in streams.values()
                    if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
                ),
                {},
            )
            core_update_stream = next(
                (
                    entry
                    for entry in streams.values()
                    if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
                ),
                {},
            )
            hub_member_channels = hub_member.get("channels") if isinstance(hub_member.get("channels"), dict) else {}
            member_command = hub_member_channels.get("hub_member.command") if isinstance(hub_member_channels.get("hub_member.command"), dict) else {}
            member_sync = hub_member_channels.get("hub_member.sync") if isinstance(hub_member_channels.get("hub_member.sync"), dict) else {}
            hub_member_connection_state = runtime.get("hub_member_connection_state") if isinstance(runtime.get("hub_member_connection_state"), dict) else {}
            rollout = hub_member_connection_state.get("update_rollout") if isinstance(hub_member_connection_state.get("update_rollout"), dict) else {}
            rollout_counts = rollout.get("rollout_counts") if isinstance(rollout.get("rollout_counts"), dict) else {}
            snapshot_counts = rollout.get("snapshot_counts") if isinstance(rollout.get("snapshot_counts"), dict) else {}
            sync_assessment = sync_runtime.get("assessment") if isinstance(sync_runtime.get("assessment"), dict) else {}
            sync_transport = sync_runtime.get("transport") if isinstance(sync_runtime.get("transport"), dict) else {}
            sync_webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
            sync_selected = str(sync_runtime.get("selected_webspace_id") or "").strip() or "default"
            selected_sync = sync_webspaces.get(sync_selected) if isinstance(sync_webspaces.get(sync_selected), dict) else {}
            sync_action_overrides = sync_runtime.get("action_overrides") if isinstance(sync_runtime.get("action_overrides"), dict) else {}
            sync_recovery_playbook = sync_runtime.get("recovery_playbook") if isinstance(sync_runtime.get("recovery_playbook"), dict) else {}
            sync_recovery_guidance = sync_runtime.get("recovery_guidance") if isinstance(sync_runtime.get("recovery_guidance"), dict) else {}
            sync_selected_webspace = sync_runtime.get("selected_webspace") if isinstance(sync_runtime.get("selected_webspace"), dict) else {}
            sync_webspace_guidance = sync_runtime.get("webspace_guidance") if isinstance(sync_runtime.get("webspace_guidance"), dict) else {}
            media_runtime = runtime.get("media_runtime") if isinstance(runtime.get("media_runtime"), dict) else {}
            media_assessment = media_runtime.get("assessment") if isinstance(media_runtime.get("assessment"), dict) else {}
            media_counts = media_runtime.get("counts") if isinstance(media_runtime.get("counts"), dict) else {}
            media_paths = media_runtime.get("paths") if isinstance(media_runtime.get("paths"), dict) else {}
            media_direct = media_paths.get("direct_local_http") if isinstance(media_paths.get("direct_local_http"), dict) else {}
            media_routed = media_paths.get("root_routed_http") if isinstance(media_paths.get("root_routed_http"), dict) else {}
            reload_override = sync_action_overrides.get("reload") if isinstance(sync_action_overrides.get("reload"), dict) else {}
            restore_override = sync_action_overrides.get("restore") if isinstance(sync_action_overrides.get("restore"), dict) else {}
            go_home_override = sync_action_overrides.get("go_home") if isinstance(sync_action_overrides.get("go_home"), dict) else {}
            set_home_current_override = (
                sync_action_overrides.get("set_home_current")
                if isinstance(sync_action_overrides.get("set_home_current"), dict)
                else {}
            )
            recovery_order = sync_recovery_playbook.get("action_order") if isinstance(sync_recovery_playbook.get("action_order"), list) else []
            recommended_action = str(sync_recovery_guidance.get("recommended_action") or "").strip() or "-"
            recommended_webspace_action = str(sync_webspace_guidance.get("recommended_action") or "").strip() or "-"
            sync_rebuild = sync_selected_webspace.get("rebuild") if isinstance(sync_selected_webspace.get("rebuild"), dict) else {}
            ts = _time.strftime("%H:%M:%S")
            typer.echo(
                f"{ts} hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} "
                f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} "
                f"transport={strategy.get('effective_transport') or '-'} "
                f"protocol={protocol_assessment.get('state') or 'unknown'} "
                f"route_backlog={route_runtime.get('pending_events') or 0} "
                f"route_reset={route_runtime.get('last_reset_reason') or '-'}:"
                f"{route_runtime.get('last_reset_ago_s') if route_runtime.get('last_reset_ago_s') is not None else '-'} "
                f"reset_total={route_runtime.get('reset_total') or 0} "
                f"route_ctrl={route_control_flow.get('state') or '-'} "
                f"route_frame={route_frame_flow.get('state') or '-'} "
                f"tg_outbox={tg_outbox.get('size') or 0} "
                f"tg_durable={'yes' if tg_outbox.get('durable_store') else 'no'} "
                f"tg_persisted={tg_outbox.get('persisted_size') or 0} "
                f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} "
                f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} "
                f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} "
                f"coverage={coverage.get('covered_flows') or 0}/{coverage.get('total_flows') or 0} "
                f"pending_acks={protocol.get('pending_ack_streams') or 0} "
                f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/{control_lifecycle_stream.get('last_issued_cursor') or 0} "
                f"control_auth={control_authority.get('state') or '-'} "
                f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} "
                f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0} "
                f"member_cmd={member_command.get('active_path') or '-'}:{member_command.get('state') or '-'} "
                f"member_sync={member_sync.get('active_path') or '-'}:{member_sync.get('state') or '-'} "
                f"member_link_state={(hub_member_connection_state.get('assessment') if isinstance(hub_member_connection_state.get('assessment'), dict) else {}).get('state') or '-'} "
                f"member_links={hub_member_connection_state.get('member_total') or 0} "
                f"member_known={hub_member_connection_state.get('known_total') or 0} "
                f"member_linkless={hub_member_connection_state.get('linkless_total') or 0} "
                f"member_rollout={rollout.get('state') or '-'} "
                f"fresh={snapshot_counts.get('fresh') or 0} "
                f"pending={snapshot_counts.get('pending') or 0} "
                f"stale={snapshot_counts.get('stale') or 0} "
                f"in_progress={rollout_counts.get('in_progress') or 0} "
                f"failed={rollout_counts.get('failed') or 0} "
                f"sync_runtime={sync_assessment.get('state') or '-'} "
                f"yws={sync_transport.get('active_yws_connections') or 0} "
                f"selected={sync_selected}:{selected_sync.get('log_mode') or '-'}:{selected_sync.get('update_log_entries') or 0}/{selected_sync.get('max_update_log_entries') or 0} "
                f"snapshot={'yes' if selected_sync.get('snapshot_file_exists') else 'no'} "
                f"ws_mode={sync_selected_webspace.get('source_mode') or '-'} "
                f"ws_home={sync_selected_webspace.get('home_scenario') or '-'} "
                f"ws_proj_scenario={sync_selected_webspace.get('projection_active_scenario') or '-'} "
                f"ws_proj={'match' if sync_selected_webspace.get('projection_matches_home') is True else 'drift' if sync_selected_webspace.get('projection_matches_home') is False else 'unknown'} "
                f"ws_rebuild={sync_rebuild.get('status') or '-'} "
                f"reload={reload_override.get('source_of_truth') or 'scenario'} "
                f"restore={'yes' if restore_override.get('enabled') else 'no'}:{restore_override.get('source_of_truth') or 'snapshot'} "
                f"set_home_current={'yes' if set_home_current_override.get('enabled') else 'no'} "
                f"policy={'>'.join(str(item) for item in recovery_order) if recovery_order else '-'} "
                f"next={recommended_action} "
                f"go_home={'yes' if go_home_override.get('enabled') else 'no'} "
                f"ws_next={recommended_webspace_action} "
                f"media={media_assessment.get('state') or '-'}:{media_counts.get('file_total') or 0} "
                f"direct={'yes' if media_direct.get('ready') else 'no'} "
                f"routed={'yes' if media_routed.get('ready') else 'no'}:{media_routed.get('playback') or '-'} "
                f"sidecar={sidecar.get('status') or ('disabled' if not sidecar.get('enabled') else 'unknown')}/"
                f"{sidecar.get('control_ready') or '-'} "
                f"transport={sidecar.get('local_listener_state') or '-'}/{sidecar.get('remote_session_state') or '-'} "
                f"pid={(sidecar.get('process') or {}).get('listener_pid') if isinstance(sidecar.get('process'), dict) else '-'} "
                f"root_incident={root_diag.get('last_incident_class') or '-'} "
                f"route_incident={route_diag.get('last_incident_class') or '-'} "
                f"state={strategy_assessment.get('state') or 'unknown'} "
                f"last={strategy.get('last_event') or '-'}"
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            ts = _time.strftime("%H:%M:%S")
            typer.echo(f"{ts} error: {type(e).__name__}: {e}")
        _time.sleep(float(interval_s))


@root_link_app.command("reconnect")
def hub_root_reconnect(
    transport: str | None = typer.Option(None, "--transport", help="ws|tcp"),
    url_override: str | None = typer.Option(None, "--url-override", help="Override NATS server URL"),
    timeout_s: float = typer.Option(15.0, "--timeout", min=1.0, help="HTTP timeout (seconds)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Request hub-root reconnect and optionally update transport overrides on-the-fly.
    """
    import requests

    base = resolve_control_base_url()
    token = _local_control_token(base)
    url = base + "/api/node/hub-root/reconnect"
    headers = {"X-AdaOS-Token": token}
    payload = {"transport": transport, "url_override": url_override}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=float(timeout_s))
        try:
            body = r.json()
        except Exception:
            body = (r.text or "").strip()
        if int(r.status_code) >= 400:
            typer.secho(f"[AdaOS] hub-root reconnect failed: HTTP {r.status_code}", fg=typer.colors.RED)
            typer.echo(f"base_url: {base}")
            if body:
                typer.echo(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2))
            raise typer.Exit(code=1)
        _print(body, json_output=json_output)
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
        payload = {
            "ok": False,
            "base_url": base,
            "error": {"type": type(e).__name__, "message": str(e)},
            "hint": (
                f"Resolved control base {base}. If this is stale, check runtime state/autostart control URL "
                "or set ADAOS_CONTROL_URL explicitly. If the control API is busy, retry with --timeout 30."
            ),
        }
        if json_output:
            _print(payload, json_output=True)
        else:
            typer.echo(f"error: {payload['error']['type']}: {payload['error']['message']}")
            typer.echo(payload["hint"])
        raise typer.Exit(code=2)
    except Exception as e:
        if json_output:
            _print({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}}, json_output=True)
            raise typer.Exit(code=2)
        raise


@root_link_app.command("reports")
def hub_root_reports(
    kind: str = typer.Option("all", "--kind", help="all|control|core-update|memory-profile"),
    hub_id: str | None = typer.Option(None, "--hub-id", help="Hub/subnet id to inspect (default: current hub)"),
    session_id: str | None = typer.Option(None, "--session-id", help="Memory-profile session id filter"),
    state: str | None = typer.Option(None, "--state", help="Memory-profile session state filter"),
    suspected_only: bool = typer.Option(False, "--suspected-only", help="Only suspected memory-profile sessions"),
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Fetch explicit root-side hub reports for control, core-update, and memory-profile streams.
    """
    ctx = get_ctx()
    conf = ctx.config
    root_base = _resolve_root_base_url(conf, root)
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    target_hub_id = hub_id
    if target_hub_id is None:
        try:
            target_hub_id = str(conf.subnet_id or "").strip() or None
        except Exception:
            target_hub_id = None

    kind_key = str(kind or "all").strip().lower()
    if kind_key not in {"all", "control", "core-update", "core_update", "memory-profile", "memory_profile"}:
        raise typer.BadParameter("kind must be one of: all, control, core-update, memory-profile")

    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload: dict[str, Any] = {"ok": True, "root_url": root_base}
    if kind_key in {"all", "control"}:
        payload["control"] = client.root_control_reports(root_token=root_token, hub_id=target_hub_id)
    if kind_key in {"all", "core-update", "core_update"}:
        payload["core_update"] = client.root_core_update_reports(root_token=root_token, hub_id=target_hub_id)
    if kind_key in {"all", "memory-profile", "memory_profile"}:
        payload["memory_profile"] = client.root_memory_profile_reports(
            root_token=root_token,
            hub_id=target_hub_id,
            session_id=session_id,
            session_state=state,
            suspected_only=(True if suspected_only else None),
        )
    if json_output:
        _print(payload, json_output=True)
        return

    def _protocol(report: dict[str, Any]) -> dict[str, Any]:
        value = report.get("_protocol")
        return value if isinstance(value, dict) else {}

    control_items = payload.get("control", {}).get("items") if isinstance(payload.get("control"), dict) else None
    if isinstance(control_items, list):
        typer.echo("control reports:")
        if not control_items:
            typer.echo("  (empty)")
        for item in control_items:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proto = _protocol(report)
            lifecycle = report.get("lifecycle") if isinstance(report.get("lifecycle"), dict) else {}
            root_control = report.get("root_control") if isinstance(report.get("root_control"), dict) else {}
            route = report.get("route") if isinstance(report.get("route"), dict) else {}
            typer.echo(
                "  "
                f"{item.get('hub_id') or report.get('subnet_id') or '-'} "
                f"cursor={proto.get('cursor') or 0} "
                f"message={proto.get('message_id') or '-'} "
                f"root_received={report.get('root_received_at') or '-'} "
                f"ack={report.get('root_ack_result') or '-'} "
                f"node_state={lifecycle.get('node_state') or '-'} "
                f"root={root_control.get('status') or root_control.get('state') or '-'} "
                f"route={route.get('status') or route.get('state') or '-'}"
            )

    core_items = payload.get("core_update", {}).get("items") if isinstance(payload.get("core_update"), dict) else None
    if isinstance(core_items, list):
        typer.echo("core_update reports:")
        if not core_items:
            typer.echo("  (empty)")
        for item in core_items:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proto = _protocol(report)
            status = report.get("status") if isinstance(report.get("status"), dict) else {}
            slot = report.get("slot_status") if isinstance(report.get("slot_status"), dict) else {}
            typer.echo(
                "  "
                f"{item.get('hub_id') or report.get('subnet_id') or '-'} "
                f"cursor={proto.get('cursor') or 0} "
                f"message={proto.get('message_id') or '-'} "
                f"root_received={report.get('root_received_at') or '-'} "
                f"ack={report.get('root_ack_result') or '-'} "
                f"state={status.get('state') or '-'} "
                f"phase={status.get('phase') or '-'} "
                f"slot={slot.get('active_slot') or '-'}"
            )

    memory_items = payload.get("memory_profile", {}).get("reports") if isinstance(payload.get("memory_profile"), dict) else None
    if isinstance(memory_items, list):
        typer.echo("memory_profile reports:")
        if not memory_items:
            typer.echo("  (empty)")
        for item in memory_items:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proto = _protocol(report)
            session = report.get("session") if isinstance(report.get("session"), dict) else {}
            typer.echo(
                "  "
                f"{item.get('hub_id') or report.get('subnet_id') or '-'} "
                f"session={item.get('session_id') or session.get('session_id') or '-'} "
                f"cursor={proto.get('cursor') or 0} "
                f"message={proto.get('message_id') or '-'} "
                f"root_received={report.get('root_received_at') or '-'} "
                f"mode={session.get('profile_mode') or '-'} "
                f"state={session.get('session_state') or '-'} "
                f"suspected={bool(session.get('suspected_leak'))} "
                f"artifacts={len(session.get('artifact_refs') or [])}"
            )


@root_link_app.command("memory-session")
def hub_root_memory_session(
    session_id: str,
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Fetch one remotely published memory-profile session summary from root."""
    ctx = get_ctx()
    conf = ctx.config
    root_base = _resolve_root_base_url(conf, root)
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload = client.root_memory_profile_report(root_token=root_token, session_id=session_id)
    if json_output:
        _print(payload, json_output=True)
        return
    report_item = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    report = report_item.get("report") if isinstance(report_item.get("report"), dict) else {}
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    operations_tail = report.get("operations_tail") if isinstance(report.get("operations_tail"), list) else []
    telemetry_tail = report.get("telemetry_tail") if isinstance(report.get("telemetry_tail"), list) else []
    artifact_refs = session.get("artifact_refs") if isinstance(session.get("artifact_refs"), list) else []
    typer.echo(
        "memory profile: "
        f"hub={report_item.get('hub_id') or report.get('subnet_id') or '-'} "
        f"session={report_item.get('session_id') or session.get('session_id') or '-'} "
        f"mode={session.get('profile_mode') or '-'} "
        f"state={session.get('session_state') or '-'} "
        f"suspected={bool(session.get('suspected_leak'))}"
    )
    typer.echo(
        "memory rss: "
        f"baseline={session.get('baseline_rss_bytes') or 0} "
        f"peak={session.get('peak_rss_bytes') or 0} "
        f"growth={session.get('rss_growth_bytes') or 0}"
    )
    typer.echo(
        "memory remote: "
        f"reported={report.get('reported_at') or '-'} "
        f"received={report.get('root_received_at') or '-'} "
        f"artifacts={len(artifact_refs)} "
        f"operations={len(operations_tail)} "
        f"telemetry={len(telemetry_tail)}"
    )
    if artifact_refs:
        first = artifact_refs[0] if isinstance(artifact_refs[0], dict) else {}
        typer.echo(f"first artifact: {first.get('artifact_id') or '-'}")
    if session.get("retry_of_session_id"):
        typer.echo(
            "retry chain: "
            f"from={session.get('retry_of_session_id')} "
            f"root={session.get('retry_root_session_id') or session.get('retry_of_session_id')} "
            f"depth={session.get('retry_depth') or 0}"
        )


@root_link_app.command("memory-artifact")
def hub_root_memory_artifact(
    session_id: str,
    artifact_id: str,
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Fetch one remotely published memory-profile artifact from root."""
    ctx = get_ctx()
    conf = ctx.config
    root_base = _resolve_root_base_url(conf, root)
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload = client.root_memory_profile_artifact(
        root_token=root_token,
        session_id=session_id,
        artifact_id=artifact_id,
    )
    if json_output:
        _print(payload, json_output=True)
        return
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    typer.echo(
        "memory artifact: "
        f"session={payload.get('session_id') or session_id} "
        f"id={artifact.get('artifact_id') or artifact_id} "
        f"kind={artifact.get('kind') or '-'} "
        f"exists={bool(payload.get('exists'))}"
    )
    if artifact.get("published_ref"):
        typer.echo(f"published ref: {artifact.get('published_ref')}")
    if artifact.get("fetch_strategy"):
        typer.echo(f"fetch strategy: {artifact.get('fetch_strategy')}")
    if artifact.get("source_api_path"):
        typer.echo(f"source api path: {artifact.get('source_api_path')}")
    content = payload.get("content")
    if isinstance(content, dict):
        typer.echo(f"content keys: {', '.join(sorted(str(key) for key in content.keys())[:8])}")


@root_link_app.command("memory-artifacts")
def hub_root_memory_artifacts(
    session_id: str,
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """List remotely published memory-profile artifacts and their publish policy status."""
    ctx = get_ctx()
    conf = ctx.config
    root_base = _resolve_root_base_url(conf, root)
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload = client.root_memory_profile_artifacts(
        root_token=root_token,
        session_id=session_id,
    )
    if json_output:
        _print(payload, json_output=True)
        return
    policy = payload.get("artifact_policy") if isinstance(payload.get("artifact_policy"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    typer.echo(
        "memory artifacts: "
        f"session={payload.get('session_id') or session_id} "
        f"count={len(artifacts)} "
        f"delivery={policy.get('delivery_mode') or '-'} "
        f"limit={policy.get('max_inline_bytes') or 0}"
    )
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        typer.echo(
            "artifact: "
            f"id={item.get('artifact_id') or '-'} "
            f"kind={item.get('kind') or '-'} "
            f"status={item.get('publish_status') or '-'} "
            f"remote={bool(item.get('remote_available'))} "
            f"size={item.get('size_bytes') or 0}"
        )


@root_link_app.command("memory-artifact-pull")
def hub_root_memory_artifact_pull(
    session_id: str,
    artifact_id: str,
    max_bytes: int = typer.Option(256 * 1024, "--max-bytes", min=1, max=1024 * 1024, help="Maximum bytes to pull from current hub"),
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Fetch a published memory-profile artifact, falling back to current-hub control for local-only artifacts."""
    ctx = get_ctx()
    conf = ctx.config
    root_base = _resolve_root_base_url(conf, root)
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload = client.root_memory_profile_artifact(
        root_token=root_token,
        session_id=session_id,
        artifact_id=artifact_id,
    )
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    merged = dict(payload)
    if not isinstance(merged.get("content"), dict) and str(artifact.get("fetch_strategy") or "").strip() == "local_control_pull":
        local_payload = _local_memory_artifact_pull(
            session_id=session_id,
            artifact_id=artifact_id,
            source_api_path=str(artifact.get("source_api_path") or "").strip() or None,
            max_bytes=max_bytes,
        )
        merged["local_pull"] = {
            "attempted": True,
            "succeeded": True,
            "source": "current_hub_control",
        }
        merged["exists"] = bool(local_payload.get("exists"))
        if isinstance(local_payload.get("transfer"), dict):
            merged["transfer"] = local_payload.get("transfer")
        if isinstance(local_payload.get("content"), dict):
            merged["content"] = local_payload.get("content")
        if isinstance(local_payload.get("text"), str):
            merged["text"] = local_payload.get("text")
        if isinstance(local_payload.get("content_base64"), str):
            merged["content_base64"] = local_payload.get("content_base64")
    if json_output:
        _print(merged, json_output=True)
        return
    typer.echo(
        "memory artifact pull: "
        f"session={merged.get('session_id') or session_id} "
        f"id={artifact.get('artifact_id') or artifact_id} "
        f"kind={artifact.get('kind') or '-'} "
        f"strategy={artifact.get('fetch_strategy') or '-'} "
        f"exists={bool(merged.get('exists'))}"
    )
    transfer = merged.get("transfer") if isinstance(merged.get("transfer"), dict) else {}
    if transfer:
        typer.echo(
            "transfer: "
            f"encoding={transfer.get('encoding') or '-'} "
            f"chunk={transfer.get('chunk_bytes') or 0} "
            f"remaining={transfer.get('remaining_bytes') or 0} "
            f"truncated={bool(transfer.get('truncated'))}"
        )
    if merged.get("local_pull"):
        typer.echo("delivery: current_hub_control")
    content = merged.get("content")
    if isinstance(content, dict):
        typer.echo(f"content keys: {', '.join(sorted(str(key) for key in content.keys())[:8])}")
        return
    if isinstance(merged.get("text"), str):
        typer.echo(f"text chars: {len(merged.get('text') or '')}")
        return
    if isinstance(merged.get("content_base64"), str):
        typer.echo(f"base64 chars: {len(merged.get('content_base64') or '')}")


@sidecar_app.command("status")
def hub_root_sidecar_status(
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Print realtime sidecar runtime and process/listener status.
    """
    import requests

    base = resolve_control_base_url()
    token = _local_control_token(base)
    url = base + "/api/node/sidecar/status"
    headers = {"X-AdaOS-Token": token}
    r = requests.get(url, headers=headers, timeout=5.0)
    r.raise_for_status()
    data = r.json()
    if json_output:
        _print(data, json_output=True)
        return
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    process = data.get("process") if isinstance(data.get("process"), dict) else {}
    scope = runtime.get("scope") if isinstance(runtime.get("scope"), dict) else {}
    continuity = runtime.get("continuity_contract") if isinstance(runtime.get("continuity_contract"), dict) else {}
    progress = runtime.get("progress") if isinstance(runtime.get("progress"), dict) else {}
    route_tunnel = runtime.get("route_tunnel_contract") if isinstance(runtime.get("route_tunnel_contract"), dict) else {}
    planned_next = ",".join(str(item) for item in (scope.get("planned_next_boundaries") or []) if item)
    typer.echo(
        f"sidecar={runtime.get('status') or 'unknown'} "
        f"phase={runtime.get('phase') or '-'} "
        f"owner={runtime.get('transport_owner') or '-'} "
        f"manager={runtime.get('lifecycle_manager') or '-'} "
        f"transport={runtime.get('local_listener_state') or '-'}/{runtime.get('remote_session_state') or '-'} "
        f"control={runtime.get('control_ready') or '-'} "
        f"route={runtime.get('route_ready') or '-'} "
        f"continuity={continuity.get('current_support') or '-'}:{continuity.get('hub_runtime_update') or '-'} "
        f"next={planned_next or '-'} "
        f"listener_pid={process.get('listener_pid') or '-'} "
        f"managed_pid={process.get('managed_pid') or '-'} "
        f"adopted={'yes' if process.get('adopted_listener') else 'no'}"
    )
    if progress:
        typer.echo(
            f"progress={progress.get('completed_milestones') or 0}/{progress.get('milestone_total') or 0} "
            f"target={progress.get('target') or '-'} "
            f"state={progress.get('state') or '-'} "
            f"current={progress.get('current_milestone') or '-'}"
        )
        if progress.get("next_blocker"):
            typer.echo(f"progress_blocker={progress.get('next_blocker')}")
    if route_tunnel:
        ws_contract = route_tunnel.get("ws") if isinstance(route_tunnel.get("ws"), dict) else {}
        yws_contract = route_tunnel.get("yws") if isinstance(route_tunnel.get("yws"), dict) else {}
        typer.echo(
            f"route_tunnel={route_tunnel.get('current_support') or '-'} "
            f"ws={ws_contract.get('current_owner') or '-'}->{ws_contract.get('planned_owner') or '-'}:"
            f"{ws_contract.get('delegation_mode') or '-'} "
            f"yws={yws_contract.get('current_owner') or '-'}->{yws_contract.get('planned_owner') or '-'}:"
            f"{yws_contract.get('delegation_mode') or '-'}"
        )
        ws_blocker = next(
            (str(item).strip() for item in (ws_contract.get("blockers") or []) if str(item).strip()),
            "",
        )
        yws_blocker = next(
            (str(item).strip() for item in (yws_contract.get("blockers") or []) if str(item).strip()),
            "",
        )
        if ws_blocker:
            typer.echo(f"ws_blocker={ws_blocker}")
        if yws_blocker and yws_blocker != ws_blocker:
            typer.echo(f"yws_blocker={yws_blocker}")


@sidecar_app.command("restart")
def hub_root_sidecar_restart(
    reconnect_hub_root: bool = typer.Option(True, "--reconnect/--no-reconnect", help="Request hub-root reconnect after sidecar restart"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Restart realtime sidecar without restarting the hub process.
    """
    import requests

    base = resolve_control_base_url()
    token = _local_control_token(base)
    url = base + "/api/node/sidecar/restart"
    headers = {"X-AdaOS-Token": token}
    payload = {"reconnect_hub_root": bool(reconnect_hub_root)}
    r = requests.post(url, headers=headers, json=payload, timeout=20.0)
    r.raise_for_status()
    data = r.json()
    if json_output:
        _print(data, json_output=True)
        return
    restart = data.get("restart") if isinstance(data.get("restart"), dict) else {}
    process = data.get("process") if isinstance(data.get("process"), dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    scope = runtime.get("scope") if isinstance(runtime.get("scope"), dict) else {}
    continuity = runtime.get("continuity_contract") if isinstance(runtime.get("continuity_contract"), dict) else {}
    progress = runtime.get("progress") if isinstance(runtime.get("progress"), dict) else {}
    route_tunnel = runtime.get("route_tunnel_contract") if isinstance(runtime.get("route_tunnel_contract"), dict) else {}
    planned_next = ",".join(str(item) for item in (scope.get("planned_next_boundaries") or []) if item)
    typer.echo(
        f"accepted={bool(restart.get('accepted'))} "
        f"reason={restart.get('reason') or '-'} "
        f"sidecar={runtime.get('status') or 'unknown'}/{runtime.get('control_ready') or '-'} "
        f"owner={runtime.get('transport_owner') or '-'} "
        f"manager={runtime.get('lifecycle_manager') or '-'} "
        f"transport={runtime.get('local_listener_state') or '-'}/{runtime.get('remote_session_state') or '-'} "
        f"continuity={continuity.get('current_support') or '-'}:{continuity.get('hub_runtime_update') or '-'} "
        f"next={planned_next or '-'} "
        f"listener_pid={process.get('listener_pid') or '-'} "
        f"managed_pid={process.get('managed_pid') or '-'}"
    )
    if progress:
        typer.echo(
            f"progress={progress.get('completed_milestones') or 0}/{progress.get('milestone_total') or 0} "
            f"target={progress.get('target') or '-'} "
            f"state={progress.get('state') or '-'} "
            f"current={progress.get('current_milestone') or '-'}"
        )
    if route_tunnel:
        ws_contract = route_tunnel.get("ws") if isinstance(route_tunnel.get("ws"), dict) else {}
        yws_contract = route_tunnel.get("yws") if isinstance(route_tunnel.get("yws"), dict) else {}
        typer.echo(
            f"route_tunnel={route_tunnel.get('current_support') or '-'} "
            f"ws={ws_contract.get('current_owner') or '-'}->{ws_contract.get('planned_owner') or '-'}:"
            f"{ws_contract.get('delegation_mode') or '-'} "
            f"yws={yws_contract.get('current_owner') or '-'}->{yws_contract.get('planned_owner') or '-'}:"
            f"{yws_contract.get('delegation_mode') or '-'}"
        )


@join_code_app.command("create")
def join_code_create(
    ttl_minutes: int = typer.Option(15, "--ttl-min", min=1, max=60),
    length: int = typer.Option(8, "--length", min=8, max=12),
    root: str | None = typer.Option(None, "--root", help="Root server base URL (default: node.yaml root.base_url)"),
    local: bool = typer.Option(False, "--local", help="Create join-code locally on the hub (offline mode; member must join via hub URL)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Create a short one-time join-code for adding member nodes to this hub's subnet.

    Default mode: create a Root session so members can join via Root using the short code.
    Offline mode: use `--local` to create the join-code on the hub directly.
    """
    ctx = get_ctx()
    conf = ctx.config
    if conf.role != "hub":
        raise typer.BadParameter("join-code creation is available only on a hub node (role=hub)")

    if local:
        info = create_join_code(subnet_id=conf.subnet_id, ttl_seconds=int(ttl_minutes) * 60, length=int(length), ctx=ctx)
        out = {
            "ok": True,
            "mode": "local",
            "code": info.code,
            "subnet_id": conf.subnet_id,
            "expires_at": info.expires_at,
        }
        _print(out, json_output=json_output)
        return

    root_base = _resolve_root_base_url(conf, root)

    path = "/v1/subnets/join-code"
    url = root_base + path
    payload = {
        "subnet_id": conf.subnet_id,
        "ttl_minutes": int(ttl_minutes),
        "length": int(length),
    }

    verify: str | bool | ssl.SSLContext = True
    cert_tuple: tuple[str, str] | None = None
    try:
        ca_path = conf.ca_cert_path()
        cert_path = conf.hub_cert_path()
        key_path = conf.hub_key_path()
        if isinstance(ca_path, Path) and ca_path.exists():
            try:
                import certifi  # type: ignore

                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            try:
                ctx.load_verify_locations(cafile=str(ca_path))
            except Exception:
                pass
            verify = ctx
        if isinstance(cert_path, Path) and isinstance(key_path, Path) and cert_path.exists() and key_path.exists():
            cert_tuple = (str(cert_path), str(key_path))
    except Exception:
        verify = True
        cert_tuple = None

    data: Any | None = None
    mtls_unauth: RootHttpError | None = None

    # Preferred auth: hub mTLS (available right after bootstrap).
    if cert_tuple is not None:
        try:
            client = RootHttpClient(base_url=root_base, verify=verify, cert=cert_tuple)
            data = client.request("POST", path, json=payload, timeout=10.0)
        except RootHttpError as exc:
            # Unauthorized: fall back to owner bearer session (dev/browser workflow).
            if exc.status_code in (401, 403):
                mtls_unauth = exc
            else:
                if exc.status_code in (404, 405):
                    raise typer.BadParameter(
                        f"Root does not support join-code create endpoint yet: {path}. Deploy a newer Root build or use `--local` on the hub."
                    ) from exc
                raise typer.BadParameter(f"Root join-code create failed: HTTP {exc.status_code}; url={url}; body={exc.payload}") from exc
        except Exception as exc:  # pragma: no cover
            raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc

    # Fallback auth: owner bearer session (developer workflow).
    if data is None:
        # Do not force owner session setup for hub join-code. If hub mTLS was rejected and
        # there is no configured owner profile, surface a clear error instead of asking for root-login.
        state = getattr(conf, "root_state", None)
        profile = state.get("profile") if isinstance(state, dict) else None

        # If Root is behind a TLS terminator/reverse proxy, hub mTLS cannot reach the backend.
        # Use ROOT_TOKEN (same as `adaos dev root init`) as a non-interactive auth fallback when available.
        if mtls_unauth is not None and not profile:
            root_token = (
                os.getenv("HUB_ROOT_TOKEN")
                or os.getenv("ADAOS_ROOT_TOKEN")
                or os.getenv("ROOT_TOKEN")
                or os.getenv("ADAOS_ROOT_OWNER_TOKEN")
                or ""
            ).strip()
            if root_token:
                try:
                    client = RootHttpClient(base_url=root_base, verify=verify)
                    data = client.request(
                        "POST",
                        path,
                        json=payload,
                        headers={"X-Root-Token": root_token},
                        timeout=10.0,
                    )
                except RootHttpError as exc:
                    if exc.status_code in (401, 403):
                        raise typer.BadParameter(
                            "Root rejected ROOT_TOKEN for join-code create "
                            f"(HTTP {exc.status_code}). "
                            "This Root build likely hasn't been updated to accept X-Root-Token on "
                            f"{path} yet. Deploy the Root backend changes for rev2026, or use `--local` "
                            "on the hub for offline/LAN-only mode. "
                            f"url={url}; body={exc.payload}"
                        ) from exc
                    raise typer.BadParameter(
                        f"Root join-code create failed via ROOT_TOKEN: HTTP {exc.status_code}; url={url}; body={exc.payload}"
                    ) from exc
                except Exception as exc:  # pragma: no cover
                    raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc
            else:
                raise typer.BadParameter(
                    f"Root rejected hub mTLS join-code create (HTTP {mtls_unauth.status_code}). "
                    "This Root build likely still requires token-based auth (mTLS is not reaching the backend). "
                    f"Set ROOT_TOKEN in the environment (or .env) and re-run, or use `--local` on the hub. "
                    f"body={mtls_unauth.payload}"
                ) from mtls_unauth

        if data is None:
            try:
                auth = RootAuthService(http=RootHttpClient(base_url=root_base, verify=verify))
                access_token = auth.get_access_token(conf)
            except RootAuthError as exc:
                if cert_tuple is None:
                    raise typer.BadParameter(
                        f"Root session is not configured: {exc}. Run either: adaos dev root init (hub bootstrap) or adaos dev root login (owner session)."
                    ) from exc
                raise typer.BadParameter(
                    f"Root session is not configured: {exc}. Hub mTLS request was unauthorized; Root may require an owner session. Run: adaos dev root login"
                ) from exc

            try:
                client = RootHttpClient(base_url=root_base, verify=verify)
                data = client.request(
                    "POST",
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10.0,
                )
            except RootHttpError as exc:
                raise typer.BadParameter(f"Root join-code create failed: HTTP {exc.status_code}; url={url}; body={exc.payload}") from exc
            except Exception as exc:  # pragma: no cover
                raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict):
        raise typer.BadParameter("Root join-code create returned invalid response (expected JSON object)")
    code = str(data.get("code") or "").strip()
    expires_at_utc = data.get("expires_at_utc")
    if not code:
        raise typer.BadParameter("Root join-code create returned invalid response (missing code)")

    out = {
        "ok": True,
        "mode": "root",
        "code": code,
        "subnet_id": conf.subnet_id,
        "root_url": root_base,
        "expires_at_utc": expires_at_utc,
    }
    _print(out, json_output=json_output)
