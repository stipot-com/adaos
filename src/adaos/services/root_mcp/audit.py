from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx

from .model import RootMcpAuditEvent


def _audit_path() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    state_dir = Path(raw() if callable(raw) else raw)
    path = state_dir / "root_mcp" / "audit.jsonl"
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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in reversed(lines):
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
            out.append(payload)
        if len(out) >= max(1, int(limit)):
            break
    return out


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
    result_summary = payload.get("result_summary") or {}
    if isinstance(result_summary, dict):
        keys = result_summary.get("keys")
        if isinstance(keys, list) and keys:
            return f"{tool_id} -> {status} ({', '.join(str(item) for item in keys[:4])})"
    return f"{tool_id} -> {status}"


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
            }
        )
        if len(items) >= max_items:
            break
    return {
        "target_id": target_token,
        "count": len(items),
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
    }


__all__ = [
    "append_audit_event",
    "list_audit_events",
    "target_activity_feed",
    "target_capability_usage_summary",
]
