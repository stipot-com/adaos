from __future__ import annotations

import logging
import os
import time
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest
from adaos.services.hub_root_protocol_store import ack_stream_message, prepare_stream_message
from adaos.services.reliability import (
    channel_diagnostics_snapshot,
    hub_root_protocol_snapshot,
    hub_root_transport_strategy_snapshot,
    runtime_signal_snapshot,
    yjs_sync_runtime_snapshot,
)
from adaos.services.root.client import RootHttpClient
from adaos.services.runtime_identity import runtime_identity_snapshot, runtime_instance_id, runtime_transition_role
from adaos.services.root_mcp.infra_access_skill import build_operational_surface
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot

_CONTROL_LIFECYCLE_FLOW_ID = "hub_root.control.lifecycle"
_LOG = logging.getLogger("adaos.startup")


def _stage_mark(stage: str, *, started: float | None = None, failed: Exception | None = None) -> float:
    now = time.perf_counter()
    if started is None:
        _LOG.info("startup stage start stage=%s", stage)
        return now
    duration = now - started
    if failed is None:
        _LOG.info("startup stage done stage=%s duration_s=%.3f", stage, duration)
    else:
        _LOG.warning(
            "startup stage failed stage=%s duration_s=%.3f error=%s",
            stage,
            duration,
            type(failed).__name__,
        )
    return now


def _control_lifecycle_stream_id(conf) -> str:
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    return f"hub-control:lifecycle:{subnet_id}:{runtime_instance_id()}"


def _control_lifecycle_authority_epoch(conf) -> str:
    manifest = active_slot_manifest() or {}
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    node_id = str(getattr(conf, "node_id", "") or "").strip() or "unknown_node"
    commit = str(manifest.get("git_commit") or "").strip()
    branch = str(manifest.get("target_rev") or manifest.get("git_branch") or "").strip()
    parts = [f"hub:{subnet_id}", f"node:{node_id}"]
    parts.append(f"role:{runtime_transition_role()}")
    parts.append(f"instance:{runtime_instance_id()}")
    if commit:
        parts.append(f"commit:{commit[:12]}")
    elif branch:
        parts.append(f"branch:{branch}")
    return "|".join(parts)


def _root_client(conf) -> RootHttpClient | None:
    try:
        ctx = get_ctx()
    except Exception:
        return None
    base_url = str(
        getattr(getattr(conf, "root_settings", None), "base_url", None)
        or getattr(ctx.settings, "api_base", None)
        or ""
    ).rstrip("/")
    if not base_url:
        return None
    cert_path = conf.hub_cert_path()
    key_path = conf.hub_key_path()
    ca_path = conf.ca_cert_path()
    if not cert_path.exists() or not key_path.exists():
        return None
    verify: str | bool = str(ca_path) if ca_path.exists() else True
    return RootHttpClient(base_url=base_url, verify=verify, cert=(str(cert_path), str(key_path)))


def _environment(conf) -> str:
    return (
        str(os.getenv("ADAOS_ENVIRONMENT") or "").strip().lower()
        or str(os.getenv("ADAOS_SUBNET_ENVIRONMENT") or "").strip().lower()
        or "test"
    )


def _zone(conf) -> str | None:
    token = str(os.getenv("ADAOS_ROOT_ZONE") or "").strip()
    return token or None


def _infra_access_operational_surface() -> dict[str, Any]:
    return build_operational_surface()


