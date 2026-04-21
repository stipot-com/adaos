from __future__ import annotations

import json
import platform
import time
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


def _supervisor_memory_probe(*, control: str, token: str) -> tuple[int | None, Any]:
    base = _supervisor_public_base_url(control)
    if not base:
        return None, {"error": "connection_error", "detail": "unable to resolve supervisor base url"}
    return _control_get_json(
        control=base,
        path="/api/supervisor/public/memory-status",
        token=token,
        timeout=2.5,
    )


def _is_local_control_url(control: str | None) -> bool:
    raw = str(control or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    return str(parsed.hostname or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _append_control_candidate(candidates: list[str], seen: set[str], raw: Any) -> None:
    value = str(raw or "").strip().rstrip("/")
    if not value or value in seen:
        return
    candidates.append(value)
    seen.add(value)


def _supervisor_runtime_control_candidates(control: str, token: str) -> list[str]:
    if not _is_local_control_url(control):
        return []
    try:
        parsed = urlparse(str(control or "").strip())
        port = parsed.port
    except Exception:
        parsed = None
        port = None
    if port not in {8777, 8778, 8779}:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    code, payload = _supervisor_transition_probe(control=control, token=token)
    if code == 200 and isinstance(payload, dict):
        for container in (
            payload,
            payload.get("status") if isinstance(payload.get("status"), dict) else {},
            payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {},
        ):
            if not isinstance(container, dict):
                continue
            _append_control_candidate(candidates, seen, container.get("runtime_url"))
            _append_control_candidate(candidates, seen, container.get("candidate_runtime_url"))
            slot_urls = container.get("slot_urls") if isinstance(container.get("slot_urls"), dict) else {}
            for raw_url in slot_urls.values():
                _append_control_candidate(candidates, seen, raw_url)
    scheme = parsed.scheme if parsed is not None and parsed.scheme else "http"
    for raw_url in (
        f"{scheme}://127.0.0.1:8777",
        f"{scheme}://127.0.0.1:8778",
        f"{scheme}://127.0.0.1:8779",
        f"{scheme}://localhost:8777",
        f"{scheme}://localhost:8778",
        f"{scheme}://localhost:8779",
    ):
        _append_control_candidate(candidates, seen, raw_url)
    return candidates


def _is_active_runtime_control_payload(payload: Any) -> bool:
    data = payload if isinstance(payload, dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    transition_role = str(runtime.get("transition_role") or "").strip().lower()
    if transition_role == "candidate":
        return False
    if runtime.get("admin_mutation_allowed") is False:
        return False
    return True


def _resolve_benchmark_control(
    *,
    control: str,
    token: str,
    webspace: str,
    timeout_sec: float = 5.0,
) -> tuple[str, str, int | None, Any]:
    requested = str(control or "").strip()
    candidates: list[str] = []
    seen: set[str] = set()
    _append_control_candidate(candidates, seen, requested)
    for candidate in _supervisor_runtime_control_candidates(requested, token):
        _append_control_candidate(candidates, seen, candidate)

    first_code: int | None = None
    first_payload: Any = None
    for index, candidate in enumerate(candidates):
        code, payload = _control_get_json(
            control=candidate,
            path=f"/api/node/yjs/webspaces/{webspace}",
            token=token,
            timeout=timeout_sec,
        )
        if first_payload is None:
            first_code = code
            first_payload = payload
        if code == 200 and isinstance(payload, dict) and _is_active_runtime_control_payload(payload):
            reason = "requested" if index == 0 else "supervisor_runtime_fallback"
            return candidate, reason, code, payload
    return requested, "requested", first_code, first_payload


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
    memory = payload.get("memory") if isinstance(payload.get("memory"), dict) else {}
    if memory:
        summary = (
            "supervisor.memory: "
            f"mode={memory.get('current_profile_mode') or 'normal'} "
            f"control={memory.get('profile_control_mode') or '-'} "
            f"suspicion={memory.get('suspicion_state') or 'idle'} "
            f"sessions={memory.get('sessions_total') or 0}"
        )
        if memory.get("requested_profile_mode"):
            summary += f" requested={memory.get('requested_profile_mode')}"
        if memory.get("suspicion_reason"):
            summary += f" reason={memory.get('suspicion_reason')}"
        if memory.get("rss_growth_bytes") is not None:
            summary += f" growth={memory.get('rss_growth_bytes')}"
        typer.echo(summary)
        last_session = memory.get("last_session") if isinstance(memory.get("last_session"), dict) else {}
        if last_session:
            typer.echo(
                "supervisor.memory.last_session: "
                f"id={last_session.get('session_id') or '-'} "
                f"state={last_session.get('session_state') or '-'} "
                f"mode={last_session.get('profile_mode') or '-'} "
                f"publish={last_session.get('publish_state') or '-'}"
            )


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


def _resolve_node_control_base_url(*, explicit: str | None = None) -> str:
    from adaos.apps.cli.active_control import resolve_control_base_url

    try:
        return resolve_control_base_url(explicit=explicit, prefer_local=True)
    except TypeError:
        # Test doubles may still expose the older two-argument signature.
        return resolve_control_base_url(explicit=explicit)


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
    supervisor_runtime = runtime.get("supervisor_runtime") if isinstance(runtime.get("supervisor_runtime"), dict) else {}
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
        contract = (
            sync_runtime.get("channel_contract")
            if isinstance(sync_runtime.get("channel_contract"), dict)
            else {}
        )
        transport = sync_runtime.get("transport") if isinstance(sync_runtime.get("transport"), dict) else {}
        ownership = (
            sync_runtime.get("ownership_boundaries")
            if isinstance(sync_runtime.get("ownership_boundaries"), dict)
            else {}
        )
        selector = ownership.get("selector") if isinstance(ownership.get("selector"), dict) else {}
        effective = (
            ownership.get("effective_projection")
            if isinstance(ownership.get("effective_projection"), dict)
            else {}
        )
        compatibility = (
            ownership.get("compatibility_caches")
            if isinstance(ownership.get("compatibility_caches"), dict)
            else {}
        )
        transport_session = (
            ownership.get("transport_session")
            if isinstance(ownership.get("transport_session"), dict)
            else {}
        )
        webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
        default_ws = webspaces.get("default") if isinstance(webspaces.get("default"), dict) else {}
        typer.echo(
            "sync_runtime: "
            f"state={assessment.get('state') or 'unknown'} "
            f"webspaces={sync_runtime.get('webspace_total') or 0} "
            f"active={sync_runtime.get('active_webspace_total') or 0} "
            f"compacted={sync_runtime.get('compacted_webspace_total') or 0} "
            f"eligible={sync_runtime.get('compaction_eligible_webspace_total') or 0} "
            f"updates={sync_runtime.get('update_log_total') or 0} "
            f"replay={sync_runtime.get('replay_window_total') or 0}/"
            f"{sync_runtime.get('replay_window_byte_total') or 0}B "
            f"svfast={sync_runtime.get('state_vector_fast_path_total') or 0}/"
            f"{sync_runtime.get('state_vector_compute_total') or 0} "
            f"yws={transport.get('active_yws_connections') or 0} "
            f"rtc_yjs={transport.get('webrtc_open_yjs_channels') or 0}/{transport.get('webrtc_peer_total') or 0} "
            f"rtc_pruned={transport.get('webrtc_pruned_stale_peers') or 0} "
            f"rooms={transport.get('room_total') or 0} "
            f"opens={transport.get('room_cold_open_total') or 0}/{transport.get('room_reuse_total') or 0} "
            f"single={transport.get('room_single_pass_bootstrap_total') or 0} "
            f"storm={'yes' if transport.get('storm_detected') else 'no'} "
            f"owner={transport.get('owner') or '-'}->{transport.get('planned_owner') or '-'} "
            f"yws10s={transport.get('recent_open_10s') or 0} "
            f"reloads={transport.get('reload_recent_60s') or 0}/{transport.get('reload_command_total') or 0} "
            f"dup={transport.get('reload_duplicate_total') or 0} "
            f"resets={transport.get('reset_recent_60s') or 0}/{transport.get('reset_command_total') or 0} "
            f"rdup={transport.get('reset_duplicate_total') or 0} "
            f"default={default_ws.get('log_mode') or '-'}:"
            f"{default_ws.get('update_log_entries') or 0}/{default_ws.get('max_update_log_entries') or 0}"
        )
        last_reload_client = str(transport.get("last_reload_client") or "").strip()
        if last_reload_client:
            typer.echo(
                "sync_runtime.reload_last: "
                f"client={last_reload_client} "
                f"webspace={transport.get('last_reload_webspace_id') or '-'} "
                f"age={transport.get('last_reload_age_s') if transport.get('last_reload_age_s') is not None else '-'} "
                f"dup={'yes' if transport.get('last_reload_duplicate_recent') else 'no'} "
                f"fp={transport.get('last_reload_fingerprint') or '-'}"
            )
        last_reset_client = str(transport.get("last_reset_client") or "").strip()
        if last_reset_client:
            typer.echo(
                "sync_runtime.reset_last: "
                f"client={last_reset_client} "
                f"webspace={transport.get('last_reset_webspace_id') or '-'} "
                f"age={transport.get('last_reset_age_s') if transport.get('last_reset_age_s') is not None else '-'} "
                f"dup={'yes' if transport.get('last_reset_duplicate_recent') else 'no'} "
                f"fp={transport.get('last_reset_fingerprint') or '-'}"
            )
        if contract:
            typer.echo(
                "sync_runtime.contract: "
                f"type={contract.get('channel_type') or '-'} "
                f"recovery={contract.get('recovery_model') or '-'} "
                f"replay={contract.get('replay_window') or '-'} "
                f"awareness={contract.get('awareness_semantics') or '-'} "
                f"persistence={contract.get('browser_local_persistence') or '-'} "
                f"done={'yes' if contract.get('completed_for_scope') else 'no'}"
            )
        if ownership:
            effective_state = (
                "ready"
                if effective.get("ready")
                else str(effective.get("readiness_state") or "").strip() or "pending"
            )
            typer.echo(
                "sync_runtime.boundaries: "
                f"selector={selector.get('owner') or '-'}:{selector.get('current_scenario') or selector.get('home_scenario') or '-'} "
                f"effective={effective.get('owner') or '-'}:{effective_state} "
                f"compat={compatibility.get('owner') or '-'}:{compatibility.get('mode') or '-'} "
                f"transport={transport_session.get('owner') or '-'}->{transport_session.get('planned_owner') or '-'}"
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
        if (
            int(route_runtime.get("local_base_discovery_total") or 0) > 0
            or int(route_runtime.get("local_base_runtime_port_shortcut_total") or 0) > 0
            or str(route_runtime.get("local_base_last_source") or "").strip()
        ):
            typer.echo(
                "protocol.route_local_base: "
                f"src={route_runtime.get('local_base_last_source') or '-'} "
                f"value={route_runtime.get('local_base_last_value') or '-'} "
                f"latency_ms={route_runtime.get('local_base_last_latency_ms') if route_runtime.get('local_base_last_latency_ms') is not None else '-'} "
                f"discover={route_runtime.get('local_base_discovery_total') or 0} "
                f"cache={route_runtime.get('local_base_cache_hit_total') or 0} "
                f"shortcut={route_runtime.get('local_base_runtime_port_shortcut_total') or 0} "
                f"errors={route_runtime.get('local_base_error_total') or 0}"
            )
            if str(route_runtime.get("local_base_last_error") or "").strip():
                typer.echo(
                    f"protocol.route_local_base_error: {route_runtime.get('local_base_last_error')}"
                )
        if int(route_runtime.get("open_request_total") or 0) > 0 or int(route_runtime.get("http_request_total") or 0) > 0:
            typer.echo(
                "protocol.route_requests: "
                f"open={route_runtime.get('open_request_total') or 0} "
                f"http={route_runtime.get('http_request_total') or 0} "
                f"last_open={route_runtime.get('last_open_path') or '-'} "
                f"token={'yes' if route_runtime.get('last_open_query_has_token') else 'no'} "
                f"bases={route_runtime.get('last_open_base_total') or 0} "
                f"last_http={route_runtime.get('last_http_method') or '-'}:{route_runtime.get('last_http_path') or '-'}"
            )
    phase0_comm = (
        runtime.get("event_model_phase0_communication")
        if isinstance(runtime.get("event_model_phase0_communication"), dict)
        else {}
    )
    if phase0_comm:
        remaining = ",".join(str(item) for item in (phase0_comm.get("remaining_tasks") or []) if item)
        tasks = phase0_comm.get("tasks") if isinstance(phase0_comm.get("tasks"), dict) else {}
        node_browser = (
            tasks.get("phase0.node_browser_ready")
            if isinstance(tasks.get("phase0.node_browser_ready"), dict)
            else {}
        )
        runtime_comm = (
            tasks.get("phase0.runtime_comm_ready")
            if isinstance(tasks.get("phase0.runtime_comm_ready"), dict)
            else {}
        )
        node_evidence = (
            node_browser.get("evidence")
            if isinstance(node_browser.get("evidence"), dict)
            else {}
        )
        runtime_evidence = (
            runtime_comm.get("evidence")
            if isinstance(runtime_comm.get("evidence"), dict)
            else {}
        )
        node_yws = (
            node_evidence.get("browser_yjs_ws_handoff")
            if isinstance(node_evidence.get("browser_yjs_ws_handoff"), dict)
            else {}
        )
        runtime_class_a = (
            runtime_evidence.get("hub_root_class_a")
            if isinstance(runtime_evidence.get("hub_root_class_a"), dict)
            else {}
        )
        runtime_ws = (
            runtime_evidence.get("browser_events_ws_handoff")
            if isinstance(runtime_evidence.get("browser_events_ws_handoff"), dict)
            else {}
        )
        runtime_yws = (
            runtime_evidence.get("browser_yjs_ws_handoff")
            if isinstance(runtime_evidence.get("browser_yjs_ws_handoff"), dict)
            else {}
        )
        runtime_continuity = (
            runtime_evidence.get("sidecar_continuity")
            if isinstance(runtime_evidence.get("sidecar_continuity"), dict)
            else {}
        )
        runtime_supervisor = (
            runtime_evidence.get("browser_safe_supervisor_continuity")
            if isinstance(runtime_evidence.get("browser_safe_supervisor_continuity"), dict)
            else {}
        )
        typer.echo(
            "event_model.phase0.communication: "
            f"state={phase0_comm.get('state') or '-'} "
            f"done={phase0_comm.get('completed_task_total') or 0}/{phase0_comm.get('task_total') or 0} "
            f"open={remaining or '-'}"
        )
        if node_browser:
            typer.echo(
                "event_model.phase0.node_browser_ready: "
                f"status={node_browser.get('status') or '-'} "
                f"yjs={'yes' if node_evidence.get('yjs_sync_channel_ready') else 'no'} "
                f"yws={node_yws.get('state') or '-'} "
                f"owner={node_yws.get('owner') or '-'}->{node_yws.get('planned_owner') or '-'}"
            )
            node_blocker = str(node_yws.get("blocker") or "").strip()
            if node_blocker:
                typer.echo(f"event_model.phase0.node_browser_ready.blocker: {node_blocker}")
        if runtime_comm:
            typer.echo(
                "event_model.phase0.runtime_comm_ready: "
                f"status={runtime_comm.get('status') or '-'} "
                f"class_a={runtime_class_a.get('state') or '-'}:"
                f"{runtime_class_a.get('covered_flows') or 0}/{runtime_class_a.get('total_flows') or 0} "
                f"ws={runtime_ws.get('state') or '-'} "
                f"yws={runtime_yws.get('state') or '-'} "
                f"continuity={runtime_continuity.get('state') or '-'} "
                f"supervisor={runtime_supervisor.get('state') or '-'}"
            )
            runtime_blockers = [
                str(item).strip()
                for item in (runtime_comm.get("pending_reasons") or [])
                if str(item).strip()
            ]
            if runtime_blockers:
                typer.echo(
                    f"event_model.phase0.runtime_comm_ready.blockers: {', '.join(runtime_blockers)}"
                )
    if supervisor_runtime:
        status = supervisor_runtime.get("status") if isinstance(supervisor_runtime.get("status"), dict) else {}
        runtime_state = supervisor_runtime.get("runtime") if isinstance(supervisor_runtime.get("runtime"), dict) else {}
        surface = (
            supervisor_runtime.get("browser_safe_surface")
            if isinstance(supervisor_runtime.get("browser_safe_surface"), dict)
            else {}
        )
        typer.echo(
            "supervisor_runtime: "
            f"available={bool(supervisor_runtime.get('available'))} "
            f"state={status.get('state') or '-'} "
            f"phase={status.get('phase') or '-'} "
            f"mode={runtime_state.get('transition_mode') or '-'} "
            f"candidate={runtime_state.get('candidate_runtime_state') or '-'} "
            f"warm_switch={runtime_state.get('warm_switch_reason') or '-'} "
            f"surface={surface.get('state') or '-'} "
            f"served_by={supervisor_runtime.get('_served_by') or '-'}"
        )
        surface_blockers = [
            str(item).strip()
            for item in (surface.get("blockers") or [])
            if str(item).strip()
        ]
        if surface_blockers:
            typer.echo(f"supervisor_runtime.surface_blockers: {', '.join(surface_blockers)}")
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
    contract = runtime.get("channel_contract") if isinstance(runtime.get("channel_contract"), dict) else {}
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
    if contract:
        typer.echo(
            "  contract: "
            f"type={contract.get('channel_type') or '-'} "
            f"recovery={contract.get('recovery_model') or '-'} "
            f"replay={contract.get('replay_window') or '-'} "
            f"awareness={contract.get('awareness_semantics') or '-'} "
            f"persistence={contract.get('browser_local_persistence') or '-'} "
            f"done={'yes' if contract.get('completed_for_scope') else 'no'}"
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
            f"rebuild={rebuild.get('status') or '-'}:{rebuild.get('action') or '-'} "
            f"rdup={rebuild.get('recovery_duplicate_total') or 0} "
            f"reason={rebuild.get('recovery_last_duplicate_reason') or '-'} "
            f"fp={rebuild.get('recovery_fingerprint') or '-'}"
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
            f"svfast={item.get('state_vector_fast_path_total') or 0}/{item.get('state_vector_compute_total') or 0} "
            f"apply={item.get('last_apply_mode') or '-'} "
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


def _print_materialization_summary(payload: dict[str, Any], *, key: str = "materialization") -> None:
    materialization = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not materialization:
        return
    missing_branches = [
        str(raw_branch or "").strip()
        for raw_branch in list(materialization.get("missing_branches") or [])
        if str(raw_branch or "").strip()
    ]
    catalog_counts = materialization.get("catalog_counts") if isinstance(materialization.get("catalog_counts"), dict) else {}
    typer.echo(
        "materialization: "
        f"ready={'yes' if materialization.get('ready') else 'no'} "
        f"state={materialization.get('readiness_state') or '-'} "
        f"apps={int(catalog_counts.get('apps') or 0)} "
        f"widgets={int(catalog_counts.get('widgets') or 0)} "
        f"topbar={int(materialization.get('topbar_count') or 0)} "
        f"page_widgets={int(materialization.get('page_widget_count') or 0)} "
        f"missing={len(missing_branches)} "
        f"source={materialization.get('snapshot_source') or '-'} "
        f"stale={'yes' if materialization.get('stale') else 'no'}"
    )
    if missing_branches:
        typer.echo(f"  missing: {','.join(missing_branches)}")
    compatibility = materialization.get("compatibility_caches") if isinstance(materialization.get("compatibility_caches"), dict) else {}
    if compatibility:
        blockers = [
            str(raw_blocker or "").strip()
            for raw_blocker in list(compatibility.get("runtime_removal_blockers") or [])
            if str(raw_blocker or "").strip()
        ]
        typer.echo(
            "compatibility: "
            f"client_fallback={'yes' if compatibility.get('client_fallback_readable') else 'no'} "
            f"present={int(compatibility.get('present_count') or 0)}/{int(compatibility.get('required_count') or 0)} "
            f"complete={'yes' if compatibility.get('complete') else 'no'} "
            f"writes={'on' if compatibility.get('switch_writes_enabled') else 'off'} "
            f"legacy_fallback={'yes' if compatibility.get('legacy_fallback_active') else 'no'} "
            f"runtime_removal_ready={'yes' if compatibility.get('runtime_removal_ready') else 'no'}"
        )
        if blockers:
            typer.echo(f"  blockers: {','.join(blockers)}")


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
    resolver = rebuild.get("resolver") if isinstance(rebuild.get("resolver"), dict) else {}
    if resolver:
        typer.echo(
            "  resolver: "
            f"source={resolver.get('source') or '-'} "
            f"legacy_fallback={'yes' if resolver.get('legacy_fallback') else 'no'} "
            f"cache_hit={'yes' if resolver.get('cache_hit') else 'no'}"
        )
    _print_apply_summary(rebuild, indent="  ")
    _print_timings_summary(rebuild, key="timings_ms", label="rebuild_timings_ms")
    _print_timings_summary(rebuild, key="switch_timings_ms", label="switch_timings_ms")
    _print_timings_summary(rebuild, key="semantic_rebuild_timings_ms", label="semantic_rebuild_timings_ms")
    _print_timings_summary(rebuild, key="ydoc_timings_ms", label="ydoc_timings_ms")
    _print_timings_summary(rebuild, key="phase_timings_ms", label="phase_timings_ms")


def _print_apply_summary(payload: dict[str, Any], *, key: str = "apply_summary", indent: str = "") -> None:
    apply = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not apply:
        return
    try:
        branch_count = int(apply.get("branch_count") or 0)
    except Exception:
        branch_count = 0
    try:
        changed = int(apply.get("changed_branches") or 0)
    except Exception:
        changed = 0
    try:
        unchanged = int(apply.get("unchanged_branches") or 0)
    except Exception:
        unchanged = 0
    try:
        failed = int(apply.get("failed_branches") or 0)
    except Exception:
        failed = 0
    try:
        fingerprint_unchanged = int(apply.get("fingerprint_unchanged_branches") or 0)
    except Exception:
        fingerprint_unchanged = 0
    try:
        diff_applied = int(apply.get("diff_applied_branches") or 0)
    except Exception:
        diff_applied = 0
    try:
        replaced = int(apply.get("replaced_branches") or 0)
    except Exception:
        replaced = 0
    changed_paths = [
        str(raw_path or "").strip()
        for raw_path in list(apply.get("changed_paths") or [])
        if str(raw_path or "").strip()
    ]
    summary = (
        f"{indent}apply: changed={changed}/{branch_count or max(changed + unchanged + failed, 1)} "
        f"unchanged={unchanged} failed={failed}"
    )
    if fingerprint_unchanged:
        summary += f" fingerprint_skip={fingerprint_unchanged}"
    if diff_applied or replaced:
        summary += f" diff={diff_applied} replace={replaced}"
    if changed_paths:
        summary += f" paths={','.join(changed_paths)}"
    typer.echo(summary)
    phases = apply.get("phases") if isinstance(apply.get("phases"), dict) else {}
    for phase_name in ("structure", "interactive"):
        phase = phases.get(phase_name) if isinstance(phases.get(phase_name), dict) else {}
        if not phase:
            continue
        try:
            phase_branch_count = int(phase.get("branch_count") or 0)
        except Exception:
            phase_branch_count = 0
        try:
            phase_changed = int(phase.get("changed_branches") or 0)
        except Exception:
            phase_changed = 0
        try:
            phase_unchanged = int(phase.get("unchanged_branches") or 0)
        except Exception:
            phase_unchanged = 0
        try:
            phase_failed = int(phase.get("failed_branches") or 0)
        except Exception:
            phase_failed = 0
        try:
            phase_fingerprint_unchanged = int(phase.get("fingerprint_unchanged_branches") or 0)
        except Exception:
            phase_fingerprint_unchanged = 0
        try:
            phase_diff_applied = int(phase.get("diff_applied_branches") or 0)
        except Exception:
            phase_diff_applied = 0
        try:
            phase_replaced = int(phase.get("replaced_branches") or 0)
        except Exception:
            phase_replaced = 0
        phase_changed_paths = [
            str(raw_path or "").strip()
            for raw_path in list(phase.get("changed_paths") or [])
            if str(raw_path or "").strip()
        ]
        phase_summary = (
            f"{indent}  apply.phase.{phase_name}: changed={phase_changed}/"
            f"{phase_branch_count or max(phase_changed + phase_unchanged + phase_failed, 1)} "
            f"unchanged={phase_unchanged} failed={phase_failed}"
        )
        if phase_fingerprint_unchanged:
            phase_summary += f" fingerprint_skip={phase_fingerprint_unchanged}"
        if phase_diff_applied or phase_replaced:
            phase_summary += f" diff={phase_diff_applied} replace={phase_replaced}"
        if phase_changed_paths:
            phase_summary += f" paths={','.join(phase_changed_paths)}"
        typer.echo(phase_summary)


def _timing_value(payload: dict[str, Any], *, key: str, name: str) -> float | None:
    timing_map = payload.get(key) if isinstance(payload.get(key), dict) else {}
    if not timing_map:
        return None
    try:
        raw = timing_map.get(name)
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        return round(float(raw), 3)
    except Exception:
        return None


def _aggregate_benchmark_values(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "avg": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _aggregate_benchmark_timing_maps(
    runs: list[dict[str, Any]],
    *,
    key: str,
    names: tuple[str, ...] | None = None,
) -> dict[str, dict[str, float]]:
    values_by_name: dict[str, list[float]] = {}
    allowed = set(names or ())
    for run in runs:
        timing_map = run.get(key) if isinstance(run.get(key), dict) else {}
        if not timing_map:
            continue
        for raw_name, raw_value in timing_map.items():
            name = str(raw_name or "").strip()
            if not name:
                continue
            if allowed and name not in allowed:
                continue
            try:
                value = round(float(raw_value), 3)
            except Exception:
                continue
            values_by_name.setdefault(name, []).append(value)
    out: dict[str, dict[str, float]] = {}
    for name in sorted(values_by_name):
        aggregate = _aggregate_benchmark_values(values_by_name[name])
        if aggregate:
            out[name] = aggregate
    return out


def _benchmark_poll_counts() -> dict[str, int]:
    return {
        "rebuild": 0,
        "rebuild_describe_fallback": 0,
        "rebuild_transient_failures": 0,
        "materialization": 0,
        "materialization_describe_fallback": 0,
        "materialization_transient_failures": 0,
    }


def _benchmark_note_poll(counts: dict[str, int] | None, key: str) -> None:
    if not isinstance(counts, dict):
        return
    name = str(key or "").strip()
    if not name:
        return
    counts[name] = int(counts.get(name) or 0) + 1


def _is_transient_benchmark_poll_failure(code: int | None, payload: Any) -> bool:
    if code is None:
        if not isinstance(payload, dict):
            return True
        error = str(payload.get("error") or "").strip().lower()
        return error in {"timeout", "connection_error", "request_error"} or not error
    try:
        return int(code) in {502, 503, 504}
    except Exception:
        return False


def _benchmark_materialization_milestones(materialization: dict[str, Any] | None, *, elapsed_ms: float) -> dict[str, float]:
    state = materialization if isinstance(materialization, dict) else {}
    ready = bool(state.get("ready"))
    readiness_state = str(state.get("readiness_state") or "").strip().lower()
    milestones: dict[str, float] = {}
    if ready or readiness_state in {"first_paint", "interactive", "hydrating", "ready"}:
        milestones["time_to_first_paint"] = round(float(elapsed_ms), 3)
    if ready or readiness_state in {"interactive", "hydrating", "ready"}:
        milestones["time_to_interactive"] = round(float(elapsed_ms), 3)
    if ready or readiness_state == "ready":
        milestones["time_to_ready"] = round(float(elapsed_ms), 3)
    return milestones


def _benchmark_ready_alignment(
    payload: dict[str, Any],
    *,
    request_started_at: float | None = None,
) -> tuple[dict[str, float] | None, str | None]:
    rebuild = payload.get("rebuild") if isinstance(payload.get("rebuild"), dict) else {}
    materialization = payload.get("materialization") if isinstance(payload.get("materialization"), dict) else {}
    phase_timings = payload.get("phase_timings_ms") if isinstance(payload.get("phase_timings_ms"), dict) else {}
    observed_timings = payload.get("observed_timings_ms") if isinstance(payload.get("observed_timings_ms"), dict) else {}

    server_ready_ms: float | None = None
    source: str | None = None

    raw_phase_ready = phase_timings.get("time_to_full_hydration")
    if raw_phase_ready is not None:
        try:
            server_ready_ms = round(max(float(raw_phase_ready), 0.0), 3)
            source = "phase_timings"
        except Exception:
            server_ready_ms = None

    if server_ready_ms is None and request_started_at is not None:
        raw_finished_at = rebuild.get("finished_at")
        if raw_finished_at is not None:
            try:
                server_ready_ms = round(max((float(raw_finished_at) - float(request_started_at)) * 1000.0, 0.0), 3)
                source = "rebuild_finished_at"
            except Exception:
                server_ready_ms = None

    if server_ready_ms is None and request_started_at is not None and bool(materialization.get("ready")):
        raw_observed_at = materialization.get("observed_at")
        if raw_observed_at is not None:
            try:
                server_ready_ms = round(max((float(raw_observed_at) - float(request_started_at)) * 1000.0, 0.0), 3)
                source = "materialization_observed_at"
            except Exception:
                server_ready_ms = None

    if server_ready_ms is None:
        return None, None

    metrics: dict[str, float] = {"server_ready": server_ready_ms}
    raw_observed_ready = observed_timings.get("time_to_ready")
    if raw_observed_ready is not None:
        try:
            observed_ready_ms = float(raw_observed_ready)
            metrics["observation_lag"] = round(max(observed_ready_ms - server_ready_ms, 0.0), 3)
        except Exception:
            pass
    return metrics, source


def _best_effort_benchmark_materialization_poll(
    *,
    control: str,
    token: str,
    webspace: str,
    timeout_sec: float,
    poll_counts: dict[str, int] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    for path in (
        f"/api/node/yjs/webspaces/{webspace}/materialization?include_runtime=0",
        f"/api/node/yjs/webspaces/{webspace}",
    ):
        if "/materialization?" in path:
            _benchmark_note_poll(poll_counts, "materialization")
        else:
            _benchmark_note_poll(poll_counts, "materialization_describe_fallback")
        code, payload = _control_get_json(
            control=control,
            path=path,
            token=token,
            timeout=timeout_sec,
        )
        if code == 404 and "/materialization?" in path:
            continue
        if _is_transient_benchmark_poll_failure(code, payload):
            _benchmark_note_poll(poll_counts, "materialization_transient_failures")
            return None, True
        if code != 200 or not isinstance(payload, dict):
            return None, False
        materialization = payload.get("materialization") if isinstance(payload.get("materialization"), dict) else {}
        if materialization:
            return dict(materialization), True
        return None, True
    return None, False


def _merge_benchmark_rebuild_payload(payload: dict[str, Any], rebuild: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(payload)
    rebuild_state = dict(rebuild or {})
    if not rebuild_state:
        return merged
    merged["rebuild"] = rebuild_state

    resolver = rebuild_state.get("resolver") if isinstance(rebuild_state.get("resolver"), dict) else {}
    if resolver:
        merged["resolver"] = dict(resolver)

    apply_summary = rebuild_state.get("apply_summary") if isinstance(rebuild_state.get("apply_summary"), dict) else {}
    if apply_summary:
        merged["apply_summary"] = dict(apply_summary)

    phase_timings = dict(merged.get("phase_timings_ms") or {}) if isinstance(merged.get("phase_timings_ms"), dict) else {}
    rebuild_phase_timings = rebuild_state.get("phase_timings_ms") if isinstance(rebuild_state.get("phase_timings_ms"), dict) else {}
    if rebuild_phase_timings:
        phase_timings.update(dict(rebuild_phase_timings))
    if phase_timings:
        merged["phase_timings_ms"] = phase_timings

    switch_timings = dict(merged.get("timings_ms") or {}) if isinstance(merged.get("timings_ms"), dict) else {}
    rebuild_switch_timings = rebuild_state.get("switch_timings_ms") if isinstance(rebuild_state.get("switch_timings_ms"), dict) else {}
    if rebuild_switch_timings:
        switch_timings.update(dict(rebuild_switch_timings))
    if switch_timings:
        merged["timings_ms"] = switch_timings

    rebuild_timings = rebuild_state.get("timings_ms") if isinstance(rebuild_state.get("timings_ms"), dict) else {}
    if rebuild_timings:
        merged["rebuild_timings_ms"] = dict(rebuild_timings)

    semantic_timings = (
        rebuild_state.get("semantic_rebuild_timings_ms")
        if isinstance(rebuild_state.get("semantic_rebuild_timings_ms"), dict)
        else {}
    )
    if semantic_timings:
        merged["semantic_rebuild_timings_ms"] = dict(semantic_timings)
    ydoc_timings = rebuild_state.get("ydoc_timings_ms") if isinstance(rebuild_state.get("ydoc_timings_ms"), dict) else {}
    if ydoc_timings:
        merged["ydoc_timings_ms"] = dict(ydoc_timings)
    return merged


def _benchmark_rebuild_is_terminal(
    rebuild: dict[str, Any] | None,
    *,
    request_id: str | None,
    scenario_id: str | None,
) -> bool:
    state = rebuild if isinstance(rebuild, dict) else {}
    if not state:
        return False
    pending = bool(state.get("pending"))
    status = str(state.get("status") or "").strip().lower()
    state_request_id = str(state.get("request_id") or "").strip() or None
    state_scenario_id = str(state.get("scenario_id") or "").strip() or None
    if request_id and state_request_id and state_request_id != request_id:
        return False
    if not request_id and scenario_id and state_scenario_id and state_scenario_id != scenario_id:
        return False
    if pending:
        return False
    return status in {"ready", "failed", "cancelled"}


def _wait_for_benchmark_rebuild(
    *,
    control: str,
    token: str,
    webspace: str,
    scenario_id: str,
    initial_rebuild: dict[str, Any] | None,
    timeout_sec: float,
    poll_interval_sec: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, float], float | None, bool, dict[str, int]]:
    rebuild_state = dict(initial_rebuild or {})
    request_id = str(rebuild_state.get("request_id") or "").strip() or None
    observed_timings: dict[str, float] = {}
    materialization_state = (
        dict(rebuild_state.get("materialization") or {})
        if isinstance(rebuild_state.get("materialization"), dict)
        else None
    )
    materialization_supported = True
    poll_counts = _benchmark_poll_counts()
    materialization_complete = bool(materialization_state and materialization_state.get("ready"))
    if _benchmark_rebuild_is_terminal(rebuild_state, request_id=request_id, scenario_id=scenario_id):
        if materialization_supported and not materialization_complete:
            current_materialization, materialization_supported = _best_effort_benchmark_materialization_poll(
                control=control,
                token=token,
                webspace=webspace,
                timeout_sec=max(2.0, max(float(poll_interval_sec or 0.0), 0.05) + 1.0),
                poll_counts=poll_counts,
            )
            if current_materialization:
                materialization_state = current_materialization
                materialization_complete = bool(materialization_state.get("ready"))
                observed_timings.update(_benchmark_materialization_milestones(materialization_state, elapsed_ms=0.0))
        return rebuild_state, materialization_state, observed_timings, 0.0, False, poll_counts

    wait_started = time.perf_counter()
    deadline = wait_started + max(float(timeout_sec or 0.0), 0.0)
    interval = max(float(poll_interval_sec or 0.0), 0.05)

    while True:
        payload = None
        code = None
        current_rebuild: dict[str, Any] = {}
        transient_rebuild_failure = False
        for path in (
            f"/api/node/yjs/webspaces/{webspace}/rebuild?include_runtime=0",
            f"/api/node/yjs/webspaces/{webspace}",
        ):
            if "/rebuild?" in path:
                _benchmark_note_poll(poll_counts, "rebuild")
            else:
                _benchmark_note_poll(poll_counts, "rebuild_describe_fallback")
            code, payload = _control_get_json(
                control=control,
                path=path,
                token=token,
                timeout=max(5.0, interval + 1.0),
            )
            if code == 404 and "/rebuild?" in path:
                continue
            if _is_transient_benchmark_poll_failure(code, payload):
                _benchmark_note_poll(poll_counts, "rebuild_transient_failures")
                transient_rebuild_failure = True
                break
            if code != 200 or not isinstance(payload, dict):
                typer.secho(f"[AdaOS] yjs benchmark-scenario failed: HTTP {code}", fg=typer.colors.RED)
                if payload:
                    typer.echo(payload)
                raise typer.Exit(code=1)
            current_rebuild = payload.get("rebuild") if isinstance(payload.get("rebuild"), dict) else {}
            break
        loop_now = time.perf_counter()
        elapsed_ms = round((loop_now - wait_started) * 1000.0, 3)
        if transient_rebuild_failure:
            if loop_now >= deadline:
                return rebuild_state if rebuild_state else None, materialization_state, observed_timings, elapsed_ms, True, poll_counts
            time.sleep(interval)
            continue
        if current_rebuild:
            rebuild_state = dict(current_rebuild)
            current_request_id = str(rebuild_state.get("request_id") or "").strip() or None
            if current_request_id:
                request_id = current_request_id
            embedded_materialization = (
                dict(rebuild_state.get("materialization") or {})
                if isinstance(rebuild_state.get("materialization"), dict)
                else None
            )
            if embedded_materialization:
                materialization_state = embedded_materialization
                materialization_complete = bool(materialization_state.get("ready"))
        if materialization_supported and not materialization_complete:
            current_materialization, materialization_supported = _best_effort_benchmark_materialization_poll(
                control=control,
                token=token,
                webspace=webspace,
                timeout_sec=max(2.0, interval + 1.0),
                poll_counts=poll_counts,
            )
            if current_materialization:
                materialization_state = current_materialization
                materialization_complete = bool(materialization_state.get("ready"))
                for name, value in _benchmark_materialization_milestones(materialization_state, elapsed_ms=elapsed_ms).items():
                    observed_timings.setdefault(name, value)
        if _benchmark_rebuild_is_terminal(rebuild_state, request_id=request_id, scenario_id=scenario_id):
            return rebuild_state, materialization_state, observed_timings, elapsed_ms, False, poll_counts
        if loop_now >= deadline:
            return rebuild_state if rebuild_state else None, materialization_state, observed_timings, elapsed_ms, True, poll_counts
        time.sleep(interval)


def _extract_benchmark_run(payload: dict[str, Any]) -> dict[str, Any]:
    rebuild = payload.get("rebuild") if isinstance(payload.get("rebuild"), dict) else {}
    materialization = payload.get("materialization") if isinstance(payload.get("materialization"), dict) else {}
    phase_timings = dict(payload.get("phase_timings_ms") or {}) if isinstance(payload.get("phase_timings_ms"), dict) else {}
    rebuild_phase_timings = rebuild.get("phase_timings_ms") if isinstance(rebuild.get("phase_timings_ms"), dict) else {}
    if rebuild_phase_timings:
        phase_timings.update(dict(rebuild_phase_timings))
    resolver = payload.get("resolver") if isinstance(payload.get("resolver"), dict) else {}
    if not resolver and isinstance(rebuild.get("resolver"), dict):
        resolver = rebuild.get("resolver")
    apply_summary = payload.get("apply_summary") if isinstance(payload.get("apply_summary"), dict) else {}
    if not apply_summary and isinstance(rebuild.get("apply_summary"), dict):
        apply_summary = rebuild.get("apply_summary")
    switch_timings = dict(payload.get("timings_ms") or {}) if isinstance(payload.get("timings_ms"), dict) else {}
    rebuild_switch_timings = rebuild.get("switch_timings_ms") if isinstance(rebuild.get("switch_timings_ms"), dict) else {}
    if rebuild_switch_timings:
        switch_timings.update(dict(rebuild_switch_timings))
    try:
        changed_branches = int(apply_summary.get("changed_branches") or 0)
    except Exception:
        changed_branches = 0
    try:
        unchanged_branches = int(apply_summary.get("unchanged_branches") or 0)
    except Exception:
        unchanged_branches = 0
    try:
        fingerprint_unchanged_branches = int(apply_summary.get("fingerprint_unchanged_branches") or 0)
    except Exception:
        fingerprint_unchanged_branches = 0
    try:
        diff_applied_branches = int(apply_summary.get("diff_applied_branches") or 0)
    except Exception:
        diff_applied_branches = 0
    try:
        replaced_branches = int(apply_summary.get("replaced_branches") or 0)
    except Exception:
        replaced_branches = 0
    poll_counts = dict(payload.get("poll_counts") or {}) if isinstance(payload.get("poll_counts"), dict) else {}
    return {
        "accepted": bool(payload.get("accepted")),
        "scenario_id": str(payload.get("scenario_id") or "").strip() or None,
        "scenario_switch_mode": str(payload.get("scenario_switch_mode") or "").strip() or None,
        "switch_skipped": bool(payload.get("switch_skipped")),
        "skip_reason": str(payload.get("skip_reason") or "").strip() or None,
        "resolver_cache_hit": bool(resolver.get("cache_hit")),
        "resolver_source": str(resolver.get("source") or "").strip() or None,
        "changed_branches": changed_branches,
        "unchanged_branches": unchanged_branches,
        "fingerprint_unchanged_branches": fingerprint_unchanged_branches,
        "diff_applied_branches": diff_applied_branches,
        "replaced_branches": replaced_branches,
        "rebuild_status": str(rebuild.get("status") or "").strip() or None,
        "materialization_state": str(materialization.get("readiness_state") or "").strip() or None,
        "rebuild_wait_timeout": bool(payload.get("rebuild_wait_timeout")),
        "poll_counts": poll_counts,
        "control": str(payload.get("control") or "").strip() or None,
        "control_selection": str(payload.get("control_selection") or "").strip() or None,
        "phase_timings_ms": dict(phase_timings),
        "observed_timings_ms": dict(payload.get("observed_timings_ms") or {})
        if isinstance(payload.get("observed_timings_ms"), dict)
        else {},
        "ready_alignment_ms": dict(payload.get("ready_alignment_ms") or {})
        if isinstance(payload.get("ready_alignment_ms"), dict)
        else {},
        "ready_alignment_source": str(payload.get("ready_alignment_source") or "").strip() or None,
        "materialization": dict(materialization),
        "timings_ms": switch_timings,
        "rebuild_timings_ms": (
            dict(payload.get("rebuild_timings_ms") or {})
            if isinstance(payload.get("rebuild_timings_ms"), dict)
            else dict(rebuild.get("timings_ms") or {})
            if isinstance(rebuild.get("timings_ms"), dict)
            else {}
        ),
        "semantic_rebuild_timings_ms": dict(payload.get("semantic_rebuild_timings_ms") or {})
        if isinstance(payload.get("semantic_rebuild_timings_ms"), dict)
        else dict(rebuild.get("semantic_rebuild_timings_ms") or {})
        if isinstance(rebuild.get("semantic_rebuild_timings_ms"), dict)
        else {},
        "ydoc_timings_ms": dict(payload.get("ydoc_timings_ms") or {})
        if isinstance(payload.get("ydoc_timings_ms"), dict)
        else dict(rebuild.get("ydoc_timings_ms") or {})
        if isinstance(rebuild.get("ydoc_timings_ms"), dict)
        else {},
    }


def _benchmark_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    phase_summary = _aggregate_benchmark_timing_maps(
        runs,
        key="phase_timings_ms",
        names=(
            "time_to_accept",
            "time_to_pointer_update",
            "time_to_first_structure",
            "time_to_interactive_focus",
            "time_to_full_hydration",
        ),
    )
    observed_summary = _aggregate_benchmark_timing_maps(
        runs,
        key="observed_timings_ms",
        names=("time_to_accept", "time_to_first_paint", "time_to_interactive", "time_to_ready"),
    )
    switch_summary = _aggregate_benchmark_timing_maps(runs, key="timings_ms")
    rebuild_summary = _aggregate_benchmark_timing_maps(runs, key="rebuild_timings_ms")
    semantic_summary = _aggregate_benchmark_timing_maps(runs, key="semantic_rebuild_timings_ms")
    ydoc_summary = _aggregate_benchmark_timing_maps(runs, key="ydoc_timings_ms")
    poll_summary = _aggregate_benchmark_timing_maps(runs, key="poll_counts")
    ready_alignment_summary = _aggregate_benchmark_timing_maps(runs, key="ready_alignment_ms")

    changed_values = [
        float(run.get("changed_branches") or 0)
        for run in runs
        if run.get("changed_branches") is not None
    ]
    unchanged_values = [
        float(run.get("unchanged_branches") or 0)
        for run in runs
        if run.get("unchanged_branches") is not None
    ]
    fingerprint_unchanged_values = [
        float(run.get("fingerprint_unchanged_branches") or 0)
        for run in runs
        if run.get("fingerprint_unchanged_branches") is not None
    ]
    diff_applied_values = [
        float(run.get("diff_applied_branches") or 0)
        for run in runs
        if run.get("diff_applied_branches") is not None
    ]
    replaced_values = [
        float(run.get("replaced_branches") or 0)
        for run in runs
        if run.get("replaced_branches") is not None
    ]
    skipped_total = sum(1 for run in runs if bool(run.get("switch_skipped")))
    cache_hit_total = sum(1 for run in runs if bool(run.get("resolver_cache_hit")))
    ready_timeout_total = sum(1 for run in runs if bool(run.get("rebuild_wait_timeout")))
    rebuild_status_totals: dict[str, int] = {}
    for run in runs:
        status = str(run.get("rebuild_status") or "").strip().lower()
        if not status:
            continue
        rebuild_status_totals[status] = int(rebuild_status_totals.get(status) or 0) + 1
    summary: dict[str, Any] = {
        "iterations": len(runs),
        "switch_skipped_total": skipped_total,
        "resolver_cache_hit_total": cache_hit_total,
        "rebuild_wait_timeout_total": ready_timeout_total,
        "phase_timings_ms": phase_summary,
        "observed_timings_ms": observed_summary,
    }
    if switch_summary:
        summary["timings_ms"] = switch_summary
    if rebuild_summary:
        summary["rebuild_timings_ms"] = rebuild_summary
    if semantic_summary:
        summary["semantic_rebuild_timings_ms"] = semantic_summary
    if ydoc_summary:
        summary["ydoc_timings_ms"] = ydoc_summary
    if poll_summary:
        summary["poll_counts"] = poll_summary
    if ready_alignment_summary:
        summary["ready_alignment_ms"] = ready_alignment_summary
    changed_aggregate = _aggregate_benchmark_values(changed_values)
    if changed_aggregate:
        summary["changed_branches"] = changed_aggregate
    unchanged_aggregate = _aggregate_benchmark_values(unchanged_values)
    if unchanged_aggregate:
        summary["unchanged_branches"] = unchanged_aggregate
    fingerprint_unchanged_aggregate = _aggregate_benchmark_values(fingerprint_unchanged_values)
    if fingerprint_unchanged_aggregate:
        summary["fingerprint_unchanged_branches"] = fingerprint_unchanged_aggregate
    diff_applied_aggregate = _aggregate_benchmark_values(diff_applied_values)
    if diff_applied_aggregate and any(value > 0 for value in diff_applied_values):
        summary["diff_applied_branches"] = diff_applied_aggregate
    replaced_aggregate = _aggregate_benchmark_values(replaced_values)
    if replaced_aggregate and any(value > 0 for value in replaced_values):
        summary["replaced_branches"] = replaced_aggregate
    if rebuild_status_totals:
        summary["rebuild_status_totals"] = dict(sorted(rebuild_status_totals.items()))
    return summary


def _print_benchmark_summary(summary: dict[str, Any]) -> None:
    phase_summary = summary.get("phase_timings_ms") if isinstance(summary.get("phase_timings_ms"), dict) else {}
    for metric_name in (
        "time_to_accept",
        "time_to_pointer_update",
        "time_to_first_structure",
        "time_to_interactive_focus",
        "time_to_full_hydration",
    ):
        item = phase_summary.get(metric_name) if isinstance(phase_summary.get(metric_name), dict) else {}
        if not item:
            continue
        typer.echo(
            f"summary.{metric_name}: "
            f"avg={float(item.get('avg')):.3f} "
            f"min={float(item.get('min')):.3f} "
            f"max={float(item.get('max')):.3f}"
        )
    observed_summary = summary.get("observed_timings_ms") if isinstance(summary.get("observed_timings_ms"), dict) else {}
    for metric_name in ("time_to_accept", "time_to_first_paint", "time_to_interactive", "time_to_ready"):
        item = observed_summary.get(metric_name) if isinstance(observed_summary.get(metric_name), dict) else {}
        if not item:
            continue
        typer.echo(
            f"summary.observed.{metric_name}: "
            f"avg={float(item.get('avg')):.3f} "
            f"min={float(item.get('min')):.3f} "
            f"max={float(item.get('max')):.3f}"
        )
    ready_alignment = summary.get("ready_alignment_ms") if isinstance(summary.get("ready_alignment_ms"), dict) else {}
    server_ready = ready_alignment.get("server_ready") if isinstance(ready_alignment.get("server_ready"), dict) else {}
    if server_ready:
        typer.echo(
            f"summary.ready_server: avg={float(server_ready.get('avg')):.3f} "
            f"min={float(server_ready.get('min')):.3f} max={float(server_ready.get('max')):.3f}"
        )
    observation_lag = ready_alignment.get("observation_lag") if isinstance(ready_alignment.get("observation_lag"), dict) else {}
    if observation_lag:
        typer.echo(
            f"summary.ready_observation_lag: avg={float(observation_lag.get('avg')):.3f} "
            f"min={float(observation_lag.get('min')):.3f} max={float(observation_lag.get('max')):.3f}"
        )
    changed = summary.get("changed_branches") if isinstance(summary.get("changed_branches"), dict) else {}
    if changed:
        typer.echo(
            f"summary.changed_branches: avg={float(changed.get('avg')):.3f} "
            f"min={float(changed.get('min')):.3f} max={float(changed.get('max')):.3f}"
        )
    unchanged = summary.get("unchanged_branches") if isinstance(summary.get("unchanged_branches"), dict) else {}
    if unchanged:
        typer.echo(
            f"summary.unchanged_branches: avg={float(unchanged.get('avg')):.3f} "
            f"min={float(unchanged.get('min')):.3f} max={float(unchanged.get('max')):.3f}"
        )
    fingerprint_unchanged = (
        summary.get("fingerprint_unchanged_branches")
        if isinstance(summary.get("fingerprint_unchanged_branches"), dict)
        else {}
    )
    if fingerprint_unchanged:
        typer.echo(
            f"summary.fingerprint_unchanged_branches: avg={float(fingerprint_unchanged.get('avg')):.3f} "
            f"min={float(fingerprint_unchanged.get('min')):.3f} max={float(fingerprint_unchanged.get('max')):.3f}"
        )
    diff_applied = summary.get("diff_applied_branches") if isinstance(summary.get("diff_applied_branches"), dict) else {}
    if diff_applied:
        typer.echo(
            f"summary.diff_applied_branches: avg={float(diff_applied.get('avg')):.3f} "
            f"min={float(diff_applied.get('min')):.3f} max={float(diff_applied.get('max')):.3f}"
        )
    replaced = summary.get("replaced_branches") if isinstance(summary.get("replaced_branches"), dict) else {}
    if replaced:
        typer.echo(
            f"summary.replaced_branches: avg={float(replaced.get('avg')):.3f} "
            f"min={float(replaced.get('min')):.3f} max={float(replaced.get('max')):.3f}"
        )
    rebuild_status_totals = summary.get("rebuild_status_totals") if isinstance(summary.get("rebuild_status_totals"), dict) else {}
    if rebuild_status_totals:
        parts = [f"{status}={int(total)}" for status, total in rebuild_status_totals.items()]
        typer.echo(f"summary.rebuild_status: {' '.join(parts)}")
    poll_summary = summary.get("poll_counts") if isinstance(summary.get("poll_counts"), dict) else {}
    for poll_name in (
        "rebuild",
        "rebuild_describe_fallback",
        "rebuild_transient_failures",
        "materialization",
        "materialization_describe_fallback",
        "materialization_transient_failures",
    ):
        item = poll_summary.get(poll_name) if isinstance(poll_summary.get(poll_name), dict) else {}
        if not item:
            continue
        typer.echo(
            f"summary.polls.{poll_name}: "
            f"avg={float(item.get('avg')):.3f} "
            f"min={float(item.get('min')):.3f} "
            f"max={float(item.get('max')):.3f}"
        )
    typer.echo(
        f"summary.flags: skipped={int(summary.get('switch_skipped_total') or 0)}/{int(summary.get('iterations') or 0)} "
        f"cache_hits={int(summary.get('resolver_cache_hit_total') or 0)}/{int(summary.get('iterations') or 0)} "
        f"ready_timeouts={int(summary.get('rebuild_wait_timeout_total') or 0)}/{int(summary.get('iterations') or 0)}"
    )


def _print_benchmark_timing_group(summary: dict[str, Any], *, key: str, label: str) -> None:
    group = summary.get(key) if isinstance(summary.get(key), dict) else {}
    if not group:
        return
    typer.echo(f"summary.{label}:")
    ranked = sorted(
        (
            (str(name or "").strip(), dict(item or {}))
            for name, item in group.items()
            if str(name or "").strip() and isinstance(item, dict)
        ),
        key=lambda entry: (-float(entry[1].get("avg") or 0.0), entry[0]),
    )
    for name, item in ranked:
        typer.echo(
            f"  {name}: avg={float(item.get('avg')):.3f} "
            f"min={float(item.get('min')):.3f} max={float(item.get('max')):.3f}"
        )


def _print_switch_summary(payload: dict[str, Any]) -> None:
    mode = str(payload.get("scenario_switch_mode") or "").strip()
    skipped = bool(payload.get("switch_skipped"))
    reason = str(payload.get("skip_reason") or "").strip()
    background = bool(payload.get("background_rebuild"))
    if not mode and not skipped and not reason:
        return
    typer.echo(
        "switch: "
        f"mode={mode or '-'} "
        f"skipped={'yes' if skipped else 'no'} "
        f"background={'yes' if background else 'no'} "
        f"reason={reason or '-'}"
    )


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
        from adaos.services.node_config import resolve_effective_root_base_url

        cfg.root_settings.base_url = resolve_effective_root_base_url(
            root.strip(),
            zone_id=getattr(cfg, "zone_id", None),
        )
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
            memory_status, memory_payload = _supervisor_memory_probe(control=control0, token=token)
            if memory_status == 200 and isinstance(memory_payload, dict):
                supervisor_payload["memory"] = (
                    memory_payload.get("memory") if isinstance(memory_payload.get("memory"), dict) else {}
                )
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    _print_switch_summary(payload)
    _print_projection_refresh_summary(payload)
    _print_rebuild_summary(payload)
    _print_timings_summary(payload)
    _print_timings_summary(payload, key="switch_timings_ms")
    _print_timings_summary(payload, key="rebuild_timings_ms")
    _print_timings_summary(payload, key="semantic_rebuild_timings_ms")
    _print_timings_summary(payload, key="phase_timings_ms")
    _print_apply_summary(payload)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    _print_materialization_summary(payload)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        _print_yjs_runtime_summary({"runtime": runtime})


def _node_yjs_materialization_action(
    *,
    webspace: str,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = _resolve_node_control_base_url(explicit=control)
    status_code, payload = _control_get_json(
        control=control0,
        path=f"/api/node/yjs/webspaces/{webspace}/materialization?include_runtime=1",
        token=_resolved_local_control_token(control0, cfg),
        timeout=5.0,
    )
    if status_code == 404:
        status_code, payload = _control_get_json(
            control=control0,
            path=f"/api/node/yjs/webspaces/{webspace}",
            token=_resolved_local_control_token(control0, cfg),
            timeout=8.0,
        )
    if status_code is None:
        typer.secho("[AdaOS] yjs materialization failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] yjs materialization failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)
    if json_output:
        _print(payload, json_output=True)
        return
    typer.echo(f"materialization: webspace={payload.get('webspace_id') or webspace}")
    _print_rebuild_summary(payload)
    _print_materialization_summary(payload)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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


def _node_yjs_benchmark_scenario_action(
    *,
    webspace: str,
    scenario_id: str,
    baseline_scenario: str | None,
    iterations: int,
    wait_ready: bool,
    ready_timeout_sec: float,
    poll_interval_sec: float,
    detail: bool,
    control: str | None,
    json_output: bool,
) -> None:
    from adaos.apps.cli.active_control import resolve_control_base_url

    cfg = load_config()
    control0 = _resolve_node_control_base_url(explicit=control)
    token0 = _resolved_local_control_token(control0, cfg)
    selected_control, control_selection, status_code, describe_payload = _resolve_benchmark_control(
        control=control0,
        token=token0,
        webspace=webspace,
        timeout_sec=8.0,
    )
    token = _resolved_local_control_token(selected_control, cfg)
    if status_code is None:
        typer.secho(_control_error_message("yjs benchmark-scenario", describe_payload), fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(describe_payload, dict):
        typer.secho(f"[AdaOS] yjs benchmark-scenario failed: HTTP {status_code}", fg=typer.colors.RED)
        if describe_payload:
            typer.echo(describe_payload)
        raise typer.Exit(code=1)

    webspace_payload = describe_payload.get("webspace") if isinstance(describe_payload.get("webspace"), dict) else {}
    resolved_baseline = str(baseline_scenario or "").strip()
    if not resolved_baseline:
        for candidate in (
            webspace_payload.get("home_scenario"),
            webspace_payload.get("current_scenario"),
            "web_desktop",
        ):
            token_candidate = str(candidate or "").strip()
            if token_candidate and token_candidate != scenario_id:
                resolved_baseline = token_candidate
                break
    if resolved_baseline == scenario_id:
        resolved_baseline = ""

    def _switch_or_exit(target_scenario: str) -> dict[str, Any]:
        request_started_at = time.time()
        request_started = time.perf_counter()
        code, payload = _control_post_json(
            control=selected_control,
            path=f"/api/node/yjs/webspaces/{webspace}/scenario",
            token=token,
            body={
                "scenario_id": target_scenario,
                "wait_for_rebuild": bool(wait_ready),
            },
            timeout=90.0,
        )
        request_elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 3)
        if code is None:
            typer.secho(_control_error_message("yjs benchmark-scenario", payload), fg=typer.colors.RED)
            raise typer.Exit(code=2)
        if code != 200 or not isinstance(payload, dict):
            typer.secho(f"[AdaOS] yjs benchmark-scenario failed: HTTP {code}", fg=typer.colors.RED)
            if payload:
                typer.echo(payload)
            raise typer.Exit(code=1)
        if not bool(payload.get("accepted")):
            typer.secho("[AdaOS] yjs benchmark-scenario failed: scenario switch was not accepted", fg=typer.colors.RED)
            typer.echo(payload)
            raise typer.Exit(code=1)
        merged_payload = dict(payload)
        observed_timings = (
            dict(merged_payload.get("observed_timings_ms") or {})
            if isinstance(merged_payload.get("observed_timings_ms"), dict)
            else {}
        )
        observed_timings["time_to_accept"] = request_elapsed_ms
        merged_payload["observed_timings_ms"] = observed_timings
        if wait_ready:
            rebuild_state = merged_payload.get("rebuild") if isinstance(merged_payload.get("rebuild"), dict) else {}
            inline_wait_completed = (
                not bool(merged_payload.get("background_rebuild"))
                and _benchmark_rebuild_is_terminal(
                    rebuild_state,
                    request_id=str(rebuild_state.get("request_id") or "").strip() or None,
                    scenario_id=target_scenario,
                )
            )
            if inline_wait_completed:
                embedded_materialization = (
                    dict(rebuild_state.get("materialization") or {})
                    if isinstance(rebuild_state.get("materialization"), dict)
                    else None
                )
                if embedded_materialization and not isinstance(merged_payload.get("materialization"), dict):
                    merged_payload["materialization"] = embedded_materialization
                accept_from_phase = _timing_value(merged_payload, key="phase_timings_ms", name="time_to_accept")
                observed_timings["time_to_accept"] = accept_from_phase if accept_from_phase is not None else request_elapsed_ms
                observed_timings.setdefault("time_to_ready", request_elapsed_ms)
                merged_payload["observed_timings_ms"] = observed_timings
                merged_payload["rebuild_wait_timeout"] = False
                merged_payload["poll_counts"] = _benchmark_poll_counts()
            else:
                (
                    final_rebuild,
                    final_materialization,
                    materialization_observed_timings,
                    wait_elapsed_ms,
                    timed_out,
                    poll_counts,
                ) = _wait_for_benchmark_rebuild(
                    control=selected_control,
                    token=token,
                    webspace=webspace,
                    scenario_id=target_scenario,
                    initial_rebuild=rebuild_state,
                    timeout_sec=ready_timeout_sec,
                    poll_interval_sec=poll_interval_sec,
                )
                if final_rebuild:
                    merged_payload = _merge_benchmark_rebuild_payload(merged_payload, final_rebuild)
                if final_materialization:
                    merged_payload["materialization"] = dict(final_materialization)
                merged_payload["rebuild_wait_timeout"] = bool(timed_out)
                observed_timings = (
                    dict(merged_payload.get("observed_timings_ms") or {})
                    if isinstance(merged_payload.get("observed_timings_ms"), dict)
                    else {}
                )
                observed_timings.update(
                    {
                        key: value
                        for key, value in materialization_observed_timings.items()
                        if key and value is not None
                    }
                )
                if wait_elapsed_ms is not None:
                    observed_timings.setdefault("time_to_ready", round(request_elapsed_ms + float(wait_elapsed_ms), 3))
                merged_payload["observed_timings_ms"] = observed_timings
                merged_payload["poll_counts"] = dict(poll_counts or {})
        ready_alignment, ready_alignment_source = _benchmark_ready_alignment(
            merged_payload,
            request_started_at=request_started_at,
        )
        if ready_alignment:
            merged_payload["ready_alignment_ms"] = ready_alignment
        if ready_alignment_source:
            merged_payload["ready_alignment_source"] = ready_alignment_source
        merged_payload["control"] = selected_control
        merged_payload["control_selection"] = control_selection
        return merged_payload

    runs: list[dict[str, Any]] = []
    for iteration in range(1, max(iterations, 1) + 1):
        payload = _switch_or_exit(scenario_id)
        run = _extract_benchmark_run(payload)
        run["iteration"] = iteration
        runs.append(run)
        if resolved_baseline:
            _switch_or_exit(resolved_baseline)

    summary = _benchmark_summary(runs)
    result = {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace,
        "scenario_id": scenario_id,
        "baseline_scenario": resolved_baseline or None,
        "iterations": len(runs),
        "control": selected_control,
        "control_selection": control_selection,
        "runs": runs,
        "summary": summary,
    }
    if json_output:
        _print(result, json_output=True)
        return

    typer.echo(
        f"yjs benchmark-scenario: webspace={webspace} "
        f"scenario={scenario_id} baseline={resolved_baseline or '-'} iterations={len(runs)}"
    )
    if detail or selected_control != control0:
        typer.echo(
            f"benchmark.control: requested={control0} selected={selected_control} reason={control_selection}"
        )
    for run in runs:
        phase = run.get("phase_timings_ms") if isinstance(run.get("phase_timings_ms"), dict) else {}
        observed = run.get("observed_timings_ms") if isinstance(run.get("observed_timings_ms"), dict) else {}
        poll_counts = run.get("poll_counts") if isinstance(run.get("poll_counts"), dict) else {}
        ready_alignment = run.get("ready_alignment_ms") if isinstance(run.get("ready_alignment_ms"), dict) else {}
        line = (
            f"run={int(run.get('iteration') or 0)} "
            f"mode={run.get('scenario_switch_mode') or '-'} "
            f"skipped={'yes' if run.get('switch_skipped') else 'no'} "
            f"cache_hit={'yes' if run.get('resolver_cache_hit') else 'no'} "
            f"changed={int(run.get('changed_branches') or 0)} "
            f"fp_skip={int(run.get('fingerprint_unchanged_branches') or 0)} "
        )
        diff_applied = int(run.get("diff_applied_branches") or 0)
        replaced = int(run.get("replaced_branches") or 0)
        if diff_applied or replaced:
            line += f"diff={diff_applied} replace={replaced} "
        line += (
            f"accept={float(phase.get('time_to_accept') or 0.0):.3f} "
            f"ready={float(observed.get('time_to_ready') or 0.0):.3f} "
            f"lag={float(ready_alignment.get('observation_lag') or 0.0):.3f} "
            f"first={float(phase.get('time_to_first_structure') or 0.0):.3f} "
            f"interactive={float(phase.get('time_to_interactive_focus') or 0.0):.3f} "
            f"full={float(phase.get('time_to_full_hydration') or 0.0):.3f} "
            f"polls=rebuild:{int(poll_counts.get('rebuild') or 0)}/materialization:{int(poll_counts.get('materialization') or 0)} "
            f"status={run.get('rebuild_status') or '-'}"
        )
        typer.echo(line)
        if detail:
            _print_timings_summary(run, key="poll_counts", label="  poll_counts")
            _print_timings_summary(run, key="observed_timings_ms", label="  observed_timings_ms")
            _print_timings_summary(run, key="ready_alignment_ms", label="  ready_alignment_ms")
            ready_alignment_source = str(run.get("ready_alignment_source") or "").strip()
            if ready_alignment_source:
                typer.echo(f"  ready_alignment_source: {ready_alignment_source}")
            _print_timings_summary(run, key="timings_ms", label="  switch_timings_ms")
            _print_timings_summary(run, key="rebuild_timings_ms", label="  rebuild_timings_ms")
            _print_timings_summary(run, key="semantic_rebuild_timings_ms", label="  semantic_rebuild_timings_ms")
            _print_timings_summary(run, key="ydoc_timings_ms", label="  ydoc_timings_ms")
            _print_materialization_summary(run)
    _print_benchmark_summary(summary)
    if detail:
        _print_benchmark_timing_group(summary, key="timings_ms", label="switch_timings_ms")
        _print_benchmark_timing_group(summary, key="rebuild_timings_ms", label="rebuild_timings_ms")
        _print_benchmark_timing_group(summary, key="semantic_rebuild_timings_ms", label="semantic_rebuild_timings_ms")
        _print_benchmark_timing_group(summary, key="ydoc_timings_ms", label="ydoc_timings_ms")
        _print_benchmark_timing_group(summary, key="ready_alignment_ms", label="ready_alignment_ms")


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


@yjs_app.command("materialization")
def node_yjs_materialization(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to inspect materialization state for"),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_materialization_action(
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


@yjs_app.command("benchmark-scenario")
def node_yjs_benchmark_scenario(
    webspace: str = typer.Option("default", "--webspace", help="Webspace id to benchmark"),
    scenario_id: str = typer.Option(..., "--scenario-id", help="Target scenario id to measure"),
    baseline_scenario: str | None = typer.Option(
        None,
        "--baseline-scenario",
        help="Optional scenario to restore between runs (defaults to home/current if different)",
    ),
    iterations: int = typer.Option(3, "--iterations", min=1, help="How many measured target switches to run"),
    wait_ready: bool = typer.Option(
        True,
        "--wait-ready/--no-wait-ready",
        help="Wait for terminal background rebuild state and include ready/full metrics",
    ),
    ready_timeout_sec: float = typer.Option(
        60.0,
        "--ready-timeout-sec",
        min=1.0,
        help="How long to wait for each background rebuild to reach a terminal state",
    ),
    poll_interval_sec: float = typer.Option(
        0.25,
        "--poll-interval-sec",
        min=0.05,
        help="Polling interval while waiting for rebuild completion",
    ),
    detail: bool = typer.Option(
        False,
        "--detail/--no-detail",
        help="Print per-run and aggregated switch/rebuild/semantic timing breakdown",
    ),
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    _node_yjs_benchmark_scenario_action(
        webspace=webspace,
        scenario_id=scenario_id,
        baseline_scenario=baseline_scenario,
        iterations=iterations,
        wait_ready=wait_ready,
        ready_timeout_sec=ready_timeout_sec,
        poll_interval_sec=poll_interval_sec,
        detail=detail,
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
    control0 = _resolve_node_control_base_url(explicit=control)
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
