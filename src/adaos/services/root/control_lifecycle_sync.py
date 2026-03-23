from __future__ import annotations

import logging
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
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot

_CONTROL_LIFECYCLE_FLOW_ID = "hub_root.control.lifecycle"


def _control_lifecycle_stream_id(conf) -> str:
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    return f"hub-control:lifecycle:{subnet_id}"


def _control_lifecycle_authority_epoch(conf) -> str:
    manifest = active_slot_manifest() or {}
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    node_id = str(getattr(conf, "node_id", "") or "").strip() or "unknown_node"
    commit = str(manifest.get("git_commit") or "").strip()
    branch = str(manifest.get("target_rev") or manifest.get("git_branch") or "").strip()
    parts = [f"hub:{subnet_id}", f"node:{node_id}"]
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

    return {
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "role": str(getattr(conf, "role", "") or ""),
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
    }


def report_hub_control_lifecycle_state(conf) -> dict[str, Any] | None:
    client = _root_client(conf)
    if client is None:
        return None
    payload = build_control_lifecycle_report(conf)
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
    payload["reported_at"] = protocol_meta.get("issued_at")
    payload["_protocol"] = dict(protocol_meta)
    result = client.hub_control_report(payload=payload)
    try:
        ack_stream_message(
            _control_lifecycle_stream_id(conf),
            message_id=str(protocol_meta.get("message_id") or ""),
            cursor=int(protocol_meta.get("cursor") or 0),
            duplicate=bool((result or {}).get("duplicate")),
            result="duplicate" if bool((result or {}).get("duplicate")) else "accepted",
        )
    except Exception:
        logging.getLogger("adaos.hub-io").debug("control lifecycle stream ack failed", exc_info=True)
    return result


__all__ = [
    "build_control_lifecycle_report",
    "report_hub_control_lifecycle_state",
]
