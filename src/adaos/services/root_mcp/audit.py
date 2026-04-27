from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx

from .model import RootMcpAuditEvent


def _root_mcp_state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.root_mcp_state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _audit_path() -> Path:
    path = _root_mcp_state_dir() / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_audit_event(event: RootMcpAuditEvent) -> RootMcpAuditEvent:
    path = _audit_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
    return event


def _matches_filters(
    payload: dict,
    *,
    tool_id: str | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
    target_id: str | None = None,
    subnet_id: str | None = None,
) -> bool:
    if tool_id and str(payload.get("tool_id") or "").strip() != str(tool_id).strip():
        return False
    if trace_id and str(payload.get("trace_id") or "").strip() != str(trace_id).strip():
        return False
    if actor and str(payload.get("actor") or "").strip() != str(actor).strip():
        return False
    if target_id and str(payload.get("target_id") or "").strip() != str(target_id).strip():
        return False
    if subnet_id:
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            return False
        if str(meta.get("subnet_id") or "").strip() != str(subnet_id).strip():
            return False
    return True


def list_audit_events(
    *,
    limit: int = 50,
    tool_id: str | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
    target_id: str | None = None,
    subnet_id: str | None = None,
) -> list[dict]:
    path = _audit_path()
    if not path.exists():
        return []
    max_items = max(1, int(limit))
    matches: deque[dict] = deque(maxlen=max_items)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                text = str(raw or "").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and _matches_filters(
                    payload,
                    tool_id=tool_id,
                    trace_id=trace_id,
                    actor=actor,
                    target_id=target_id,
                    subnet_id=subnet_id,
                ):
                    matches.append(payload)
    except OSError:
        return []
    return list(reversed(matches))


