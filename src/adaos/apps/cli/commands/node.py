from __future__ import annotations

import json
import platform
from urllib.parse import urlparse, urlunparse
from typing import Any

import requests
import typer

from adaos.services.node_config import load_config, save_config, set_role as cfg_set_role
from adaos.apps.cli.active_control import resolve_control_token

app = typer.Typer(help="Node operations (join/status/role).")
role_app = typer.Typer(help="Manage local node role.")
yjs_app = typer.Typer(help="Yjs sync diagnostics and control.")
app.add_typer(role_app, name="role")
app.add_typer(yjs_app, name="yjs")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        typer.echo(str(data))


def _control_error_message(prefix: str, payload: Any) -> str:
    if isinstance(payload, dict):
        error = str(payload.get("error") or "").strip().lower()
        detail = str(payload.get("detail") or "").strip()
        if error == "timeout":
            suffix = "local control API timed out"
            if detail:
                suffix += f" ({detail})"
            return f"[AdaOS] {prefix} failed: {suffix}"
        if error == "connection_error":
            suffix = "local control API connection failed"
            if detail:
                suffix += f" ({detail})"
            return f"[AdaOS] {prefix} failed: {suffix}"
    return f"[AdaOS] {prefix} failed: local control API is unreachable"


def _supervisor_public_base_url(control: str) -> str | None:
    raw = str(control or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port
    target_port = 8776 if port in {None, 8777, 8778} else port
    netloc = f"{parsed.hostname}:{target_port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def _supervisor_transition_probe(*, control: str, token: str) -> tuple[int | None, Any]:
    base = _supervisor_public_base_url(control)
    if not base:
        return None, {"error": "connection_error", "detail": "unable to resolve supervisor base url"}
    return _control_get_json(
        control=base,
        path="/api/supervisor/public/update-status",
        token=token,
        timeout=2.5,
    )


def _is_supervisor_controlled_transition(payload: Any) -> bool:
    data = payload if isinstance(payload, dict) else {}
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    attempt = data.get("attempt") if isinstance(data.get("attempt"), dict) else {}
    state = str(status.get("state") or "").strip().lower()
    phase = str(status.get("phase") or "").strip().lower()
    attempt_state = str(attempt.get("state") or "").strip().lower()
    if attempt_state == "awaiting_root_restart":
        return True
    if state in {"countdown", "applying", "restarting", "failed", "cancelled"}:
        return True
    if state == "validated" and phase == "root_promotion_pending":
        return True
    if state == "succeeded" and phase == "root_promoted":
        return True
    return False


def _print_supervisor_transition_summary(payload: dict[str, Any]) -> None:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    typer.echo("runtime_restarting_under_supervisor: yes")
    if status.get("state"):
        typer.echo(f"supervisor.state: {status.get('state')}")
    if status.get("phase"):
        typer.echo(f"supervisor.phase: {status.get('phase')}")
    if attempt.get("state"):
        typer.echo(f"supervisor.attempt: {attempt.get('state')}")
    if runtime.get("active_slot"):
        typer.echo(f"supervisor.active_slot: {runtime.get('active_slot')}")
    if status.get("message"):
        typer.echo(f"supervisor.message: {status.get('message')}")


def _control_get_json(*, control: str, path: str, token: str, timeout: float = 2.5) -> tuple[int | None, Any]:
    url = control.rstrip("/") + path
    headers = {"X-AdaOS-Token": token or resolve_control_token()}
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        response = sess.get(url, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        return None, {"error": "timeout", "detail": str(exc)}
    except requests.ConnectionError as exc:
        return None, {"error": "connection_error", "detail": str(exc)}
    except Exception as exc:
        return None, {"error": "request_error", "detail": str(exc)}
    try:
        payload = response.json()
    except Exception:
        payload = (response.text or "").strip()
    return response.status_code, payload


def _control_post_json(
    *,
    control: str,
    path: str,
    token: str,
    body: dict[str, Any],
    timeout: float = 2.5,
) -> tuple[int | None, Any]:
    url = control.rstrip("/") + path
    headers = {"X-AdaOS-Token": token or resolve_control_token()}
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        response = sess.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as exc:
        return None, {"error": "timeout", "detail": str(exc)}
    except requests.ConnectionError as exc:
        return None, {"error": "connection_error", "detail": str(exc)}
    except Exception as exc:
        return None, {"error": "request_error", "detail": str(exc)}
    try:
        payload = response.json()
    except Exception:
        payload = (response.text or "").strip()
    return response.status_code, payload


def _control_patch_json(
    *,
    control: str,
    path: str,
    token: str,
    body: dict[str, Any],
    timeout: float = 2.5,
) -> tuple[int | None, Any]:
    url = control.rstrip("/") + path
    headers = {"X-AdaOS-Token": token or resolve_control_token()}
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        response = sess.patch(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as exc:
        return None, {"error": "timeout", "detail": str(exc)}
    except requests.ConnectionError as exc:
        return None, {"error": "connection_error", "detail": str(exc)}
    except Exception as exc:
        return None, {"error": "request_error", "detail": str(exc)}
    try:
        payload = response.json()
    except Exception:
        payload = (response.text or "").strip()
    return response.status_code, payload


def _resolved_local_control_token(control: str, cfg: Any) -> str:
    explicit = getattr(cfg, "token", None)
    try:
        return resolve_control_token(explicit=explicit, base_url=control)
    except TypeError:
        # Test doubles may still expose the older one-argument signature.
        return resolve_control_token(explicit=explicit)


def _print_reliability_summary(payload: dict[str, Any]) -> None:
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
    matrix = runtime.get("degraded_matrix") if isinstance(runtime.get("degraded_matrix"), dict) else {}
    channel_diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    channel_overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    hub_member = runtime.get("hub_member_channels") if isinstance(runtime.get("hub_member_channels"), dict) else {}
    hub_member_connection_state = runtime.get("hub_member_connection_state") if isinstance(runtime.get("hub_member_connection_state"), dict) else {}
    sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    media_runtime = runtime.get("media_runtime") if isinstance(runtime.get("media_runtime"), dict) else {}
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    integration = tree.get("integration") if isinstance(tree.get("integration"), dict) else {}

    typer.echo(
        f"node={node.get('node_id') or '?'} role={node.get('role') or '?'} "
        f"zone={node.get('zone_id') or '-'} "
        f"ready={bool(node.get('ready'))} state={node.get('node_state') or '?'}"
    )
    hub_root_zone = runtime.get("hub_root_zone") if isinstance(runtime.get("hub_root_zone"), dict) else {}
    if hub_root_zone:
        typer.echo(
            "hub_root.zone: "
            f"configured={hub_root_zone.get('configured_zone_id') or '-'} "
            f"active={hub_root_zone.get('active_zone_id') or '-'} "
            f"server={hub_root_zone.get('selected_server') or '-'}"
        )
    if channel_overview:
        for name in ("hub_root", "hub_root_browser", "browser_hub_sync"):
            item = channel_overview.get(name) if isinstance(channel_overview.get(name), dict) else {}
            if item:
                typer.echo(
                    f"{name}: {item.get('effective_status') or 'unknown'}/"
                    f"{item.get('effective_state') or 'unknown'}"
                )
    if strategy:
        typer.echo(
            "hub_root.transport: "
            f"requested={strategy.get('requested_transport') or '-'} "
            f"effective={strategy.get('effective_transport') or '-'} "
            f"state={strategy_assessment.get('state') or 'unknown'} "
            f"server={strategy.get('selected_server') or '-'} "
            f"last={strategy.get('last_event') or '-'} "
            f"attempts={strategy.get('attempt_seq') if strategy.get('attempt_seq') is not None else '?'}"
        )
    if sidecar:
        provenance = sidecar.get("transport_provenance") if isinstance(sidecar.get("transport_provenance"), dict) else {}
        process = sidecar.get("process") if isinstance(sidecar.get("process"), dict) else {}
        scope = sidecar.get("scope") if isinstance(sidecar.get("scope"), dict) else {}
        continuity = sidecar.get("continuity_contract") if isinstance(sidecar.get("continuity_contract"), dict) else {}
        progress = sidecar.get("progress") if isinstance(sidecar.get("progress"), dict) else {}
        route_tunnel = sidecar.get("route_tunnel_contract") if isinstance(sidecar.get("route_tunnel_contract"), dict) else {}
        planned_next = ",".join(str(item) for item in (scope.get("planned_next_boundaries") or []) if item)
        typer.echo(
            "sidecar: "
            f"phase={sidecar.get('phase') or '-'} "
            f"enabled={bool(sidecar.get('enabled'))} "
            f"status={sidecar.get('status') or 'unknown'} "
            f"owner={sidecar.get('transport_owner') or '-'} "
            f"manager={sidecar.get('lifecycle_manager') or '-'} "
            f"transport={sidecar.get('local_listener_state') or ('ready' if sidecar.get('transport_ready') else 'down')}/"
            f"{sidecar.get('remote_session_state') or '-'} "
            f"control={sidecar.get('control_ready') or '-'} "
            f"route={sidecar.get('route_ready') or '-'} "
            f"connects={provenance.get('remote_connect_total') or 0}/"
            f"{provenance.get('remote_connect_fail_total') or 0} "
            f"superseded={provenance.get('superseded_total') or 0} "
            f"pid={process.get('listener_pid') or '-'} "
            f"local={sidecar.get('local_url') or '-'} "
            f"continuity={continuity.get('current_support') or '-'}:{continuity.get('hub_runtime_update') or '-'} "
            f"next={planned_next or '-'} "
            f"diag_age_s={sidecar.get('diag_age_s') if sidecar.get('diag_age_s') is not None else '-'}"
        )
        if progress:
            typer.echo(
                "sidecar.progress: "
                f"target={progress.get('target') or '-'} "
                f"state={progress.get('state') or '-'} "
                f"done={progress.get('completed_milestones') or 0}/{progress.get('milestone_total') or 0} "
                f"percent={progress.get('percent') if progress.get('percent') is not None else '-'} "
                f"current={progress.get('current_milestone') or '-'}"
            )
            if progress.get("next_blocker"):
                typer.echo(f"sidecar.progress.blocker: {progress.get('next_blocker')}")
        if route_tunnel:
            ws_contract = route_tunnel.get("ws") if isinstance(route_tunnel.get("ws"), dict) else {}
            yws_contract = route_tunnel.get("yws") if isinstance(route_tunnel.get("yws"), dict) else {}
            typer.echo(
                "sidecar.route_tunnel: "
                f"support={route_tunnel.get('current_support') or '-'} "
                f"boundary={route_tunnel.get('ownership_boundary') or '-'} "
                f"ws={ws_contract.get('current_owner') or '-'}->{ws_contract.get('planned_owner') or '-'}:"
                f"{ws_contract.get('delegation_mode') or '-'} "
                f"yws={yws_contract.get('current_owner') or '-'}->{yws_contract.get('planned_owner') or '-'}:"
                f"{yws_contract.get('delegation_mode') or '-'}"
            )
            ws_blocker = next(
                (
                    str(item).strip()
                    for item in (ws_contract.get("blockers") or [])
                    if str(item).strip()
                ),
                "",
            )
            yws_blocker = next(
                (
                    str(item).strip()
                    for item in (yws_contract.get("blockers") or [])
                    if str(item).strip()
                ),
                "",
            )
            if ws_blocker:
                typer.echo(f"sidecar.route_tunnel.ws_blocker: {ws_blocker}")
            if yws_blocker and yws_blocker != ws_blocker:
                typer.echo(f"sidecar.route_tunnel.yws_blocker: {yws_blocker}")
    if sync_runtime:
        assessment = sync_runtime.get("assessment") if isinstance(sync_runtime.get("assessment"), dict) else {}
        transport = sync_runtime.get("transport") if isinstance(sync_runtime.get("transport"), dict) else {}
        webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
        default_ws = webspaces.get("default") if isinstance(webspaces.get("default"), dict) else {}
        typer.echo(
            "sync_runtime: "
            f"state={assessment.get('state') or 'unknown'} "
            f"webspaces={sync_runtime.get('webspace_total') or 0} "
            f"active={sync_runtime.get('active_webspace_total') or 0} "
            f"compacted={sync_runtime.get('compacted_webspace_total') or 0} "
            f"updates={sync_runtime.get('update_log_total') or 0} "
            f"replay={sync_runtime.get('replay_window_total') or 0} "
            f"yws={transport.get('active_yws_connections') or 0} "
            f"owner={transport.get('owner') or '-'}->{transport.get('planned_owner') or '-'} "
            f"yws10s={transport.get('recent_open_10s') or 0} "
            f"default={default_ws.get('log_mode') or '-'}:"
            f"{default_ws.get('update_log_entries') or 0}/{default_ws.get('max_update_log_entries') or 0}"
        )
    if media_runtime:
        assessment = media_runtime.get("assessment") if isinstance(media_runtime.get("assessment"), dict) else {}
        transport = media_runtime.get("transport") if isinstance(media_runtime.get("transport"), dict) else {}
        counts = media_runtime.get("counts") if isinstance(media_runtime.get("counts"), dict) else {}
        paths = media_runtime.get("paths") if isinstance(media_runtime.get("paths"), dict) else {}
        update_guard = media_runtime.get("update_guard") if isinstance(media_runtime.get("update_guard"), dict) else {}
        direct_local = paths.get("direct_local_http") if isinstance(paths.get("direct_local_http"), dict) else {}
        root_routed = paths.get("root_routed_http") if isinstance(paths.get("root_routed_http"), dict) else {}
        webrtc_tracks = paths.get("webrtc_tracks") if isinstance(paths.get("webrtc_tracks"), dict) else {}
        typer.echo(
            "media_runtime: "
            f"state={assessment.get('state') or 'unknown'} "
            f"scope={media_runtime.get('scope') or '-'} "
            f"files={counts.get('file_total') or 0} "
            f"total={counts.get('total_bytes') or 0}B "
            f"live_peers={counts.get('live_connected_peers') or 0}/{counts.get('live_peer_total') or 0} "
            f"tracks={counts.get('incoming_audio_tracks') or 0}a/{counts.get('incoming_video_tracks') or 0}v "
            f"loopback={counts.get('loopback_audio_tracks') or 0}a/{counts.get('loopback_video_tracks') or 0}v "
            f"direct={'yes' if direct_local.get('ready') else 'no'} "
            f"routed={'yes' if root_routed.get('ready') else 'no'}:"
            f"{root_routed.get('playback') or '-'} "
            f"broadcast={'yes' if webrtc_tracks.get('ready') else 'no'} "
            f"impact={transport.get('control_readiness_impact') or '-'}"
        )
        if update_guard:
            typer.echo(
                "media.update_guard: "
                f"live={'yes' if update_guard.get('live_session_present') else 'no'} "
                f"criticality={update_guard.get('criticality') or '-'} "
                f"member={update_guard.get('member_runtime_update') or '-'} "
                f"hub={update_guard.get('hub_runtime_update') or '-'} "
                f"support={update_guard.get('current_support') or '-'} "
                f"topology={update_guard.get('observed_live_topology') or '-'}"
            )
    if protocol:
        assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
        coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
        classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
        control_cls = classes.get("control") if isinstance(classes.get("control"), dict) else {}
        route_cls = classes.get("route") if isinstance(classes.get("route"), dict) else {}
        outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
        control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
        tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
        llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
        route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
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
        typer.echo(
            "protocol: "
            f"state={assessment.get('state') or 'unknown'} "
            f"control_subs={control_cls.get('active_subscriptions') or 0} "
            f"route_subs={route_cls.get('active_subscriptions') or 0} "
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
            f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0}"
        )
    if hub_member:
        assessment = hub_member.get("assessment") if isinstance(hub_member.get("assessment"), dict) else {}
        channels = hub_member.get("channels") if isinstance(hub_member.get("channels"), dict) else {}
        command = channels.get("hub_member.command") if isinstance(channels.get("hub_member.command"), dict) else {}
        event = channels.get("hub_member.event") if isinstance(channels.get("hub_member.event"), dict) else {}
        sync = channels.get("hub_member.sync") if isinstance(channels.get("hub_member.sync"), dict) else {}
        presence = channels.get("hub_member.presence") if isinstance(channels.get("hub_member.presence"), dict) else {}
        route_channel = channels.get("hub_member.route") if isinstance(channels.get("hub_member.route"), dict) else {}
        typer.echo(
            "hub_member: "
            f"state={assessment.get('state') or 'unknown'} "
            f"cmd={command.get('active_path') or '-'}:{command.get('state') or '-'} "
            f"evt={event.get('active_path') or '-'}:{event.get('state') or '-'} "
            f"sync={sync.get('active_path') or '-'}:{sync.get('state') or '-'} "
            f"presence={presence.get('active_path') or '-'}:{presence.get('state') or '-'} "
            f"route={route_channel.get('active_path') or '-'}:{route_channel.get('state') or '-'}"
        )
    if hub_member_connection_state:
        assessment = hub_member_connection_state.get("assessment") if isinstance(hub_member_connection_state.get("assessment"), dict) else {}
        if str(hub_member_connection_state.get("role") or "") == "hub":
            members = hub_member_connection_state.get("members") if isinstance(hub_member_connection_state.get("members"), list) else []
            known_total = int(hub_member_connection_state.get("known_total") or 0)
            linkless_total = int(hub_member_connection_state.get("linkless_total") or 0)
            rollout = hub_member_connection_state.get("update_rollout") if isinstance(hub_member_connection_state.get("update_rollout"), dict) else {}
            rollout_counts = rollout.get("rollout_counts") if isinstance(rollout.get("rollout_counts"), dict) else {}
            snapshot_counts = rollout.get("snapshot_counts") if isinstance(rollout.get("snapshot_counts"), dict) else {}
            labels = [
                str(item.get("label") or item.get("node_id") or "member")
                for item in members[:4]
                if isinstance(item, dict)
            ]
            runtimes = [
                str(item.get("snapshot_runtime_git_short_commit") or item.get("snapshot_runtime_version") or "-")
                for item in members[:4]
                if isinstance(item, dict)
            ]
            updates = [
                str(item.get("snapshot_update_state") or item.get("last_hub_core_update_state") or "-")
                for item in members[:4]
                if isinstance(item, dict)
            ]
            typer.echo(
                "hub_member_links: "
                f"state={assessment.get('state') or 'unknown'} "
                f"members={hub_member_connection_state.get('member_total') or 0} "
                f"known={known_total} "
                f"linkless={linkless_total} "
                f"broadcasts={hub_member_connection_state.get('hub_core_update_broadcast_total') or 0} "
                f"rollout={rollout.get('state') or '-'} "
                f"fresh={snapshot_counts.get('fresh') or 0} "
                f"pending={snapshot_counts.get('pending') or 0} "
                f"stale={snapshot_counts.get('stale') or 0} "
                f"in_progress={rollout_counts.get('in_progress') or 0} "
                f"failed={rollout_counts.get('failed') or 0} "
                f"nodes={','.join(labels) if labels else '-'} "
                f"runtime={','.join(runtimes) if runtimes else '-'} "
                f"update={','.join(updates) if updates else '-'}"
            )
        else:
            hub = hub_member_connection_state.get("hub") if isinstance(hub_member_connection_state.get("hub"), dict) else {}
            mirrored = hub.get("last_hub_core_update") if isinstance(hub.get("last_hub_core_update"), dict) else {}
            follow = hub.get("last_follow_result") if isinstance(hub.get("last_follow_result"), dict) else {}
            typer.echo(
                "member_link: "
                f"state={hub_member_connection_state.get('state') or 'unknown'} "
                f"hub={hub.get('hub_node_id') or '-'} "
                f"hub_update={mirrored.get('state') or '-'} "
                f"follow_ok={follow.get('ok') if isinstance(follow, dict) and 'ok' in follow else '-'} "
                f"follow_err={hub.get('last_follow_error') or '-'}"
            )
    for name in ("hub_local_core", "root_control", "route", "sync", "hub_member", "member_sync", "media"):
        item = tree.get(name) if isinstance(tree.get(name), dict) else {}
        typer.echo(f"{name}: {item.get('status') or 'unknown'}")
    for name in ("telegram", "github", "llm"):
        item = integration.get(name) if isinstance(integration.get(name), dict) else {}
        typer.echo(f"integration.{name}: {item.get('status') or 'unknown'}")
    for name in ("root_control", "route"):
        item = channel_diagnostics.get(name) if isinstance(channel_diagnostics.get(name), dict) else {}
        stability = item.get("stability") if isinstance(item.get("stability"), dict) else {}
        if item:
            incident_classes = item.get("incident_classes_5m") if isinstance(item.get("incident_classes_5m"), dict) else {}
            top_incident = next(iter(incident_classes.keys()), None)
            typer.echo(
                f"diag.{name}: {stability.get('state') or 'unknown'} "
                f"score={stability.get('score') if stability.get('score') is not None else '?'} "
                f"recent_non_ready_5m={item.get('recent_non_ready_transitions_5m') or 0} "
                f"last_class={item.get('last_incident_class') or '-'} "
                f"top_5m={top_incident or '-'}"
            )
    for name in (
        "new_root_backed_member_admission",
        "root_routed_browser_proxy",
        "telegram_action_completion",
        "github_action_completion",
        "llm_action_completion",
        "core_update_coordination_via_root",
        "remote_member_snapshot_projection",
        "hub_triggered_member_update_follow",
        "member_sync_projection",
    ):
        item = matrix.get(name) if isinstance(matrix.get(name), dict) else {}
        typer.echo(f"{name}: {'allowed' if item.get('allowed') else 'blocked'}")


def _print_yjs_runtime_summary(payload: dict[str, Any]) -> None:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    transport = runtime.get("transport") if isinstance(runtime.get("transport"), dict) else {}
    selected = str(runtime.get("selected_webspace_id") or "").strip()
    action_overrides = runtime.get("action_overrides") if isinstance(runtime.get("action_overrides"), dict) else {}
    recovery_playbook = runtime.get("recovery_playbook") if isinstance(runtime.get("recovery_playbook"), dict) else {}
    recovery_guidance = runtime.get("recovery_guidance") if isinstance(runtime.get("recovery_guidance"), dict) else {}
    selected_webspace = runtime.get("selected_webspace") if isinstance(runtime.get("selected_webspace"), dict) else {}
    webspace_guidance = runtime.get("webspace_guidance") if isinstance(runtime.get("webspace_guidance"), dict) else {}
    reload_override = action_overrides.get("reload") if isinstance(action_overrides.get("reload"), dict) else {}
    restore_override = action_overrides.get("restore") if isinstance(action_overrides.get("restore"), dict) else {}
    go_home_override = action_overrides.get("go_home") if isinstance(action_overrides.get("go_home"), dict) else {}
    set_home_current_override = (
        action_overrides.get("set_home_current") if isinstance(action_overrides.get("set_home_current"), dict) else {}
    )
    recovery_order = recovery_playbook.get("action_order") if isinstance(recovery_playbook.get("action_order"), list) else []
    recommended_action = str(recovery_guidance.get("recommended_action") or "").strip() or "-"
    risk_level = str(recovery_guidance.get("risk_level") or "").strip() or "-"
    recommended_webspace_action = str(webspace_guidance.get("recommended_action") or "").strip() or "-"
    rebuild = selected_webspace.get("rebuild") if isinstance(selected_webspace.get("rebuild"), dict) else {}
    typer.echo(
        "yjs_runtime: "
        f"state={assessment.get('state') or 'unknown'} "
        f"selected={selected or '-'} "
        f"webspaces={runtime.get('webspace_total') or 0} "
        f"active={runtime.get('active_webspace_total') or 0} "
        f"compacted={runtime.get('compacted_webspace_total') or 0} "
        f"updates={runtime.get('update_log_total') or 0} "
        f"replay={runtime.get('replay_window_total') or 0} "
        f"yws={transport.get('active_yws_connections') or 0} "
        f"yws10s={transport.get('recent_open_10s') or 0} "
        f"reload={reload_override.get('source_of_truth') or 'scenario'} "
        f"restore={'yes' if restore_override.get('enabled') else 'no'}:{restore_override.get('source_of_truth') or 'snapshot'} "
        f"set_home_current={'yes' if set_home_current_override.get('enabled') else 'no'} "
        f"policy={'>'.join(str(item) for item in recovery_order) if recovery_order else '-'} "
        f"next={recommended_action} "
        f"risk={risk_level}"
    )
    if selected_webspace:
        typer.echo(
            "  webspace: "
            f"title={selected_webspace.get('title') or selected or '-'} "
            f"kind={selected_webspace.get('kind') or '-'} "
            f"mode={selected_webspace.get('source_mode') or '-'} "
            f"home={selected_webspace.get('home_scenario') or '-'} "
            f"proj_scenario={selected_webspace.get('projection_active_scenario') or '-'} "
            f"projection={'match' if selected_webspace.get('projection_matches_home') is True else 'drift' if selected_webspace.get('projection_matches_home') is False else 'unknown'} "
            f"go_home={'yes' if go_home_override.get('enabled') else 'no'} "
            f"set_home_current={'yes' if set_home_current_override.get('enabled') else 'no'} "
            f"next={recommended_webspace_action} "
            f"rebuild={rebuild.get('status') or '-'}"
        )
    operator_summary = str(recovery_guidance.get("operator_summary") or "").strip()
    if operator_summary:
        typer.echo(f"  recovery: {operator_summary}")
    warnings = recovery_guidance.get("warnings") if isinstance(recovery_guidance.get("warnings"), list) else []
    for warning in warnings:
        text = str(warning or "").strip()
        if text:
            typer.echo(f"  warn: {text}")
    ws_operator_summary = str(webspace_guidance.get("operator_summary") or "").strip()
    if ws_operator_summary:
        typer.echo(f"  webspace_guidance: {ws_operator_summary}")
    ws_warnings = webspace_guidance.get("warnings") if isinstance(webspace_guidance.get("warnings"), list) else []
    for warning in ws_warnings:
        text = str(warning or "").strip()
        if text:
            typer.echo(f"  webspace_warn: {text}")
    webspaces = runtime.get("webspaces") if isinstance(runtime.get("webspaces"), dict) else {}
    for webspace_id, item in sorted(webspaces.items()):
        if not isinstance(item, dict):
            continue
        typer.echo(
            f"- {webspace_id}: "
            f"mode={item.get('log_mode') or '-'} "
            f"log={item.get('update_log_entries') or 0}/{item.get('max_update_log_entries') or 0} "
            f"replay={item.get('replay_window_entries') or 0}/{item.get('replay_window_limit') or 0} "
            f"writes={item.get('write_total') or 0} "
            f"compacts={item.get('compact_total') or 0} "
            f"backups={item.get('backup_total') or 0} "
            f"snapshot={'yes' if item.get('snapshot_file_exists') else 'no'} "
            f"last_write_ago={item.get('last_write_ago_s') if item.get('last_write_ago_s') is not None else '-'} "
            f"last_backup_ago={item.get('last_backup_ago_s') if item.get('last_backup_ago_s') is not None else '-'}"
        )


def _print_projection_summary(payload: dict[str, Any], *, key: str = "projection") -> None:
    projection = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not projection:
        return
    typer.echo(
        "projection: "
        f"target={projection.get('target_scenario') or '-'}@{projection.get('target_space') or 'workspace'} "
        f"active={projection.get('active_scenario') or '-'}@{projection.get('active_space') or 'workspace'} "
        f"match={'yes' if projection.get('active_matches_target') else 'no'} "
        f"base_rules={projection.get('base_rule_count') if projection.get('base_rule_count') is not None else 0} "
        f"scenario_rules={projection.get('scenario_rule_count') if projection.get('scenario_rule_count') is not None else 0}"
    )


def _print_overlay_summary(payload: dict[str, Any], *, key: str = "overlay") -> None:
    overlay = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not overlay:
        return
    installed = overlay.get("installed") if isinstance(overlay.get("installed"), dict) else {}
    pinned_widgets = overlay.get("pinned_widgets") if isinstance(overlay.get("pinned_widgets"), list) else []
    topbar = overlay.get("topbar") if isinstance(overlay.get("topbar"), list) else []
    page_schema = overlay.get("page_schema") if isinstance(overlay.get("page_schema"), dict) else {}
    page_widgets = page_schema.get("widgets") if isinstance(page_schema.get("widgets"), list) else []
    typer.echo(
        "overlay: "
        f"source={overlay.get('source') or '-'} "
        f"has_overlay={'yes' if overlay.get('has_overlay') else 'no'} "
        f"installed_apps={len(installed.get('apps') or [])} "
        f"installed_widgets={len(installed.get('widgets') or [])} "
        f"pinned_widgets={len(pinned_widgets)} "
        f"topbar={len(topbar)} "
        f"page_widgets={len(page_widgets)}"
    )


def _print_desktop_summary(payload: dict[str, Any], *, key: str = "desktop") -> None:
    desktop = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not desktop:
        return
    installed = desktop.get("installed") if isinstance(desktop.get("installed"), dict) else {}
    pinned_widgets = desktop.get("pinnedWidgets") if isinstance(desktop.get("pinnedWidgets"), list) else []
    topbar = desktop.get("topbar") if isinstance(desktop.get("topbar"), list) else []
    page_schema = desktop.get("pageSchema") if isinstance(desktop.get("pageSchema"), dict) else {}
    page_widgets = page_schema.get("widgets") if isinstance(page_schema.get("widgets"), list) else []
    layout = page_schema.get("layout") if isinstance(page_schema.get("layout"), dict) else {}
    typer.echo(
        "desktop: "
        f"installed_apps={len(installed.get('apps') or [])} "
        f"installed_widgets={len(installed.get('widgets') or [])} "
        f"pinned_widgets={len(pinned_widgets)} "
        f"topbar={len(topbar)} "
        f"page_widgets={len(page_widgets)} "
        f"layout={layout.get('type') or '-'}"
    )


def _print_projection_refresh_summary(payload: dict[str, Any]) -> None:
    refresh = payload.get("projection_refresh") if isinstance(payload.get("projection_refresh"), dict) else {}
    if not refresh:
        return
    typer.echo(
        "projection_refresh: "
        f"attempted={'yes' if refresh.get('attempted') else 'no'} "
        f"scenario={refresh.get('scenario_id') or '-'}@{refresh.get('space') or 'workspace'} "
        f"rules_loaded={refresh.get('rules_loaded') if refresh.get('rules_loaded') is not None else 0} "
        f"source={refresh.get('source') or '-'}"
    )
    error = str(refresh.get("error") or "").strip()
    if error:
        typer.echo(f"  warn: {error}")


def _print_rebuild_summary(payload: dict[str, Any], *, key: str = "rebuild") -> None:
    rebuild = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not rebuild:
        return
    typer.echo(
        "rebuild: "
        f"status={rebuild.get('status') or '-'} "
        f"pending={'yes' if rebuild.get('pending') else 'no'} "
        f"background={'yes' if rebuild.get('background') else 'no'} "
        f"action={rebuild.get('action') or '-'} "
        f"scenario={rebuild.get('scenario_id') or '-'}"
    )
    error = str(rebuild.get("error") or "").strip()
    if error:
        typer.echo(f"  warn: {error}")
    _print_timings_summary(rebuild, key="timings_ms", label="rebuild_timings_ms")
    _print_timings_summary(rebuild, key="semantic_rebuild_timings_ms", label="semantic_rebuild_timings_ms")


def _print_timings_summary(payload: dict[str, Any], *, key: str = "timings_ms", label: str | None = None) -> None:
    timings = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not timings:
        return
    parts: list[str] = []
    for raw_name, raw_value in timings.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        try:
            parts.append(f"{name}={float(raw_value):.3f}")
        except Exception:
            continue
    if not parts:
        return
    typer.echo(f"{label or key}: {' '.join(parts)}")


def _normalize_rendezvous_url(*, rendezvous_url: str, root_base: str) -> str:
    """
    Root/hub join endpoints can sit behind TLS-terminating proxies and occasionally return
    `http://...` rendezvous URLs even when the public entrypoint is `https://...`.

    We persist the rendezvous into runtime node state; ensure scheme matches the public Root URL when safe.
    """
    try:
        hub_u = urlparse(str(rendezvous_url or "").strip())
        root_u = urlparse(str(root_base or "").strip())
    except Exception:
        return rendezvous_url

    # Safe upgrade for typical public deployments (no explicit ports).
    if (
        hub_u.scheme == "http"
        and root_u.scheme == "https"
        and hub_u.hostname
        and root_u.hostname
        and hub_u.hostname.lower() == root_u.hostname.lower()
        and hub_u.port is None
        and root_u.port is None
    ):
        return urlunparse(hub_u._replace(scheme="https"))

    return rendezvous_url


def _ensure_managed_key_paths(cfg) -> None:
    """
    Persist hub PKI materials under the active AdaOS base dir.

    We store canonical relative paths (`keys/...`) so a moved `.adaos`
    continues to resolve correctly after ENV_TYPE/base_dir changes.
    """
    cfg.root_settings.ca_cert = "keys/ca.cert"
    cfg.subnet_settings.hub.key = "keys/hub_private.pem"
    cfg.subnet_settings.hub.cert = "keys/hub_cert.pem"


@app.command("join")
def node_join(
    code: str = typer.Option(..., "--code", help="Short one-time join-code"),
    root: str = typer.Option(..., "--root", help="Join endpoint base URL (Hub or Root proxy)"),
    hub_url: str | None = typer.Option(None, "--hub-url", help="Optional explicit Hub URL override (offline/LAN setups)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Join a subnet as member using a short one-time join-code.

    This stores the returned subnet token + hub URL in the local runtime state under the active base_dir.
    """
    cfg = load_config()

    root_base = root.rstrip("/")
    candidates = [
        # Root-mediated join (preferred): Root issues/validates join-code and returns hub rendezvous.
        root_base + "/v1/subnets/join",
        # Legacy / offline: join directly against a hub node that holds the join-code locally.
        root_base + "/api/node/join",
    ]
    payload = {
        "code": code,
        "node_id": cfg.node_id,
        "hostname": platform.node(),
    }
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass

    def _is_missing_route(resp: requests.Response) -> bool:
        if resp.status_code not in (404, 405):
            return False
        try:
            js = resp.json()
        except Exception:
            txt = (resp.text or "").lower()
            return "cannot post" in txt or "not found" in txt
        if isinstance(js, dict):
            detail = js.get("detail")
            return detail == "Not Found"
        return False

    resp = None
    used_url = None
    last_body: Any = None
    for url in candidates:
        try:
            r = sess.post(url, json=payload, timeout=10)
        except Exception as exc:
            typer.secho(f"[AdaOS] join failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED)
            typer.echo(f"url: {url}")
            raise typer.Exit(code=2) from exc
        if r.status_code == 200:
            resp = r
            used_url = url
            break
        # Route not available on this server; try next candidate.
        if _is_missing_route(r):
            continue
        resp = r
        used_url = url
        try:
            last_body = r.json()
        except Exception:
            last_body = (r.text or "").strip()
        break

    if resp is None or used_url is None:
        typer.secho("[AdaOS] join failed: no join endpoint found on root URL", fg=typer.colors.RED)
        for url in candidates:
            typer.echo(f"url: {url}")
        typer.echo("hint: pass --root http://<HUB_HOST>:8777 for direct hub join (offline/local dev), or update Root to a build that supports /v1/subnets/join.")
        raise typer.Exit(code=1)

    if resp.status_code != 200:
        if last_body is None:
            try:
                last_body = resp.json()
            except Exception:
                last_body = (resp.text or "").strip()
        typer.secho(f"[AdaOS] join failed: HTTP {resp.status_code}", fg=typer.colors.RED)
        typer.echo(f"url: {used_url}")
        if last_body:
            typer.echo(last_body)
        if resp.status_code == 404 and isinstance(last_body, dict) and last_body.get("detail") == "join-code not found":
            typer.echo(
                "hint: join-code was not found on this Root. Common causes: "
                "the code was created on a different Root/zone "
                "(for example https://api.inimatic.com vs https://ru.api.inimatic.com), "
                "the code has already been used or expired, "
                "or it was created in hub local mode (--local)."
            )
            typer.echo("hint: for --local codes, join against the hub URL: --root http://<HUB_HOST>:8777")
        raise typer.Exit(code=1)

    data = resp.json() or {}
    token = str(data.get("token") or "").strip()
    subnet_id = str(data.get("subnet_id") or "").strip()
    rendezvous_url = str(data.get("hub_url") or root).strip()
    if hub_url:
        rendezvous_url = str(hub_url).strip()
    rendezvous_url = _normalize_rendezvous_url(rendezvous_url=rendezvous_url, root_base=root_base)
    if not token or not subnet_id or not rendezvous_url:
        typer.secho("[AdaOS] join failed: invalid response from server (missing token/subnet_id/hub_url)", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    cfg.token = token
    cfg.subnet_id = subnet_id
    cfg.hub_url = rendezvous_url
    try:
        cfg.root_settings.base_url = root.strip()
    except Exception:
        pass
    cfg.role = "member"
    _ensure_managed_key_paths(cfg)
    save_config(cfg)

    out = {
        "ok": True,
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "root_url": cfg.root_settings.base_url,
        "join_url": used_url,
    }
    _print(out, json_output=json_output)


@app.command("status")
def node_status(
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Probe local control API for readiness"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    result: dict[str, Any] = {
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "ready": None,
        "route_mode": None,
        "connected_to_hub": None,
    }
    if probe:
        status_code, payload = _control_get_json(
            control=control0,
            path="/api/node/status",
            token=_resolved_local_control_token(control0, cfg),
        )
        if status_code == 200 and isinstance(payload, dict):
            result["ready"] = bool(payload.get("ready"))
            result["route_mode"] = payload.get("route_mode")
            result["connected_to_hub"] = payload.get("connected_to_hub")
    _print(result, json_output=json_output)


@app.command("reliability")
def node_reliability(
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    token = _resolved_local_control_token(control0, cfg)
    status_code, payload = _control_get_json(
        control=control0,
        path="/api/node/reliability",
        token=token,
        timeout=5.0,
    )
    if status_code is None:
        supervisor_status, supervisor_payload = _supervisor_transition_probe(control=control0, token=token)
        if supervisor_status == 200 and isinstance(supervisor_payload, dict) and _is_supervisor_controlled_transition(supervisor_payload):
            fallback_payload = {
                "ok": True,
                "fallback": "supervisor_public_update_status",
                "controlled_transition": True,
                "message": "runtime_restarting_under_supervisor",
                "supervisor": supervisor_payload,
            }
            if json_output:
                _print(fallback_payload, json_output=True)
            else:
                _print_supervisor_transition_summary(supervisor_payload)
            raise typer.Exit(code=0)
        typer.secho(_control_error_message("reliability probe", payload), fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] reliability probe failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)

    if json_output:
        _print(payload, json_output=True)
    else:
        _print_reliability_summary(payload)


@app.command("members")
def node_members(
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_get_json(
        control=control0,
        path="/api/node/members",
        token=_resolved_local_control_token(control0, cfg),
    )
    if status_code is None:
        typer.secho("[AdaOS] member probe failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] member probe failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    state = (
        payload.get("hub_member_connection_state")
        if isinstance(payload.get("hub_member_connection_state"), dict)
        else {}
    )
    role = str(state.get("role") or "").strip().lower()
    assessment = state.get("assessment") if isinstance(state.get("assessment"), dict) else {}
    if role == "hub":
        rollout = state.get("update_rollout") if isinstance(state.get("update_rollout"), dict) else {}
        typer.echo(
            f"hub_member_links: state={assessment.get('state') or 'unknown'} "
            f"members={state.get('member_total') or 0} "
            f"known={state.get('known_total') or 0} "
            f"linkless={state.get('linkless_total') or 0} "
            f"rollout={rollout.get('state') or '-'}"
        )
        members = state.get("known_members") if isinstance(state.get("known_members"), list) else []
        if not members:
            members = state.get("members") if isinstance(state.get("members"), list) else []
        for item in members:
            if not isinstance(item, dict):
                continue
            last_control = item.get("last_control_result") if isinstance(item.get("last_control_result"), dict) else {}
            typer.echo(
                f"- {item.get('label') or item.get('node_id') or 'member'} "
                f"state={item.get('state') or '-'} "
                f"snapshot={item.get('snapshot_state') or '-'} "
                f"rollout={item.get('rollout_state') or '-'} "
                f"runtime={item.get('snapshot_runtime_git_short_commit') or item.get('snapshot_runtime_version') or '-'} "
                f"update={item.get('snapshot_update_state') or '-'} "
                f"control={item.get('last_control_action') or '-'}:{last_control.get('ok') if 'ok' in last_control else '-'} "
                f"observed_via={item.get('observed_via') or '-'} "
                f"last_snapshot_ago={item.get('last_snapshot_ago_s') if item.get('last_snapshot_ago_s') is not None else '-'} "
                f"last_seen_ago={item.get('last_seen_ago_s') if item.get('last_seen_ago_s') is not None else '-'}"
            )
        return
    hub = state.get("hub") if isinstance(state.get("hub"), dict) else {}
    follow = hub.get("last_follow_result") if isinstance(hub.get("last_follow_result"), dict) else {}
    typer.echo(
        f"member_link: state={assessment.get('state') or 'unknown'} "
        f"hub={hub.get('hub_node_id') or '-'} "
        f"hub_update={((hub.get('last_hub_core_update') if isinstance(hub.get('last_hub_core_update'), dict) else {}) or {}).get('state') or '-'} "
        f"follow_ok={follow.get('ok') if 'ok' in follow else '-'}"
    )


@yjs_app.command("status")
def node_yjs_status(
    webspace: str | None = typer.Option(None, "--webspace", help="Optional webspace id to focus on"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    token = str(webspace or "").strip()
    path = f"/api/node/yjs/webspaces/{token}/runtime" if token else "/api/node/yjs/runtime"
    status_code, payload = _control_get_json(
        control=control0,
        path=path,
        token=_resolved_local_control_token(control0, cfg),
        timeout=5.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs runtime probe failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs runtime probe failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    _print_yjs_runtime_summary(payload)


@yjs_app.command("backup")
def node_yjs_backup(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to snapshot"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}/backup",
        token=_resolved_local_control_token(control0, cfg),
        body={},
        timeout=8.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs backup failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs backup failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(
        f"yjs backup: accepted={payload.get('accepted')} webspace={payload.get('webspace_id') or webspace}"
    )
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_control_action(
    *,
    action: str,
    webspace: str,
    scenario_id: str | None,
    set_home: bool | None,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}/{action}",
        token=_resolved_local_control_token(control0, cfg),
        body={
            "scenario_id": scenario_id or None,
            **({"set_home": bool(set_home)} if set_home is not None else {}),
        },
        timeout=20.0,
    )
    if status_code is None:
        typer.secho(f"[AdaOS] yjs {action} failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs {action} failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(
        f"yjs {action}: accepted={payload.get('accepted')} "
        f"webspace={payload.get('webspace_id') or webspace} "
        f"scenario={payload.get('scenario_id') or '-'}"
    )
    _print_projection_refresh_summary(payload)
    _print_timings_summary(payload)
    _print_timings_summary(payload, key="switch_timings_ms")
    _print_timings_summary(payload, key="rebuild_timings_ms")
    _print_timings_summary(payload, key="semantic_rebuild_timings_ms")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_ensure_dev_action(
    *,
    scenario_id: str,
    requested_id: str | None,
    title: str | None,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path="/api/node/yjs/dev-webspaces/ensure",
        token=_resolved_local_control_token(control0, cfg),
        body={
            "scenario_id": str(scenario_id or "").strip() or None,
            "requested_id": str(requested_id or "").strip() or None,
            "title": str(title or "").strip() or None,
        },
        timeout=20.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs ensure-dev failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs ensure-dev failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(
        f"yjs ensure-dev: accepted={payload.get('accepted')} "
        f"created={payload.get('created')} "
        f"webspace={payload.get('webspace_id') or '-'} "
        f"scenario={payload.get('scenario_id') or scenario_id}"
    )
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_create_action(
    *,
    webspace: str | None,
    title: str | None,
    scenario_id: str | None,
    dev: bool,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path="/api/node/yjs/webspaces",
        token=_resolved_local_control_token(control0, cfg),
        body={
            "id": str(webspace or "").strip() or None,
            "title": str(title or "").strip() or None,
            "scenario_id": str(scenario_id or "").strip() or None,
            "dev": bool(dev),
        },
        timeout=20.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs create failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs create failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    webspace_payload = payload.get("webspace") if isinstance(payload.get("webspace"), dict) else {}
    typer.echo(
        f"yjs create: accepted={payload.get('accepted')} "
        f"webspace={webspace_payload.get('id') or webspace or '-'} "
        f"scenario={webspace_payload.get('home_scenario') or scenario_id or 'web_desktop'} "
        f"kind={webspace_payload.get('kind') or ('dev' if dev else 'workspace')}"
    )
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_update_action(
    *,
    webspace: str,
    title: str | None,
    home_scenario: str | None,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_patch_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}",
        token=_resolved_local_control_token(control0, cfg),
        body={
            "title": str(title or "").strip() or None,
            "home_scenario": str(home_scenario or "").strip() or None,
        },
        timeout=20.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs update failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs update failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    webspace_payload = payload.get("webspace") if isinstance(payload.get("webspace"), dict) else {}
    typer.echo(
        f"yjs update: accepted={payload.get('accepted')} "
        f"webspace={webspace_payload.get('id') or webspace} "
        f"home={webspace_payload.get('home_scenario') or home_scenario or '-'}"
    )
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_describe_action(
    *,
    webspace: str,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_get_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}",
        token=_resolved_local_control_token(control0, cfg),
        timeout=8.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs describe failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs describe failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    webspace_payload = payload.get("webspace") if isinstance(payload.get("webspace"), dict) else {}
    typer.echo(
        f"webspace: id={webspace_payload.get('webspace_id') or webspace} "
        f"kind={webspace_payload.get('kind') or '-'} "
        f"mode={webspace_payload.get('source_mode') or '-'} "
        f"home={webspace_payload.get('home_scenario') or '-'} "
        f"current={webspace_payload.get('current_scenario') or '-'}"
    )
    _print_overlay_summary(payload)
    _print_desktop_summary(payload)
    _print_projection_summary(payload)
    _print_rebuild_summary(payload)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_desktop_action(
    *,
    webspace: str,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_get_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}/desktop",
        token=_resolved_local_control_token(control0, cfg),
        timeout=8.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] yjs desktop failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs desktop failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(f"desktop: webspace={payload.get('webspace_id') or webspace}")
    _print_desktop_summary(payload)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


@yjs_app.command("create")
def node_yjs_create(
    webspace: str | None = typer.Option(None, "--webspace", help="Preferred webspace id"),
    title: str | None = typer.Option(None, "--title", help="Display title"),
    scenario_id: str | None = typer.Option(None, "--scenario-id", help="Initial home scenario"),
    dev: bool = typer.Option(False, "--dev", help="Create as dev webspace"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_create_action(
        webspace=webspace,
        title=title,
        scenario_id=scenario_id,
        dev=dev,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("update")
def node_yjs_update(
    webspace: str = typer.Option(..., "--webspace", help="Webspace id to update"),
    title: str | None = typer.Option(None, "--title", help="Updated display title"),
    home_scenario: str | None = typer.Option(None, "--home-scenario", help="Updated home scenario"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_update_action(
        webspace=webspace,
        title=title,
        home_scenario=home_scenario,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("describe")
def node_yjs_describe(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to inspect"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_describe_action(
        webspace=webspace,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("desktop")
def node_yjs_desktop(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to inspect desktop state for"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_desktop_action(
        webspace=webspace,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("reload")
def node_yjs_reload(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to reseed"),
    scenario_id: str | None = typer.Option(None, "--scenario-id", help="Explicit scenario id override"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="reload",
        webspace=webspace,
        scenario_id=scenario_id,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("reset")
def node_yjs_reset(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to hard-reset"),
    scenario_id: str | None = typer.Option(None, "--scenario-id", help="Explicit scenario id override"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="reset",
        webspace=webspace,
        scenario_id=scenario_id,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("restore")
def node_yjs_restore(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to restore from disk snapshot"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="restore",
        webspace=webspace,
        scenario_id=None,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("scenario")
def node_yjs_scenario(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to switch"),
    scenario_id: str = typer.Option(..., "--scenario-id", help="Target scenario id"),
    set_home: bool = typer.Option(False, "--set-home", help="Also persist this scenario as the webspace home"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="scenario",
        webspace=webspace,
        scenario_id=scenario_id,
        set_home=True if set_home else None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("go-home")
def node_yjs_go_home(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to return to home scenario"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="go-home",
        webspace=webspace,
        scenario_id=None,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("set-home")
def node_yjs_set_home(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to update"),
    scenario_id: str = typer.Option(..., "--scenario-id", help="Scenario id to persist as home"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="set-home",
        webspace=webspace,
        scenario_id=scenario_id,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("set-home-current")
def node_yjs_set_home_current(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to update"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_control_action(
        action="set-home-current",
        webspace=webspace,
        scenario_id=None,
        set_home=None,
        control=control,
        json_output=json_output,
    )


@yjs_app.command("ensure-dev")
def node_yjs_ensure_dev(
    scenario_id: str = typer.Option(..., "--scenario-id", help="Scenario id to open in a dev webspace"),
    webspace: str | None = typer.Option(None, "--webspace", help="Optional preferred dev webspace id"),
    title: str | None = typer.Option(None, "--title", help="Optional preferred display title"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_ensure_dev_action(
        scenario_id=scenario_id,
        requested_id=webspace,
        title=title,
        control=control,
        json_output=json_output,
    )


@app.command("member-refresh")
def node_member_refresh(
    node_id: str = typer.Option(..., "--node-id", help="Remote member node_id"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path=f"/api/node/members/{node_id}/snapshot/request",
        token=_resolved_local_control_token(control0, cfg),
        body={},
    )
    if status_code is None:
        typer.secho("[AdaOS] member refresh failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] member refresh failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(
        f"member snapshot refresh: accepted={payload.get('accepted')} "
        f"node_id={payload.get('node_id') or node_id} "
        f"reason={payload.get('reason') or '-'}"
    )


@app.command("member-update")
def node_member_update(
    node_id: str = typer.Option(..., "--node-id", help="Remote member node_id"),
    action: str = typer.Option(..., "--action", help="update|cancel|rollback"),
    target_rev: str | None = typer.Option(None, "--target-rev", help="Target rev for update"),
    target_version: str | None = typer.Option(None, "--target-version", help="Target version for update"),
    countdown_sec: float | None = typer.Option(None, "--countdown", min=0.0, help="Countdown before restart"),
    drain_timeout_sec: float | None = typer.Option(None, "--drain-timeout", min=0.0, help="Drain timeout seconds"),
    signal_delay_sec: float | None = typer.Option(None, "--signal-delay", min=0.0, help="Signal delay seconds"),
    reason: str | None = typer.Option(None, "--reason", help="Operator reason"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_post_json(
        control=control0,
        path=f"/api/node/members/{node_id}/update",
        token=_resolved_local_control_token(control0, cfg),
        body={
            "action": action,
            "target_rev": target_rev,
            "target_version": target_version,
            "countdown_sec": countdown_sec,
            "drain_timeout_sec": drain_timeout_sec,
            "signal_delay_sec": signal_delay_sec,
            "reason": reason,
        },
        timeout=8.0,
    )
    if status_code is None:
        typer.secho("[AdaOS] member update failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] member update failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(
        f"member update request: accepted={payload.get('accepted')} "
        f"node_id={payload.get('node_id') or node_id} "
        f"action={payload.get('action') or action} "
        f"request_id={payload.get('request_id') or '-'}"
    )


@role_app.command("set")
def role_set(
    role: str = typer.Option(..., "--role", help="hub|member"),
    subnet_id: str | None = typer.Option(None, "--subnet-id"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    cfg = cfg_set_role(role, hub_url=None, subnet_id=subnet_id)
    out = {"ok": True, "node_id": cfg.node_id, "subnet_id": cfg.subnet_id, "role": cfg.role, "ready": None}
    _print(out, json_output=json_output)