def _compact_protocol_runtime() -> dict[str, Any]:
    runtime = hub_root_protocol_snapshot()
    route_runtime = dict(runtime.get("route_runtime") or {}) if isinstance(runtime.get("route_runtime"), dict) else {}
    route_flows = dict(route_runtime.get("flows") or {}) if isinstance(route_runtime.get("flows"), dict) else {}
    control_flow = dict(route_flows.get("control") or {}) if isinstance(route_flows.get("control"), dict) else {}
    frame_flow = dict(route_flows.get("frame") or {}) if isinstance(route_flows.get("frame"), dict) else {}
    integration_outboxes = dict(runtime.get("integration_outboxes") or {}) if isinstance(runtime.get("integration_outboxes"), dict) else {}
    telegram = dict(integration_outboxes.get("telegram") or {}) if isinstance(integration_outboxes.get("telegram"), dict) else {}
    control_authority = dict(runtime.get("control_authority") or {}) if isinstance(runtime.get("control_authority"), dict) else {}
    assessment = dict(runtime.get("assessment") or {}) if isinstance(runtime.get("assessment"), dict) else {}
    return {
        "assessment": {
            "state": str(assessment.get("state") or "").strip() or None,
            "reason": str(assessment.get("reason") or "").strip() or None,
        },
        "pending_ack_streams": int(runtime.get("pending_ack_streams") or 0),
        "updated_at": runtime.get("updated_at"),
        "route_runtime": {
            "active_tunnels": int(route_runtime.get("active_tunnels") or 0),
            "pending_tunnels": int(route_runtime.get("pending_tunnels") or 0),
            "pending_events": int(route_runtime.get("pending_events") or 0),
            "max_pending_events": int(route_runtime.get("max_pending_events") or 0),
            "pending_chunks": int(route_runtime.get("pending_chunks") or 0),
            "last_force_close_at": route_runtime.get("last_force_close_at"),
            "last_no_upstream_at": route_runtime.get("last_no_upstream_at"),
            "last_publish_fail_at": route_runtime.get("last_publish_fail_at"),
            "last_reset_at": route_runtime.get("last_reset_at"),
            "last_reset_reason": str(route_runtime.get("last_reset_reason") or "").strip() or None,
            "updated_at": route_runtime.get("updated_at"),
            "flows": {
                "control": {
                    "state": str(control_flow.get("state") or "").strip() or None,
                    "reason": str(control_flow.get("reason") or "").strip() or None,
                    "last_event": str(control_flow.get("last_event") or "").strip() or None,
                    "last_error": str(control_flow.get("last_error") or "").strip() or None,
                },
                "frame": {
                    "state": str(frame_flow.get("state") or "").strip() or None,
                    "reason": str(frame_flow.get("reason") or "").strip() or None,
                    "last_event": str(frame_flow.get("last_event") or "").strip() or None,
                    "last_error": str(frame_flow.get("last_error") or "").strip() or None,
                },
            },
        },
        "integration_outboxes": {
            "telegram": {
                "size": int(telegram.get("size") or 0),
                "max_size": int(telegram.get("max_size") or 0) if telegram.get("max_size") is not None else None,
                "durable_store": bool(telegram.get("durable_store")),
                "publish_ok": int(telegram.get("publish_ok") or 0),
                "publish_fail": int(telegram.get("publish_fail") or 0),
                "last_error": str(telegram.get("last_error") or "").strip() or None,
                "last_error_at": telegram.get("last_error_at"),
                "updated_at": telegram.get("updated_at"),
            }
        },
        "control_authority": {
            "state": str(control_authority.get("state") or "").strip() or None,
            "reason": str(control_authority.get("reason") or "").strip() or None,
            "stale_after_s": control_authority.get("stale_after_s"),
            "ack_age_s": control_authority.get("ack_age_s"),
            "issue_age_s": control_authority.get("issue_age_s"),
            "issued_cursor": int(control_authority.get("issued_cursor") or 0),
            "acked_cursor": int(control_authority.get("acked_cursor") or 0),
            "pending": bool(control_authority.get("pending")),
        },
    }


