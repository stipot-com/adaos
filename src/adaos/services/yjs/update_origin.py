from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from threading import RLock
from typing import Any


def _env_float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except Exception:
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except Exception:
        value = default
    return max(minimum, value)


_PENDING_TTL_SEC = _env_float("ADAOS_YJS_BACKEND_ROOM_UPDATE_SKIP_TTL_S", 30.0, minimum=1.0)
_PENDING_MAX = _env_int("ADAOS_YJS_BACKEND_ROOM_UPDATE_SKIP_MAX", 2048, minimum=16)
_LOCK = RLock()
_PENDING: OrderedDict[tuple[str, int, str], dict[str, Any]] = OrderedDict()


def _webspace_token(webspace_id: str) -> str:
    return str(webspace_id or "").strip() or "default"


def _fingerprint(update: bytes | bytearray | memoryview) -> tuple[int, str]:
    payload = bytes(update or b"")
    return len(payload), hashlib.sha1(payload).hexdigest()


def _prune_locked(now: float) -> None:
    cutoff = now - _PENDING_TTL_SEC
    stale: list[tuple[str, int, str]] = []
    for key, item in _PENDING.items():
        try:
            marked_at = float(item.get("marked_at") or 0.0)
        except Exception:
            marked_at = 0.0
        if marked_at >= cutoff:
            break
        stale.append(key)
    for key in stale:
        _PENDING.pop(key, None)
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.popitem(last=False)


def mark_backend_room_update(
    webspace_id: str,
    update: bytes | bytearray | memoryview | None,
    *,
    source: str = "yjs.doc.room_update",
    owner: str | None = None,
    channel: str | None = None,
) -> None:
    """
    Mark a live-room update that was already persisted before being fanout-applied.

    Detached backend mutations first write their diff to YStore, then apply that
    diff to the active YRoom so browsers receive it immediately. The YRoom also
    persists every update it broadcasts. This short-lived, exact-update marker
    lets the YRoom skip only that duplicate persistence while preserving client
    fanout and normal browser-originated durability.
    """
    if not update:
        return
    now = time.monotonic()
    key = (_webspace_token(webspace_id), *_fingerprint(update))
    with _LOCK:
        _prune_locked(now)
        existing = _PENDING.get(key)
        if existing is None:
            existing = {
                "webspace_id": key[0],
                "bytes": key[1],
                "sha1": key[2],
                "marked_at": now,
                "source": str(source or "").strip() or "yjs.doc.room_update",
                "owner": str(owner or "").strip() or None,
                "channel": str(channel or "").strip() or None,
                "count": 0,
            }
        existing["marked_at"] = now
        existing["count"] = int(existing.get("count") or 0) + 1
        _PENDING[key] = existing
        _PENDING.move_to_end(key)


def consume_backend_room_update(
    webspace_id: str,
    update: bytes | bytearray | memoryview | None,
) -> dict[str, Any] | None:
    """Consume one matching backend-origin marker for this exact live-room update."""
    if not update:
        return None
    now = time.monotonic()
    key = (_webspace_token(webspace_id), *_fingerprint(update))
    with _LOCK:
        _prune_locked(now)
        existing = _PENDING.get(key)
        if existing is None:
            return None
        count = int(existing.get("count") or 0)
        if count <= 1:
            _PENDING.pop(key, None)
        else:
            existing["count"] = count - 1
            _PENDING[key] = existing
            _PENDING.move_to_end(key)
        result = dict(existing)
        result["count"] = max(0, count - 1)
        return result


def pending_backend_room_update_count() -> int:
    now = time.monotonic()
    with _LOCK:
        _prune_locked(now)
        return len(_PENDING)


def reset_backend_room_update_markers() -> None:
    with _LOCK:
        _PENDING.clear()


__all__ = [
    "consume_backend_room_update",
    "mark_backend_room_update",
    "pending_backend_room_update_count",
    "reset_backend_room_update_markers",
]
