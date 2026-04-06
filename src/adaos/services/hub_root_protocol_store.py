from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from adaos.services.runtime_paths import current_state_dir

_LOCK = threading.RLock()


def _base_state_dir() -> Path:
    return current_state_dir()


def _state_root() -> Path:
    root = _base_state_dir() / "hub_root_protocol"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _streams_path() -> Path:
    return _state_root() / "streams.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _stable_payload_hash(payload: Any) -> str:
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raw = json.dumps({"repr": repr(payload)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stream_template(
    stream_id: str,
    *,
    flow_id: str,
    traffic_class: str,
    delivery_class: str,
    message_type: str,
    ack_required: bool,
) -> dict[str, Any]:
    return {
        "stream_id": stream_id,
        "flow_id": flow_id,
        "traffic_class": traffic_class,
        "delivery_class": delivery_class,
        "message_type": message_type,
        "ack_required": bool(ack_required),
        "last_issued_cursor": 0,
        "last_acked_cursor": 0,
        "issued_total": 0,
        "ack_total": 0,
        "duplicate_total": 0,
        "last_message_id": "",
        "last_operation_key": "",
        "last_issue_at": 0.0,
        "last_ack_at": 0.0,
        "last_duplicate_at": 0.0,
        "last_ack_result": "",
        "pending": None,
        "updated_at": 0.0,
    }


def prepare_stream_message(
    *,
    stream_id: str,
    flow_id: str,
    traffic_class: str,
    delivery_class: str,
    message_type: str,
    payload: Any,
    ttl_ms: int,
    authority_epoch: str,
    ack_required: bool = True,
) -> dict[str, Any]:
    sid = str(stream_id or "").strip()
    if not sid:
        raise ValueError("stream_id is required")
    flow = str(flow_id or "").strip()
    if not flow:
        raise ValueError("flow_id is required")
    traffic = str(traffic_class or "").strip().lower() or "integration"
    delivery = str(delivery_class or "").strip().lower() or "must_not_lose"
    msg_type = str(message_type or "").strip().lower() or "event"
    ttl = max(1000, int(ttl_ms))
    epoch = str(authority_epoch or "").strip()
    now = time.time()
    payload_hash = _stable_payload_hash(payload)
    operation_key = f"{sid}:{payload_hash[:24]}"
    with _LOCK:
        state = _read_json(_streams_path())
        streams = state.setdefault("streams", {})
        entry = streams.get(sid)
        if not isinstance(entry, dict):
            entry = _stream_template(
                sid,
                flow_id=flow,
                traffic_class=traffic,
                delivery_class=delivery,
                message_type=msg_type,
                ack_required=ack_required,
            )
            streams[sid] = entry
        entry["flow_id"] = flow
        entry["traffic_class"] = traffic
        entry["delivery_class"] = delivery
        entry["message_type"] = msg_type
        entry["ack_required"] = bool(ack_required)

        pending = entry.get("pending")
        if isinstance(pending, dict) and str(pending.get("payload_hash") or "") == payload_hash:
            pending["last_send_at"] = now
            pending["send_attempts"] = int(pending.get("send_attempts") or 0) + 1
            entry["updated_at"] = now
            state["updated_at"] = now
            _write_json(_streams_path(), state)
            return {
                "stream_id": sid,
                "message_id": str(pending.get("message_id") or ""),
                "message_type": msg_type,
                "delivery_class": delivery,
                "issued_at": float(pending.get("issued_at") or now),
                "ttl_ms": int(pending.get("ttl_ms") or ttl),
                "authority_epoch": str(pending.get("authority_epoch") or epoch),
                "ack_required": bool(ack_required),
                "cursor": int(pending.get("cursor") or 0),
                "operation_key": str(pending.get("operation_key") or operation_key),
                "reused_pending": True,
            }

        cursor = int(entry.get("last_issued_cursor") or 0) + 1
        message_id = f"{sid}:{cursor}:{payload_hash[:12]}"
        pending = {
            "message_id": message_id,
            "cursor": cursor,
            "issued_at": now,
            "last_send_at": now,
            "send_attempts": 1,
            "payload_hash": payload_hash,
            "operation_key": operation_key,
            "ttl_ms": ttl,
            "authority_epoch": epoch,
        }
        entry["last_issued_cursor"] = cursor
        entry["issued_total"] = int(entry.get("issued_total") or 0) + 1
        entry["last_message_id"] = message_id
        entry["last_operation_key"] = operation_key
        entry["last_issue_at"] = now
        entry["pending"] = pending
        entry["updated_at"] = now
        state["updated_at"] = now
        _write_json(_streams_path(), state)
        return {
            "stream_id": sid,
            "message_id": message_id,
            "message_type": msg_type,
            "delivery_class": delivery,
            "issued_at": now,
            "ttl_ms": ttl,
            "authority_epoch": epoch,
            "ack_required": bool(ack_required),
            "cursor": cursor,
            "operation_key": operation_key,
            "reused_pending": False,
        }


def ack_stream_message(
    stream_id: str,
    *,
    message_id: str | None = None,
    cursor: int | None = None,
    duplicate: bool = False,
    result: str | None = None,
) -> dict[str, Any]:
    sid = str(stream_id or "").strip()
    if not sid:
        raise ValueError("stream_id is required")
    now = time.time()
    with _LOCK:
        state = _read_json(_streams_path())
        streams = state.setdefault("streams", {})
        entry = streams.get(sid)
        if not isinstance(entry, dict):
            entry = _stream_template(
                sid,
                flow_id="",
                traffic_class="integration",
                delivery_class="must_not_lose",
                message_type="state_report",
                ack_required=True,
            )
            streams[sid] = entry
        if cursor is not None:
            entry["last_acked_cursor"] = max(int(entry.get("last_acked_cursor") or 0), int(cursor))
        entry["ack_total"] = int(entry.get("ack_total") or 0) + 1
        if duplicate:
            entry["duplicate_total"] = int(entry.get("duplicate_total") or 0) + 1
            entry["last_duplicate_at"] = now
        if result is not None:
            entry["last_ack_result"] = str(result or "").strip()
        entry["last_ack_at"] = now
        pending = entry.get("pending")
        pending_cursor = None
        pending_message_id = ""
        if isinstance(pending, dict):
            try:
                pending_cursor = int(pending.get("cursor") or 0)
            except Exception:
                pending_cursor = None
            pending_message_id = str(pending.get("message_id") or "")
        if (cursor is not None and pending_cursor == int(cursor)) or (message_id and pending_message_id == str(message_id)):
            entry["pending"] = None
        entry["updated_at"] = now
        state["updated_at"] = now
        _write_json(_streams_path(), state)
        return dict(entry)


def protocol_streams_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        state = _read_json(_streams_path())
    streams = state.get("streams")
    if not isinstance(streams, dict):
        streams = {}
    result: dict[str, Any] = {}
    for sid, raw in streams.items():
        if not isinstance(raw, dict):
            continue
        entry = json.loads(json.dumps(raw))
        pending = entry.get("pending")
        entry["issue_lag"] = max(0, int(entry.get("last_issued_cursor") or 0) - int(entry.get("last_acked_cursor") or 0))
        issue_at = entry.get("last_issue_at")
        ack_at = entry.get("last_ack_at")
        dup_at = entry.get("last_duplicate_at")
        updated_at = entry.get("updated_at")
        entry["last_issue_ago_s"] = round(max(0.0, now - float(issue_at)), 3) if isinstance(issue_at, (int, float)) and issue_at else None
        entry["last_ack_ago_s"] = round(max(0.0, now - float(ack_at)), 3) if isinstance(ack_at, (int, float)) and ack_at else None
        entry["last_duplicate_ago_s"] = round(max(0.0, now - float(dup_at)), 3) if isinstance(dup_at, (int, float)) and dup_at else None
        entry["updated_ago_s"] = round(max(0.0, now - float(updated_at)), 3) if isinstance(updated_at, (int, float)) and updated_at else None
        if isinstance(pending, dict):
            send_at = pending.get("last_send_at")
            issued_at = pending.get("issued_at")
            pending["age_s"] = round(max(0.0, now - float(send_at)), 3) if isinstance(send_at, (int, float)) and send_at else None
            pending["issued_ago_s"] = round(max(0.0, now - float(issued_at)), 3) if isinstance(issued_at, (int, float)) and issued_at else None
        result[str(sid)] = entry
    return {
        "streams": result,
        "updated_at": state.get("updated_at"),
        "updated_ago_s": round(max(0.0, now - float(state.get("updated_at"))), 3)
        if isinstance(state.get("updated_at"), (int, float)) and state.get("updated_at")
        else None,
    }


__all__ = [
    "ack_stream_message",
    "prepare_stream_message",
    "protocol_streams_snapshot",
]