def _compact_yjs_runtime(conf) -> dict[str, Any]:
    snapshot = yjs_sync_runtime_snapshot(role=str(getattr(conf, "role", "") or ""))
    transport = dict(snapshot.get("transport") or {}) if isinstance(snapshot.get("transport"), dict) else {}
    selected_webspace = dict(snapshot.get("selected_webspace") or {}) if isinstance(snapshot.get("selected_webspace"), dict) else {}
    selected_gateway_room = dict(selected_webspace.get("gateway_room") or {}) if isinstance(selected_webspace.get("gateway_room"), dict) else {}
    selected_room_diag = dict(selected_gateway_room.get("diagnostic") or {}) if isinstance(selected_gateway_room.get("diagnostic"), dict) else {}
    selected_store_runtime = dict(selected_gateway_room.get("ystore_runtime") or {}) if isinstance(selected_gateway_room.get("ystore_runtime"), dict) else {}
    selected_store = {}
    selected_webspace_id = str(snapshot.get("selected_webspace_id") or "").strip() or None
    webspaces = dict(snapshot.get("webspaces") or {}) if isinstance(snapshot.get("webspaces"), dict) else {}
    if selected_webspace_id and isinstance(webspaces.get(selected_webspace_id), dict):
        selected_store = dict(webspaces.get(selected_webspace_id) or {})
    assessment = dict(snapshot.get("assessment") or {}) if isinstance(snapshot.get("assessment"), dict) else {}
    rebuild = dict(selected_webspace.get("rebuild") or {}) if isinstance(selected_webspace.get("rebuild"), dict) else {}
    return {
        "available": bool(snapshot.get("available")),
        "assessment": {
            "state": str(assessment.get("state") or "").strip() or None,
            "reason": str(assessment.get("reason") or "").strip() or None,
        },
        "selected_webspace_id": selected_webspace_id,
        "webspace_total": int(snapshot.get("webspace_total") or 0),
        "active_webspace_total": int(snapshot.get("active_webspace_total") or 0),
        "compaction_eligible_webspace_total": int(snapshot.get("compaction_eligible_webspace_total") or 0),
        "update_log_total": int(snapshot.get("update_log_total") or 0),
        "replay_window_total": int(snapshot.get("replay_window_total") or 0),
        "replay_window_byte_total": int(snapshot.get("replay_window_byte_total") or 0),
        "transport": {
            "active_yws_connections": int(transport.get("active_yws_connections") or 0),
            "storm_detected": bool(transport.get("storm_detected")),
            "recent_open_60s": int(transport.get("recent_open_60s") or 0),
            "server_ready": bool(transport.get("server_ready")),
            "active_room_total": int(transport.get("active_room_total") or 0),
            "room_reset_total": int(transport.get("room_reset_total") or 0),
            "reload_recent_60s": int(transport.get("reload_recent_60s") or 0),
            "reset_recent_60s": int(transport.get("reset_recent_60s") or 0),
            "update_stream_buffer_used_total": int(transport.get("update_stream_buffer_used_total") or 0),
            "update_stream_waiting_send_total": int(transport.get("update_stream_waiting_send_total") or 0),
            "update_stream_waiting_receive_total": int(transport.get("update_stream_waiting_receive_total") or 0),
        },
        "selected_webspace": {
            "title": str(selected_webspace.get("title") or "").strip() or None,
            "kind": str(selected_webspace.get("kind") or "").strip() or None,
            "source_mode": str(selected_webspace.get("source_mode") or "").strip() or None,
            "rebuild_status": str(rebuild.get("status") or "").strip() or None,
            "store_runtime": {
                "log_mode": str(selected_store.get("log_mode") or "").strip() or None,
                "update_log_entries": int(selected_store.get("update_log_entries") or 0),
                "max_update_log_entries": int(selected_store.get("max_update_log_entries") or 0),
                "replay_window_entries": int(selected_store.get("replay_window_entries") or 0),
                "replay_window_limit": int(selected_store.get("replay_window_limit") or 0),
                "replay_window_bytes": int(selected_store.get("replay_window_bytes") or 0),
                "replay_window_byte_limit": int(selected_store.get("replay_window_byte_limit") or 0),
                "runtime_compaction_eligible": bool(selected_store.get("runtime_compaction_eligible")),
                "snapshot_file_exists": bool(selected_store.get("snapshot_file_exists")),
                "snapshot_file_size": int(selected_store.get("snapshot_file_size") or 0),
            },
            "gateway_room": {
                "client_total": int(selected_gateway_room.get("client_total") or 0),
                "ready": bool(selected_gateway_room.get("ready")),
                "started": bool(selected_gateway_room.get("started")),
                "task_group_active": bool(selected_gateway_room.get("task_group_active")),
                "ystore_attached": bool(selected_gateway_room.get("ystore_attached")),
                "diagnostic": {
                    "pending_send_tasks": int(selected_room_diag.get("pending_send_tasks") or 0),
                    "pending_store_tasks": int(selected_room_diag.get("pending_store_tasks") or 0),
                    "update_total": int(selected_room_diag.get("update_total") or 0),
                    "update_bytes_total": int(selected_room_diag.get("update_bytes_total") or 0),
                },
                "send_stream": {
                    "current_buffer_used": int(((selected_room_diag.get("send_stream") or {}) if isinstance(selected_room_diag.get("send_stream"), dict) else {}).get("current_buffer_used") or 0),
                    "max_buffer_size": int(((selected_room_diag.get("send_stream") or {}) if isinstance(selected_room_diag.get("send_stream"), dict) else {}).get("max_buffer_size") or 0),
                    "tasks_waiting_send": int(((selected_room_diag.get("send_stream") or {}) if isinstance(selected_room_diag.get("send_stream"), dict) else {}).get("tasks_waiting_send") or 0),
                    "tasks_waiting_receive": int(((selected_room_diag.get("send_stream") or {}) if isinstance(selected_room_diag.get("send_stream"), dict) else {}).get("tasks_waiting_receive") or 0),
                },
                "ystore": {
                    "update_log_entries": int(selected_store_runtime.get("update_log_entries") or 0),
                    "update_log_bytes": int(selected_store_runtime.get("update_log_bytes") or 0),
                    "replay_window_bytes": int(selected_store_runtime.get("replay_window_bytes") or 0),
                    "last_update_bytes": int(selected_store_runtime.get("last_update_bytes") or 0),
                },
            },
        },
    }


