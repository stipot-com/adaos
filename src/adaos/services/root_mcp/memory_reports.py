from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reports_path() -> Path:
    path = _state_dir() / "root_mcp" / "memory_profile_reports.json"
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
    _reports_path().write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _session_id(report: Mapping[str, Any]) -> str:
    session = _normalize_mapping(report.get("session"))
    token = str(session.get("session_id") or report.get("session_id") or "").strip()
    return token or f"memory-session:{new_id()}"


def _hub_id(report: Mapping[str, Any]) -> str:
    token = str(report.get("target_id") or "").strip()
    if token:
        return token
    subnet_id = str(report.get("subnet_id") or "").strip() or "unknown"
    return f"hub:{subnet_id}"


def ingest_memory_profile_report(
    report: Mapping[str, Any],
    *,
    ingest_auth: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(report)
    session_id = _session_id(payload)
    hub_id = _hub_id(payload)
    auth = _normalize_mapping(ingest_auth)
    items = _read_reports()
    current = items.get(session_id) or {}
    message_id = str((_normalize_mapping(payload.get("_protocol"))).get("message_id") or "").strip() or None
    duplicate = bool(message_id and str(current.get("message_id") or "") == message_id)
    stored = {
        "session_id": session_id,
        "hub_id": hub_id,
        "subnet_id": payload.get("subnet_id"),
        "zone": payload.get("zone"),
        "reported_at": str(payload.get("reported_at") or _iso_now()),
        "message_id": message_id,
        "report": payload,
        "ingest_auth": auth,
        "server_time_utc": _iso_now(),
        "event_id": new_id(),
    }
    if not duplicate:
        items[session_id] = stored
        _write_reports(items)
    else:
        stored["event_id"] = str(current.get("event_id") or new_id())
        stored["server_time_utc"] = str(current.get("server_time_utc") or _iso_now())
    return {
        "ok": True,
        "duplicate": duplicate,
        "hub_id": hub_id,
        "session_id": session_id,
        "event_id": stored["event_id"],
        "server_time_utc": stored["server_time_utc"],
        "report_verified": bool(auth.get("verified")),
        "report_auth_method": str(auth.get("method") or "").strip() or "unknown",
    }


def list_memory_profile_reports(
    *,
    hub_id: str | None = None,
    session_id: str | None = None,
    session_state: str | None = None,
    suspected_only: bool = False,
    subnet_id: str | None = None,
    zone: str | None = None,
) -> list[dict[str, Any]]:
    items = _read_reports()
    hub_filter = str(hub_id or "").strip() or None
    session_filter = str(session_id or "").strip() or None
    state_filter = str(session_state or "").strip().lower() or None
    subnet_filter = str(subnet_id or "").strip() or None
    zone_filter = str(zone or "").strip() or None
    out: list[dict[str, Any]] = []
    for current_session_id, item in sorted(items.items()):
        if session_filter and current_session_id != session_filter:
            continue
        if hub_filter and str(item.get("hub_id") or "").strip() != hub_filter:
            continue
        report = dict(item.get("report") or {})
        session = _normalize_mapping(report.get("session"))
        if state_filter and str(session.get("session_state") or "").strip().lower() != state_filter:
            continue
        if suspected_only and not bool(session.get("suspected_leak")):
            continue
        if subnet_filter and str(report.get("subnet_id") or "").strip() != subnet_filter:
            continue
        if zone_filter and str(report.get("zone") or "").strip() != zone_filter:
            continue
        out.append(
            {
                "session_id": current_session_id,
                "hub_id": item.get("hub_id"),
                "report": report,
                "ingest_auth": dict(item.get("ingest_auth") or {}),
                "event_id": item.get("event_id"),
                "server_time_utc": item.get("server_time_utc"),
            }
        )
    return out


def get_memory_profile_report(session_id: str) -> dict[str, Any] | None:
    token = str(session_id or "").strip()
    if not token:
        return None
    item = _read_reports().get(token)
    if not isinstance(item, dict):
        return None
    return {
        "session_id": token,
        "hub_id": item.get("hub_id"),
        "report": dict(item.get("report") or {}),
        "ingest_auth": dict(item.get("ingest_auth") or {}),
        "event_id": item.get("event_id"),
        "server_time_utc": item.get("server_time_utc"),
    }


def get_memory_profile_artifact(
    session_id: str,
    artifact_id: str,
    *,
    offset: int = 0,
    max_bytes: int = 256 * 1024,
) -> dict[str, Any] | None:
    report_item = get_memory_profile_report(session_id)
    if report_item is None:
        return None
    report = report_item.get("report") if isinstance(report_item.get("report"), dict) else {}
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    refs = session.get("artifact_refs") if isinstance(session.get("artifact_refs"), list) else []
    artifact = next(
        (
            dict(item)
            for item in refs
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip() == str(artifact_id or "").strip()
        ),
        None,
    )
    if artifact is None:
        return None
    payloads = report.get("artifact_payloads") if isinstance(report.get("artifact_payloads"), list) else []
    payload = next(
        (
            dict(item)
            for item in payloads
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip() == str(artifact_id or "").strip()
        ),
        None,
    )
    start = max(0, int(offset or 0))
    chunk_limit = max(1, min(int(max_bytes or 256 * 1024), 1024 * 1024))
    exists = payload is not None
    content = payload.get("content") if isinstance(payload, dict) else None
    transfer: dict[str, Any] = {
        "offset": start,
        "requested_max_bytes": chunk_limit,
        "size_bytes": int(artifact.get("size_bytes") or 0),
        "chunk_bytes": 0,
        "remaining_bytes": 0,
        "truncated": False,
        "encoding": "unavailable",
        "pull_supported": exists,
    }
    delivery: dict[str, Any] = {
        "mode": "root_inline_content" if exists else str(artifact.get("fetch_strategy") or "local_control_pull"),
        "source_api_path": artifact.get("source_api_path"),
        "published_ref": artifact.get("published_ref"),
    }
    if exists and isinstance(content, dict):
        raw = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        size_bytes = len(raw)
        transfer["size_bytes"] = size_bytes
        transfer["chunk_bytes"] = size_bytes
        transfer["remaining_bytes"] = 0
        transfer["truncated"] = False
        transfer["encoding"] = "json"
        transfer["pull_supported"] = True
    return {
        "session_id": str(session_id or "").strip(),
        "hub_id": report_item.get("hub_id"),
        "artifact": artifact,
        "exists": exists,
        "content": content if isinstance(content, dict) else None,
        "transfer": transfer,
        "delivery": delivery,
    }


def list_memory_profile_artifacts(session_id: str) -> dict[str, Any] | None:
    report_item = get_memory_profile_report(session_id)
    if report_item is None:
        return None
    report = report_item.get("report") if isinstance(report_item.get("report"), dict) else {}
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    refs = session.get("artifact_refs") if isinstance(session.get("artifact_refs"), list) else []
    artifacts = [dict(item) for item in refs if isinstance(item, dict)]
    policy = report.get("artifact_policy") if isinstance(report.get("artifact_policy"), dict) else {}
    return {
        "session_id": str(session_id or "").strip(),
        "hub_id": report_item.get("hub_id"),
        "artifact_policy": dict(policy),
        "artifacts": artifacts,
    }


__all__ = [
    "get_memory_profile_artifact",
    "list_memory_profile_artifacts",
    "get_memory_profile_report",
    "ingest_memory_profile_report",
    "list_memory_profile_reports",
]
