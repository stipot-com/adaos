from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx

_LOCK = threading.RLock()


def _base_state_dir() -> Path:
    try:
        ctx = get_ctx()
        raw = ctx.paths.state_dir()
        raw = raw() if callable(raw) else raw
        return Path(raw).expanduser().resolve()
    except Exception:
        pass
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve() / "state"
    return (Path.home() / ".adaos" / "state").resolve()


def _outbox_root() -> Path:
    root = _base_state_dir() / "hub_root_outboxes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def outbox_store_path(name: str) -> Path:
    key = str(name or "").strip().lower() or "default"
    return _outbox_root() / f"{key}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_outbox_items(name: str) -> deque[tuple[str, bytes, dict[str, Any] | None]]:
    path = outbox_store_path(name)
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return deque()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return deque()
    result: deque[tuple[str, bytes, dict[str, Any] | None]] = deque()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject") or "").strip()
        data_b64 = raw.get("data_b64")
        if not subject or not isinstance(data_b64, str):
            continue
        try:
            data = base64.b64decode(data_b64.encode("ascii"))
        except Exception:
            continue
        meta = raw.get("meta")
        result.append((subject, data, dict(meta) if isinstance(meta, dict) else None))
    return result


def save_outbox_items(name: str, items: Any) -> int:
    path = outbox_store_path(name)
    serialized: list[dict[str, Any]] = []
    for item in list(items or []):
        if not isinstance(item, tuple):
            continue
        if len(item) < 2:
            continue
        subject = str(item[0] or "").strip()
        data = bytes(item[1] or b"")
        meta = item[2] if len(item) >= 3 and isinstance(item[2], dict) else None
        if not subject:
            continue
        serialized.append(
            {
                "subject": subject,
                "data_b64": base64.b64encode(data).decode("ascii"),
                "meta": dict(meta) if isinstance(meta, dict) else None,
            }
        )
    payload = {"items": serialized, "updated_at": time.time()}
    with _LOCK:
        _write_json(path, payload)
    return len(serialized)


__all__ = [
    "load_outbox_items",
    "outbox_store_path",
    "save_outbox_items",
]
