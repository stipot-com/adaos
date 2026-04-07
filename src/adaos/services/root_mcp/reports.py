from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id

from .targets import get_managed_target, upsert_managed_target


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reports_path() -> Path:
    path = _state_dir() / "root_mcp" / "control_reports.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_reports() -> dict[str, dict[str, Any]]:
    path = _reports_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw_items = payload.get("items") if isinstance(payload, dict) else {}
    if not isinstance(raw_items, dict):
        return {}
    return {str(key): dict(value) for key, value in raw_items.items() if isinstance(value, Mapping)}


def _write_reports(items: dict[str, dict[str, Any]]) -> None:
    path = _reports_path()
    payload = {"items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _coerce_target_id(report: Mapping[str, Any]) -> str:
    token = str(report.get("target_id") or "").strip()
    if token:
        return token
    subnet_id = str(report.get("subnet_id") or "").strip() or "unknown"
    return f"hub:{subnet_id}"


def _coerce_status(report: Mapping[str, Any]) -> str:
    lifecycle = _normalize_mapping(report.get("lifecycle"))
    node_state = str(lifecycle.get("node_state") or "").strip().lower()
    if node_state in {"running", "active", "ready"}:
        return "online"
    if node_state in {"draining", "degraded"}:
        return "degraded"
    if node_state in {"stopped", "offline"}:
        return "offline"
    return "planned"


def _merge_operational_surface(report: Mapping[str, Any], target_id: str) -> dict[str, Any]:
    current_target = get_managed_target(target_id)
    current_surface = _normalize_mapping(getattr(current_target, "operational_surface", {}))
    payload_surface = _normalize_mapping(report.get("operational_surface"))
    merged = dict(current_surface)
    merged.update(payload_surface)
    if "published_by" not in merged:
        merged["published_by"] = "skill:infra_access_skill"
    return merged


def sync_target_from_control_report(
    report: Mapping[str, Any],
    *,
    ingest_auth: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target_id = _coerce_target_id(report)
    subnet_id = str(report.get("subnet_id") or "").strip() or None
    zone = str(report.get("zone") or "").strip() or None
    environment = str(report.get("environment") or "").strip().lower() or "test"
    role = str(report.get("role") or "").strip().lower() or "hub"
    title = str(report.get("title") or "").strip() or (f"Hub {subnet_id}" if subnet_id else target_id)
    transport = _normalize_mapping(report.get("transport"))
    if "channel" not in transport:
        transport["channel"] = "hub_root_protocol"
    auth = _normalize_mapping(ingest_auth)
    verified = bool(auth.get("verified"))
    auth_method = str(auth.get("method") or "").strip() or "unknown"
    trust_state = "verified" if verified else "unverified"
    upserted = upsert_managed_target(
        {
            "target_id": target_id,
            "title": title,
            "kind": role if role in {"hub", "member", "browser"} else "hub",
            "environment": environment,
            "status": _coerce_status(report),
            "zone": zone,
            "subnet_id": subnet_id,
            "transport": transport,
            "operational_surface": _merge_operational_surface(report, target_id),
            "access": _normalize_mapping(report.get("access")),
            "policy": _normalize_mapping(report.get("policy")),
            "meta": {
                "last_reported_at": str(report.get("reported_at") or _iso_now()),
                "registry_source": "control_report",
                "report_auth_method": auth_method,
                "report_verified": verified,
                "report_trust": trust_state,
            },
        }
    )
    return upserted.to_dict()


def ingest_control_report(
    report: Mapping[str, Any],
    *,
    ingest_auth: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(report)
    target_id = _coerce_target_id(payload)
    message_id = str((_normalize_mapping(payload.get("_protocol"))).get("message_id") or "").strip() or None
    items = _read_reports()
    current = items.get(target_id) or {}
    duplicate = bool(message_id and str(current.get("message_id") or "") == message_id)
    auth = _normalize_mapping(ingest_auth)
    target = sync_target_from_control_report(payload, ingest_auth=auth)
    stored = {
        "target_id": target_id,
        "hub_id": target_id,
        "subnet_id": payload.get("subnet_id"),
        "reported_at": str(payload.get("reported_at") or _iso_now()),
        "message_id": message_id,
        "report": payload,
        "target": target,
        "ingest_auth": auth,
        "server_time_utc": _iso_now(),
        "event_id": new_id(),
    }
    if not duplicate:
        items[target_id] = stored
        _write_reports(items)
    else:
        stored["event_id"] = str(current.get("event_id") or new_id())
        stored["server_time_utc"] = str(current.get("server_time_utc") or _iso_now())
    return {
        "ok": True,
        "duplicate": duplicate,
        "hub_id": target_id,
        "target_id": target_id,
        "event_id": stored["event_id"],
        "server_time_utc": stored["server_time_utc"],
        "report_verified": bool(auth.get("verified")),
        "report_auth_method": str(auth.get("method") or "").strip() or "unknown",
    }


def list_control_reports(*, hub_id: str | None = None) -> list[dict[str, Any]]:
    items = _read_reports()
    out: list[dict[str, Any]] = []
    target_filter = str(hub_id or "").strip() or None
    for target_id, item in sorted(items.items()):
        if target_filter and target_filter != target_id:
            continue
        out.append(
            {
                "hub_id": target_id,
                "report": dict(item.get("report") or {}),
                "target": dict(item.get("target") or {}),
                "ingest_auth": dict(item.get("ingest_auth") or {}),
                "event_id": item.get("event_id"),
                "server_time_utc": item.get("server_time_utc"),
            }
        )
    return out


def control_report_registry_summary() -> dict[str, Any]:
    items = _read_reports()
    return {
        "available": True,
        "registry_path": str(_reports_path()),
        "report_count": len(items),
    }


__all__ = [
    "control_report_registry_summary",
    "ingest_control_report",
    "list_control_reports",
    "sync_target_from_control_report",
]
