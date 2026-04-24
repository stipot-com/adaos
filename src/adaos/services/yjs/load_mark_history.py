from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx


def _history_path() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.logs_dir()
    logs_dir = Path(raw() if callable(raw) else raw)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "yjs_load_mark.jsonl"


def history_path() -> str:
    return str(_history_path())


def append_history_snapshot(
    *,
    webspace_id: str,
    ts: float,
    rows: list[dict[str, Any]],
) -> None:
    path = _history_path()
    token = str(webspace_id or "").strip() or "default"
    with path.open("a", encoding="utf-8") as handle:
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            payload = {
                "ts": float(ts),
                "webspace_id": token,
                "kind": str(row.get("kind") or "").strip() or "unknown",
                "bucket_id": str(row.get("id") or row.get("display") or "").strip() or "unknown",
                "display": str(row.get("display") or row.get("id") or "").strip() or "unknown",
                "status": str(row.get("status") or "").strip() or "unknown",
                "byte_status": str(row.get("byte_status") or "").strip() or None,
                "write_status": str(row.get("write_status") or "").strip() or None,
                "avg_bps": float(row.get("avg_bps") or 0.0),
                "peak_bps": float(row.get("peak_bps") or 0.0),
                "avg_wps": float(row.get("avg_wps") or 0.0),
                "peak_wps": float(row.get("peak_wps") or 0.0),
                "recent_bytes": int(row.get("recent_bytes") or 0),
                "recent_writes": int(row.get("recent_writes") or 0),
                "lifetime_bytes": int(row.get("lifetime_bytes") or 0),
                "sample_total": int(row.get("sample_total") or 0),
                "write_total": int(row.get("write_total") or 0),
                "current_size_bytes": int(row.get("current_size_bytes") or 0),
                "last_source": str(row.get("last_source") or "").strip() or None,
                "last_changed_at": float(row.get("last_changed_at") or 0.0),
                "last_changed_ago_s": float(row.get("last_changed_ago_s") or 0.0),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _matches_filters(
    payload: dict[str, Any],
    *,
    webspace_id: str | None = None,
    kind: str | None = None,
    bucket_id: str | None = None,
    display_contains: str | None = None,
    status: str | None = None,
    last_source: str | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> bool:
    if webspace_id and str(payload.get("webspace_id") or "").strip() != str(webspace_id).strip():
        return False
    if kind and str(payload.get("kind") or "").strip() != str(kind).strip():
        return False
    if bucket_id and str(payload.get("bucket_id") or "").strip() != str(bucket_id).strip():
        return False
    if status and str(payload.get("status") or "").strip() != str(status).strip():
        return False
    if last_source and str(payload.get("last_source") or "").strip() != str(last_source).strip():
        return False
    if display_contains:
        haystack = str(payload.get("display") or "").strip().lower()
        needle = str(display_contains).strip().lower()
        if needle and needle not in haystack:
            return False
    ts = float(payload.get("ts") or 0.0)
    if since_ts is not None and ts < float(since_ts):
        return False
    if until_ts is not None and ts > float(until_ts):
        return False
    return True


def list_history_rows(
    *,
    limit: int = 100,
    webspace_id: str | None = None,
    kind: str | None = None,
    bucket_id: str | None = None,
    display_contains: str | None = None,
    status: str | None = None,
    last_source: str | None = None,
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> dict[str, Any]:
    path = _history_path()
    if not path.exists():
        return {"path": str(path), "count": 0, "items": []}
    max_items = max(1, min(int(limit), 2000))
    matches: deque[dict[str, Any]] = deque(maxlen=max_items)
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
                if not isinstance(payload, dict):
                    continue
                if _matches_filters(
                    payload,
                    webspace_id=webspace_id,
                    kind=kind,
                    bucket_id=bucket_id,
                    display_contains=display_contains,
                    status=status,
                    last_source=last_source,
                    since_ts=since_ts,
                    until_ts=until_ts,
                ):
                    matches.append(payload)
    except OSError:
        return {"path": str(path), "count": 0, "items": []}
    items = list(reversed(matches))
    return {
        "path": str(path),
        "count": len(items),
        "items": items,
    }


__all__ = [
    "append_history_snapshot",
    "history_path",
    "list_history_rows",
]