def _control_report_headers() -> dict[str, str]:
    token = str(
        os.getenv("ADAOS_HUB_CONTROL_REPORT_TOKEN")
        or os.getenv("ADAOS_ROOT_HUB_REPORT_TOKEN")
        or ""
    ).strip()
    if not token:
        return {}
    return {"X-AdaOS-Hub-Report-Token": token}


def build_control_lifecycle_report(conf) -> dict[str, Any]:
    lifecycle = runtime_lifecycle_snapshot()
    signals = runtime_signal_snapshot()
    diagnostics = channel_diagnostics_snapshot()
    strategy = hub_root_transport_strategy_snapshot()

    root_signal = signals.get("root_control") if isinstance(signals.get("root_control"), dict) else {}
    route_signal = signals.get("route") if isinstance(signals.get("route"), dict) else {}
    root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
    route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
    assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    slot_manifest = active_slot_manifest() or {}
    identity = runtime_identity_snapshot()

    return {
        "target_id": f"hub:{str(getattr(conf, 'subnet_id', '') or '').strip() or 'unknown_hub'}",
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "role": str(getattr(conf, "role", "") or ""),
        "runtime_instance_id": str(identity.get("runtime_instance_id") or ""),
        "transition_role": str(identity.get("transition_role") or "active"),
        "environment": _environment(conf),
        "zone": _zone(conf),
        "lifecycle": {
            "node_state": str(lifecycle.get("node_state") or "unknown"),
            "reason": str(lifecycle.get("reason") or ""),
            "draining": bool(lifecycle.get("draining")),
            "accepting_new_work": bool(lifecycle.get("accepting_new_work")),
        },
        "root_control": {
            "status": str(root_signal.get("status") or ""),
            "summary": str(root_signal.get("summary") or ""),
            "stability_state": str(((root_diag.get("stability") or {}) if isinstance(root_diag.get("stability"), dict) else {}).get("state") or ""),
            "last_incident_class": str(root_diag.get("last_incident_class") or ""),
        },
        "route": {
            "status": str(route_signal.get("status") or ""),
            "summary": str(route_signal.get("summary") or ""),
            "stability_state": str(((route_diag.get("stability") or {}) if isinstance(route_diag.get("stability"), dict) else {}).get("state") or ""),
            "last_incident_class": str(route_diag.get("last_incident_class") or ""),
        },
        "transport": {
            "requested_transport": str(strategy.get("requested_transport") or ""),
            "effective_transport": str(strategy.get("effective_transport") or ""),
            "selected_server": str(strategy.get("selected_server") or ""),
            "last_event": str(strategy.get("last_event") or ""),
            "assessment_state": str(assessment.get("state") or ""),
        },
        "runtime": {
            "active_slot": str(slot_manifest.get("slot") or slot_manifest.get("slot_id") or ""),
            "git_commit": str(slot_manifest.get("git_commit") or ""),
            "target_rev": str(slot_manifest.get("target_rev") or slot_manifest.get("git_branch") or ""),
            "runtime_instance_id": str(identity.get("runtime_instance_id") or ""),
            "transition_role": str(identity.get("transition_role") or "active"),
            "started_at": identity.get("started_at"),
            "hostname": str(identity.get("hostname") or ""),
        },
        "protocol_runtime": _compact_protocol_runtime(),
        "yjs_runtime": _compact_yjs_runtime(conf),
        "operational_surface": _infra_access_operational_surface(),
    }


