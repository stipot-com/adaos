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
    hub_root_transport_strategy_snapshot,
    runtime_signal_snapshot,
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
        getattr(ctx.settings, "api_base", None)
        or getattr(getattr(conf, "root_settings", None), "base_url", None)
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
