from __future__ import annotations

from collections import deque
import time
from typing import Any, Dict, Tuple

import y_py as Y

from adaos.services.yjs.observers import register_room_observer

_TXN_OBSERVERS: Dict[str, Tuple[int, int]] = {}
_TXN_HISTORY: Dict[str, deque[float]] = {}
_TXN_STATS: Dict[str, Dict[str, Any]] = {}


def _stats_entry(webspace_id: str) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    stats = _TXN_STATS.get(key)
    if stats is not None:
        return stats
    stats = {
        "attach_total": 0,
        "txn_total": 0,
        "last_attach_at": None,
        "last_tx_at": None,
    }
    _TXN_STATS[key] = stats
    return stats


def yjs_txn_probe_snapshot(*, webspace_id: str | None = None) -> dict[str, Any]:
    selected_key = str(webspace_id or "").strip() or None
    keys: set[str] = set(_TXN_STATS.keys()) | set(_TXN_OBSERVERS.keys()) | set(_TXN_HISTORY.keys())
    if selected_key:
        keys.add(selected_key)
    now = time.time()
    details: dict[str, Any] = {}
    active_total = 0
    for key in sorted(keys):
        if selected_key and key != selected_key:
            continue
        attached = _TXN_OBSERVERS.get(key)
        stats = dict(_stats_entry(key))
        history = _TXN_HISTORY.get(key) or deque()
        recent_txn_10s = sum(1 for stamp in history if stamp >= now - 10.0)
        recent_txn_60s = sum(1 for stamp in history if stamp >= now - 60.0)
        active_total += 1 if attached is not None else 0
        details[key] = {
            "webspace_id": key,
            "active": bool(attached is not None),
            "ydoc_id": int(attached[0]) if attached is not None else None,
            "sub_id": int(attached[1]) if attached is not None else None,
            "recent_txn_10s": recent_txn_10s,
            "recent_txn_60s": recent_txn_60s,
            "last_tx_ago_s": (
                round(max(0.0, now - float(stats["last_tx_at"])), 3)
                if isinstance(stats.get("last_tx_at"), (int, float))
                else None
            ),
            **stats,
        }
    return {
        "active_probe_total": active_total,
        "webspaces": details,
        "selected": dict(details.get(selected_key) or {}) if selected_key else {},
    }


def forget_yjs_txn_probe(webspace_id: str, ydoc_id: int | None = None) -> None:
    key = str(webspace_id or "").strip() or "default"
    attached = _TXN_OBSERVERS.get(key)
    if attached is None:
        return
    current_ydoc_id, _sub_id = attached
    if ydoc_id is not None and current_ydoc_id != int(ydoc_id):
        return
    _TXN_OBSERVERS.pop(key, None)


def _ensure_txn_probe(webspace_id: str, ydoc: Y.YDoc) -> None:
    key = str(webspace_id or "").strip() or "default"
    ydoc_id = id(ydoc)
    attached = _TXN_OBSERVERS.get(key)
    if attached is not None and attached[0] == ydoc_id:
        return
    stats = _stats_entry(key)
    stats["attach_total"] = int(stats.get("attach_total") or 0) + 1
    stats["last_attach_at"] = time.time()

    def _on_txn(_event=None) -> None:
        current = _TXN_OBSERVERS.get(key)
        if current is None or current[0] != ydoc_id:
            return
        now = time.time()
        entry = _stats_entry(key)
        entry["txn_total"] = int(entry.get("txn_total") or 0) + 1
        entry["last_tx_at"] = now
        history = _TXN_HISTORY.setdefault(key, deque(maxlen=512))
        history.append(now)

    sub_id = ydoc.observe_after_transaction(_on_txn)
    try:
        sub_id_int = int(sub_id)
    except Exception:
        sub_id_int = 0
    _TXN_OBSERVERS[key] = (ydoc_id, sub_id_int)


try:
    register_room_observer(_ensure_txn_probe)
except Exception:
    pass


__all__ = [
    "forget_yjs_txn_probe",
    "yjs_txn_probe_snapshot",
]
