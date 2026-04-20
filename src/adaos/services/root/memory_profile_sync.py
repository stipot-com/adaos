from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest
from adaos.services.hub_root_protocol_store import ack_stream_message, prepare_stream_message
from adaos.services.root.client import RootHttpClient
from adaos.services.runtime_identity import runtime_instance_id, runtime_transition_role

_MEMORY_PROFILE_FLOW_ID = "hub_root.memory_profile"
_REMOTE_ARTIFACT_MAX_BYTES = 256 * 1024
_REMOTE_ARTIFACT_MAX_COUNT = 6
_REMOTE_ARTIFACT_ALLOWED_KINDS = {
    "tracemalloc_start_snapshot",
    "tracemalloc_final_snapshot",
    "tracemalloc_top_growth",
    "tracemalloc_trace_start",
    "tracemalloc_trace_final",
}


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


def memory_profile_artifact_published_ref(*, session_id: str, artifact_id: str) -> str:
    return f"root://hub-memory-profile/{str(session_id or '').strip()}/{str(artifact_id or '').strip()}"


def memory_profile_artifact_source_api_path(*, session_id: str, artifact_id: str) -> str:
    token = str(session_id or "").strip()
    ref = str(artifact_id or "").strip()
    return f"/api/supervisor/memory/sessions/{token}/artifacts/{ref}"


def _artifact_publish_status(*, kind: str, content_type: str | None, path_text: str) -> tuple[str, bool, Path | None, int | None]:
    if kind not in _REMOTE_ARTIFACT_ALLOWED_KINDS:
        return ("kind_not_allowed", False, None, None)
    if content_type != "application/json":
        return ("content_type_not_supported", False, None, None)
    if not path_text:
        return ("path_missing", False, None, None)
    path = Path(path_text)
    if not path.exists() or not path.is_file():
        return ("file_missing", False, path, None)
    try:
        size_bytes = int(path.stat().st_size)
    except Exception:
        return ("stat_failed", False, path, None)
    if size_bytes > _REMOTE_ARTIFACT_MAX_BYTES:
        return ("size_limit_exceeded", False, path, size_bytes)
    return ("inline_available", True, path, size_bytes)


def _inline_artifact_payloads(session_summary: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary = dict(session_summary or {})
    session_id = str(summary.get("session_id") or "").strip()
    artifact_refs = summary.get("artifact_refs") if isinstance(summary.get("artifact_refs"), list) else []
    compact_artifacts: list[dict[str, Any]] = []
    inline_payloads: list[dict[str, Any]] = []
    for item in artifact_refs:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        content_type = str(item.get("content_type") or "").strip() or None
        path_text = str(item.get("path") or "").strip()
        publish_status, inline_allowed, path, resolved_size = _artifact_publish_status(
            kind=kind,
            content_type=content_type,
            path_text=path_text,
        )
        published_ref = memory_profile_artifact_published_ref(session_id=session_id, artifact_id=artifact_id) if session_id and artifact_id else None
        remote_available = publish_status == "inline_available"
        compact_artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "content_type": content_type,
                "size_bytes": resolved_size if resolved_size is not None else item.get("size_bytes"),
                "created_at": item.get("created_at"),
                "published_ref": published_ref,
                "publish_status": publish_status,
                "remote_available": remote_available,
                "fetch_strategy": "inline_content" if remote_available else "local_control_pull",
                "source_api_path": (
                    memory_profile_artifact_source_api_path(session_id=session_id, artifact_id=artifact_id)
                    if session_id and artifact_id
                    else None
                ),
            }
        )
        if len(inline_payloads) >= _REMOTE_ARTIFACT_MAX_COUNT:
            continue
        if not inline_allowed or path is None:
            continue
        size_bytes = resolved_size if resolved_size is not None else int(item.get("size_bytes") or 0)
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        inline_payloads.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "published_ref": published_ref,
                "content": content,
            }
        )
    if len(inline_payloads) >= _REMOTE_ARTIFACT_MAX_COUNT:
        published_ids = {
            str(item.get("artifact_id") or "").strip()
            for item in inline_payloads
            if isinstance(item, dict)
        }
        compact_artifacts = [
            {
                **item,
                "publish_status": (
                    "inline_limit_reached"
                    if item.get("publish_status") == "inline_available"
                    and str(item.get("artifact_id") or "").strip() not in published_ids
                    else item.get("publish_status")
                ),
                "remote_available": bool(
                    item.get("publish_status") == "inline_available"
                    and str(item.get("artifact_id") or "").strip() in published_ids
                ),
                "fetch_strategy": (
                    "inline_content"
                    if item.get("publish_status") == "inline_available"
                    and str(item.get("artifact_id") or "").strip() in published_ids
                    else "local_control_pull"
                ),
            }
            for item in compact_artifacts
        ]
    return compact_artifacts, inline_payloads


def build_memory_profile_report(
    conf,
    *,
    session_summary: dict[str, Any],
    operations: list[dict[str, Any]] | None = None,
    telemetry: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = dict(session_summary or {})
    compact_artifacts, inline_payloads = _inline_artifact_payloads(summary)
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
        "artifact_payloads": inline_payloads,
        "artifact_policy": {
            "delivery_mode": "inline_json_only",
            "fallback_delivery_mode": "local_control_pull",
            "max_inline_artifacts": _REMOTE_ARTIFACT_MAX_COUNT,
            "max_inline_bytes": _REMOTE_ARTIFACT_MAX_BYTES,
            "allowed_kinds": sorted(_REMOTE_ARTIFACT_ALLOWED_KINDS),
        },
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
    "memory_profile_artifact_published_ref",
    "memory_profile_artifact_source_api_path",
    "report_hub_memory_profile",
]