def report_hub_control_lifecycle_state(conf) -> dict[str, Any] | None:
    client = _root_client(conf)
    if client is None:
        return None
    payload_started = _stage_mark("control_report_build_payload")
    payload = build_control_lifecycle_report(conf)
    _stage_mark("control_report_build_payload", started=payload_started)
    prepare_started = _stage_mark("control_report_prepare_stream")
    protocol_meta = prepare_stream_message(
        stream_id=_control_lifecycle_stream_id(conf),
        flow_id=_CONTROL_LIFECYCLE_FLOW_ID,
        traffic_class="control",
        delivery_class="must_not_lose",
        message_type="state_report",
        payload=payload,
        ttl_ms=120_000,
        authority_epoch=_control_lifecycle_authority_epoch(conf),
        ack_required=True,
    )
    _stage_mark("control_report_prepare_stream", started=prepare_started)
    payload["reported_at"] = protocol_meta.get("issued_at")
    payload["_protocol"] = dict(protocol_meta)
    send_started = _stage_mark("control_report_send_http")
    result = client.hub_control_report(payload=payload, headers=_control_report_headers() or None)
    _stage_mark("control_report_send_http", started=send_started)
    ack_started = _stage_mark("control_report_ack_stream")
    try:
        ack_stream_message(
            _control_lifecycle_stream_id(conf),
            message_id=str(protocol_meta.get("message_id") or ""),
            cursor=int(protocol_meta.get("cursor") or 0),
            duplicate=bool((result or {}).get("duplicate")),
            result="duplicate" if bool((result or {}).get("duplicate")) else "accepted",
        )
        _stage_mark("control_report_ack_stream", started=ack_started)
    except Exception as exc:
        _stage_mark("control_report_ack_stream", started=ack_started, failed=exc)
        logging.getLogger("adaos.hub-io").debug("control lifecycle stream ack failed", exc_info=True)
    return result


__all__ = [
    "build_control_lifecycle_report",
    "report_hub_control_lifecycle_state",
]