def _normalize_statuses(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip().lower()
        if token and token not in out:
            out.append(token)
    return out


def _event_kind(payload: dict[str, Any]) -> str:
    tool_id = str(payload.get("tool_id") or "").strip()
    if tool_id == "hub.control_report.ingest":
        return "control_report"
    if tool_id == "hub.memory_profile_report.ingest" or tool_id.startswith("hub.memory."):
        return "profile_ops"
    if tool_id.startswith("hub."):
        return "target_tool"
    if tool_id.startswith("root.access_tokens."):
        return "target_token_management"
    if tool_id.startswith("development."):
        return "development_tool"
    return "audit_event"


def _event_summary(payload: dict[str, Any]) -> str:
    tool_id = str(payload.get("tool_id") or "").strip() or "unknown"
    status = str(payload.get("status") or "").strip() or "unknown"
    meta = payload.get("meta") or {}
    profile_ops = dict(meta.get("profile_ops") or {}) if isinstance(meta, dict) and isinstance(meta.get("profile_ops"), dict) else {}
    if tool_id == "hub.memory_profile_report.ingest" or tool_id.startswith("hub.memory."):
        action = str(profile_ops.get("action") or tool_id.removeprefix("hub.memory.") or "unknown").strip()
        session_id = str(profile_ops.get("session_id") or meta.get("session_id") or "").strip()
        artifact_id = str(profile_ops.get("artifact_id") or "").strip()
        parts = [f"profile_ops.{action}", status]
        if session_id:
            parts.append(f"session={session_id}")
        if artifact_id:
            parts.append(f"artifact={artifact_id}")
        return " ".join(parts)
    result_summary = payload.get("result_summary") or {}
    if isinstance(result_summary, dict):
        keys = result_summary.get("keys")
        if isinstance(keys, list) and keys:
            return f"{tool_id} -> {status} ({', '.join(str(item) for item in keys[:4])})"
    return f"{tool_id} -> {status}"


def _event_class(payload: dict[str, Any]) -> str:
    tool_id = str(payload.get("tool_id") or "").strip()
    if tool_id == "hub.control_report.ingest":
        return "control_report_ingest"
    if tool_id == "hub.memory_profile_report.ingest":
        return "profile_report_ingest"
    if tool_id.startswith("hub.memory."):
        return "profile_ops"
    if tool_id in {"hub.issue_mcp_session", "hub.list_mcp_sessions", "hub.revoke_mcp_session"} or tool_id.startswith("root.mcp_sessions."):
        return "mcp_session"
    if tool_id in {"hub.issue_access_token", "hub.list_access_tokens", "hub.revoke_access_token"} or tool_id.startswith("root.access_tokens."):
        return "access_token"
    if tool_id.startswith("hub."):
        return "target_operation"
    if tool_id.startswith("development.") or tool_id.startswith("adaos_dev."):
        return "development"
    return "audit_event"


def _parse_iso_datetime(value: Any) -> datetime | None:
    token = str(value or "").strip()
    if not token:
        return None
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_timestamp(values: list[str]) -> str | None:
    best: datetime | None = None
    for value in values:
        parsed = _parse_iso_datetime(value)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best = parsed
    if best is None:
        return None
    return best.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timeline_details(payload: dict[str, Any]) -> dict[str, Any]:
    tool_id = str(payload.get("tool_id") or "").strip()
    meta = dict(payload.get("meta") or {}) if isinstance(payload.get("meta"), dict) else {}
    result_summary = dict(payload.get("result_summary") or {}) if isinstance(payload.get("result_summary"), dict) else {}
    details: dict[str, Any] = {}
    subnet_id = str(meta.get("subnet_id") or "").strip()
    zone = str(meta.get("zone") or "").strip()
    if subnet_id:
        details["subnet_id"] = subnet_id
    if zone:
        details["zone"] = zone
    if tool_id == "hub.control_report.ingest":
        details.update(
            {
                "report_verified": bool(meta.get("report_verified")),
                "report_auth_method": str(meta.get("report_auth_method") or "").strip() or None,
                "message_id": str(meta.get("message_id") or "").strip() or None,
                "duplicate": bool(result_summary.get("duplicate")),
            }
        )
    profile_ops = dict(meta.get("profile_ops") or {}) if isinstance(meta.get("profile_ops"), dict) else {}
    if tool_id == "hub.memory_profile_report.ingest" or tool_id.startswith("hub.memory."):
        session_id = str(profile_ops.get("session_id") or meta.get("session_id") or "").strip()
        profile_mode = str(profile_ops.get("profile_mode") or "").strip()
        artifact_id = str(profile_ops.get("artifact_id") or "").strip()
        action = str(profile_ops.get("action") or tool_id.removeprefix("hub.memory.") or "unknown").strip()
        if action:
            details["profile_action"] = action
        if session_id:
            details["session_id"] = session_id
        if profile_mode:
            details["profile_mode"] = profile_mode
        if artifact_id:
            details["artifact_id"] = artifact_id
    if tool_id.startswith("root.access_tokens.") or tool_id.startswith("root.mcp_sessions."):
        token_or_session_id = str(result_summary.get("token_id") or result_summary.get("session_id") or "").strip()
        if token_or_session_id:
            details["resource_id"] = token_or_session_id
    return {key: value for key, value in details.items() if value is not None}


def target_activity_feed(
    *,
    target_id: str,
    limit: int = 50,
    statuses: list[str] | None = None,
    include_control_reports: bool = True,
) -> dict[str, Any]:
    target_token = str(target_id or "").strip()
    if not target_token:
        raise ValueError("target_id is required")

    max_items = max(1, min(int(limit), 200))
    allowed_statuses = _normalize_statuses(statuses or [])
    raw_items = list_audit_events(limit=max(100, max_items * 6), target_id=target_token)
    items: list[dict[str, Any]] = []
    for payload in raw_items:
        tool_id = str(payload.get("tool_id") or "").strip()
        if not include_control_reports and tool_id == "hub.control_report.ingest":
            continue
        status = str(payload.get("status") or "").strip().lower() or "unknown"
        if allowed_statuses and status not in allowed_statuses:
            continue
        meta = payload.get("meta") or {}
        trace = meta.get("trace") if isinstance(meta, dict) and isinstance(meta.get("trace"), dict) else {}
        profile_ops = dict(meta.get("profile_ops") or {}) if isinstance(meta, dict) and isinstance(meta.get("profile_ops"), dict) else {}
        items.append(
            {
                "event_id": payload.get("event_id"),
                "trace_id": payload.get("trace_id"),
                "request_id": payload.get("request_id"),
                "target_id": target_token,
                "kind": _event_kind(payload),
                "tool_id": tool_id,
                "status": status,
                "actor": payload.get("actor"),
                "recorded_at": payload.get("finished_at") or payload.get("started_at"),
                "execution_adapter": payload.get("execution_adapter"),
                "policy_decision": payload.get("policy_decision"),
                "summary": _event_summary(payload),
                "error": dict(payload.get("error") or {}) if isinstance(payload.get("error"), dict) else {},
                "trace": {
                    "routing": dict(trace.get("routing") or {}) if isinstance(trace.get("routing"), dict) else {},
                    "request": dict(trace.get("request") or {}) if isinstance(trace.get("request"), dict) else {},
                    "redactions": list(trace.get("redactions") or []) if isinstance(trace.get("redactions"), list) else [],
                },
                "profile_ops": profile_ops,
            }
        )
        if len(items) >= max_items:
            break
    return {
        "target_id": target_token,
        "history_kind": "audit_activity_view",
        "derived_from": ["root_audit", *([] if not include_control_reports else ["control_report_ingest_events"])],
        "limitations": [
            "This feed is optimized for recent activity summaries.",
            "Use the typed subnet timeline surface for richer operational history reconstruction.",
        ],
        "count": len(items),
        "items": items,
    }


def target_operational_timeline(
    *,
    target_id: str,
    limit: int = 100,
    include_control_reports: bool = True,
    include_profile_ops: bool = True,
) -> dict[str, Any]:
    target_token = str(target_id or "").strip()
    if not target_token:
        raise ValueError("target_id is required")

    max_items = max(1, min(int(limit), 300))
    raw_items = list_audit_events(limit=max(200, max_items * 8), target_id=target_token)
    items: list[dict[str, Any]] = []
    counts_by_class: dict[str, int] = {}
    recorded_values: list[str] = []

    for payload in raw_items:
        tool_id = str(payload.get("tool_id") or "").strip()
        if not include_control_reports and tool_id == "hub.control_report.ingest":
            continue
        if not include_profile_ops and (tool_id == "hub.memory_profile_report.ingest" or tool_id.startswith("hub.memory.")):
            continue

        event_class = _event_class(payload)
        recorded_at = str(payload.get("finished_at") or payload.get("started_at") or "").strip() or None
        if recorded_at:
            recorded_values.append(recorded_at)
        counts_by_class[event_class] = int(counts_by_class.get(event_class) or 0) + 1
        items.append(
            {
                "event_id": payload.get("event_id"),
                "trace_id": payload.get("trace_id"),
                "request_id": payload.get("request_id"),
                "target_id": target_token,
                "recorded_at": recorded_at,
                "source_kind": "root_audit",
                "event_class": event_class,
                "tool_id": tool_id,
                "status": str(payload.get("status") or "").strip().lower() or "unknown",
                "actor": payload.get("actor"),
                "execution_adapter": payload.get("execution_adapter"),
                "policy_decision": payload.get("policy_decision"),
                "summary": _event_summary(payload),
                "details": _timeline_details(payload),
                "error": dict(payload.get("error") or {}) if isinstance(payload.get("error"), dict) else {},
            }
        )
        if len(items) >= max_items:
            break

    derived_from = ["root_audit"]
    if include_control_reports:
        derived_from.append("control_report_ingest_events")
    if include_profile_ops:
        derived_from.append("memory_profile_report_ingest_events")

    return {
        "target_id": target_token,
        "timeline_kind": "typed_subnet_timeline",
        "history_kind": "operational_timeline",
        "derived_from": derived_from,
        "coverage": {
            "event_history": "audit_backed",
            "control_report_snapshot": "latest_only",
            "limitations": [
                "Timeline items are currently reconstructed from Root MCP audit plus report-ingest events.",
                "Full runtime-only history and bounded log references are not yet attached as first-class timeline entries.",
            ],
        },
        "summary": {
            "item_count": len(items),
            "latest_event_at": _latest_timestamp(recorded_values),
            "classes": counts_by_class,
        },
        "items": items,
    }


def target_capability_usage_summary(
    *,
    target_id: str,
    limit: int = 200,
    include_control_reports: bool = False,
) -> dict[str, Any]:
    target_token = str(target_id or "").strip()
    if not target_token:
        raise ValueError("target_id is required")

    raw_items = list_audit_events(limit=max(100, min(int(limit), 1000)), target_id=target_token)
    by_tool: dict[str, dict[str, Any]] = {}
    total = 0
    error_count = 0
    last_activity_at = None
    for payload in raw_items:
        tool_id = str(payload.get("tool_id") or "").strip()
        if not tool_id:
            continue
        if not include_control_reports and tool_id == "hub.control_report.ingest":
            continue
        total += 1
        status = str(payload.get("status") or "").strip().lower() or "unknown"
        if status != "ok":
            error_count += 1
        recorded_at = payload.get("finished_at") or payload.get("started_at")
        if recorded_at and (last_activity_at is None or str(recorded_at) > str(last_activity_at)):
            last_activity_at = recorded_at
        item = by_tool.setdefault(
            tool_id,
            {
                "tool_id": tool_id,
                "count": 0,
                "ok_count": 0,
                "error_count": 0,
                "last_status": status,
                "last_activity_at": recorded_at,
                "execution_adapters": {},
            },
        )
        item["count"] += 1
        if status == "ok":
            item["ok_count"] += 1
        else:
            item["error_count"] += 1
        item["last_status"] = status
        item["last_activity_at"] = recorded_at
        adapter = str(payload.get("execution_adapter") or "").strip() or "unknown"
        adapter_counts = item["execution_adapters"]
        adapter_counts[adapter] = int(adapter_counts.get(adapter) or 0) + 1

    tools = sorted(by_tool.values(), key=lambda item: (-int(item["count"]), str(item["tool_id"])))
    return {
        "target_id": target_token,
        "window_event_count": total,
        "error_count": error_count,
        "last_activity_at": last_activity_at,
        "tools": tools,
        "planes": {
            "profile_ops_event_count": sum(int(item.get("count") or 0) for item in tools if str(item.get("tool_id") or "").startswith("hub.memory.") or str(item.get("tool_id") or "") == "hub.memory_profile_report.ingest"),
        },
    }


__all__ = [
    "append_audit_event",
    "list_audit_events",
    "target_activity_feed",
    "target_operational_timeline",
    "target_capability_usage_summary",
]
