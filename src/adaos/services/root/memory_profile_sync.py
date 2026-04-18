from __future__ import annotations

import logging
import os
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest
from adaos.services.hub_root_protocol_store import ack_stream_message, prepare_stream_message
from adaos.services.root.client import RootHttpClient
from adaos.services.runtime_identity import runtime_instance_id, runtime_transition_role

_MEMORY_PROFILE_FLOW_ID = "hub_root.memory_profile"


def _memory_profile_stream_id(conf, *, session_id: str) -> str:
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    token = str(session_id or "").strip() or "unknown_session"
    return f"hub-memory-profile:{subnet_id}:{runtime_instance_id()}:{token}"


def _memory_profile_authority_epoch(conf, *, session_id: str) -> str:
    manifest = active_slot_manifest() or {}
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "unknown_hub"
    node_id = str(getattr(conf, "node_id", "") or "").strip() or "unknown_node"
    commit = str(manifest.get("git_commit") or "").strip()
    branch = str(manifest.get("target_rev") or manifest.get("git_branch") or "").strip()
    parts = [f"hub:{subnet_id}", f"node:{node_id}"]
    parts.append(f"role:{runtime_transition_role()}")
    parts.append(f"instance:{runtime_instance_id()}")
    parts.append(f"session:{str(session_id or '').strip() or 'unknown_session'}")
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


def build_memory_profile_report(
    conf,
    *,
    session_summary: dict[str, Any],
    operations: list[dict[str, Any]] | None = None,
    telemetry: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = dict(session_summary or {})
    artifact_refs = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
    compact_artifacts: list[dict[str, Any]] = []
    for item in artifact_refs:
        if not isinstance(item, dict):
            continue
        compact_artifacts.append(
            {
                "artifact_id": str(item.get("artifact_id") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "content_type": str(item.get("content_type") or "").strip() or None,
                "size_bytes": item.get("size_bytes"),
                "created_at": item.get("created_at"),
                "published_ref": item.get("published_ref"),
            }
        )
    return {
        "target_id": f"hub:{str(getattr(conf, 'subnet_id', '') or '').strip() or 'unknown_hub'}",
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "role": str(getattr(conf, "role", "") or ""),
        "environment": _environment(conf),
        "zone": _zone(conf),
        "runtime_instance_id": str(summary.get("runtime_instance_id") or runtime_instance_id() or ""),
        "transition_role": str(summary.get("transition_role") or runtime_transition_role() or "active"),
        "session": {
            "session_id": str(summary.get("session_id") or "").strip(),
            "profile_mode": str(summary.get("profile_mode") or "").strip(),
            "session_state": str(summary.get("session_state") or "").strip(),
            "slot": str(summary.get("slot") or "").strip() or None,
            "trigger_source": str(summary.get("trigger_source") or "").strip() or None,
            "trigger_reason": str(summary.get("trigger_reason") or "").strip() or None,
            "trigger_threshold": str(summary.get("trigger_threshold") or "").strip() or None,
            "requested_at": summary.get("requested_at"),
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "stopped_at": summary.get("stopped_at"),
            "stop_reason": str(summary.get("stop_reason") or "").strip() or None,
            "suspected_leak": bool(summary.get("suspected_leak")),
            "baseline_rss_bytes": summary.get("baseline_rss_bytes"),
            "peak_rss_bytes": summary.get("peak_rss_bytes"),
            "rss_growth_bytes": summary.get("rss_growth_bytes"),
            "retry_of_session_id": str(summary.get("retry_of_session_id") or "").strip() or None,
            "retry_root_session_id": str(summary.get("retry_root_session_id") or "").strip() or None,
            "retry_depth": int(summary.get("retry_depth") or 0),
            "artifact_refs": compact_artifacts,
            "top_growth_sites": list(summary.get("top_growth_sites") or [])[:20],
        },
        "operations_tail": [dict(item) for item in (operations or [])[-20:] if isinstance(item, dict)],
        "telemetry_tail": [dict(item) for item in (telemetry or [])[-20:] if isinstance(item, dict)],
    }


def report_hub_memory_profile(
    conf,
    *,
    session_summary: dict[str, Any],
    operations: list[dict[str, Any]] | None = None,
    telemetry: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    client = _root_client(conf)
    if client is None:
        return None
    session_id = str(session_summary.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required for memory profile reports")
    payload = build_memory_profile_report(
        conf,
        session_summary=session_summary,
        operations=operations,
        telemetry=telemetry,
    )
    protocol_meta = prepare_stream_message(
        stream_id=_memory_profile_stream_id(conf, session_id=session_id),
        flow_id=_MEMORY_PROFILE_FLOW_ID,
        traffic_class="diagnostics",
        delivery_class="must_not_lose",
        message_type="state_report",
        payload=payload,
        ttl_ms=300_000,
        authority_epoch=_memory_profile_authority_epoch(conf, session_id=session_id),
        ack_required=True,
    )
    payload["reported_at"] = protocol_meta.get("issued_at")
    payload["_protocol"] = dict(protocol_meta)
    result = client.hub_memory_profile_report(payload=payload)
    try:
        ack_stream_message(
            _memory_profile_stream_id(conf, session_id=session_id),
            message_id=str(protocol_meta.get("message_id") or ""),
            cursor=int(protocol_meta.get("cursor") or 0),
            duplicate=bool((result or {}).get("duplicate")),
            result="duplicate" if bool((result or {}).get("duplicate")) else "accepted",
        )
    except Exception:
        logging.getLogger("adaos.hub-io").debug("memory profile stream ack failed", exc_info=True)
    return result


__all__ = [
    "build_memory_profile_report",
    "report_hub_memory_profile",
]
