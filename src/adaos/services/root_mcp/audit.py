from __future__ import annotations

import json
from pathlib import Path

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


__all__ = ["append_audit_event", "list_audit_events"]
