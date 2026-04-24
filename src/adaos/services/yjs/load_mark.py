from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from adaos.sdk.core.decorators import subscribe
from adaos.sdk.io.out import stream_publish
from adaos.services.yjs.store import add_ystore_write_listener

_log = logging.getLogger("adaos.yjs.load_mark")

_ROOT_NAMES: tuple[str, ...] = ("ui", "data", "registry", "runtime", "devices")
_WINDOW_SEC = max(10, int(os.getenv("ADAOS_YJS_LOAD_MARK_WINDOW_SEC") or "60"))
_BUCKET_SEC = max(1, int(os.getenv("ADAOS_YJS_LOAD_MARK_BUCKET_SEC") or "1"))
_HIGH_BPS = max(1, int(os.getenv("ADAOS_YJS_LOAD_MARK_HIGH_BPS") or str(32 * 1024)))
_CRITICAL_BPS = max(_HIGH_BPS + 1, int(os.getenv("ADAOS_YJS_LOAD_MARK_CRITICAL_BPS") or str(128 * 1024)))
_UNATTRIBUTED_ROOT = str(os.getenv("ADAOS_YJS_LOAD_MARK_UNATTRIBUTED_ROOT") or "_by_initiator/unknown").strip() or "_by_initiator/unknown"
_UNATTRIBUTED_PREFIX = str(os.getenv("ADAOS_YJS_LOAD_MARK_UNATTRIBUTED_PREFIX") or "_by_initiator/").strip() or "_by_initiator/"
_OWNER_PREFIX = str(os.getenv("ADAOS_YJS_LOAD_MARK_OWNER_PREFIX") or "_by_owner/").strip() or "_by_owner/"
_UNKNOWN_OWNER = str(os.getenv("ADAOS_YJS_LOAD_MARK_UNKNOWN_OWNER") or f"{_OWNER_PREFIX}unknown").strip() or f"{_OWNER_PREFIX}unknown"
_STREAM_RECEIVER = str(os.getenv("ADAOS_YJS_LOAD_MARK_STREAM_RECEIVER") or "infrastate.yjs.load_mark").strip() or "infrastate.yjs.load_mark"
_STREAM_PUBLISH_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("ADAOS_YJS_LOAD_MARK_STREAM_MIN_INTERVAL_SEC") or "0.25"))
_STREAM_TOP_N = max(0, int(os.getenv("ADAOS_YJS_LOAD_MARK_STREAM_TOP_N") or "0"))
_HIGH_WPS = max(1.0, float(os.getenv("ADAOS_YJS_LOAD_MARK_HIGH_WPS") or "8"))
_CRITICAL_WPS = max(_HIGH_WPS + 0.1, float(os.getenv("ADAOS_YJS_LOAD_MARK_CRITICAL_WPS") or "32"))
_OWNER_ALERT_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("ADAOS_YJS_LOAD_MARK_OWNER_ALERT_MIN_INTERVAL_SEC") or "15"))

_LOCK = threading.RLock()
_WEBSPACE_STATE: dict[str, dict[str, Any]] = {}
_ACTIVE_STREAM_SUBSCRIPTIONS: dict[str, int] = {}
_LAST_STREAM_PUBLISH_AT: dict[str, float] = {}
_OWNER_ALERTS: dict[str, float] = {}
_STREAM_TICK_INTERVAL_SEC = max(0.25, float(os.getenv("ADAOS_YJS_LOAD_MARK_STREAM_TICK_INTERVAL_SEC") or "1.0"))
_STREAM_TICKER_THREAD: threading.Thread | None = None
_STREAM_TICKER_STOP = threading.Event()


def _clone_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if isinstance(value, dict):
            return {str(key): _clone_json(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_clone_json(item) for item in value]
        if isinstance(value, tuple):
            return [_clone_json(item) for item in value]
        return value


def _json_size(value: Any) -> int:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        payload = json.dumps(_clone_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(payload.encode("utf-8"))


def _coerce_map_value(node: Any) -> dict[str, Any]:
    keys = getattr(node, "keys", None)
    getter = getattr(node, "get", None)
    if not callable(keys) or not callable(getter):
        return {}
    result: dict[str, Any] = {}
    try:
        for key in list(keys()):
            token = str(key or "").strip()
            if not token:
                continue
            try:
                result[token] = getter(key)
            except Exception:
                result[token] = None
    except Exception:
        return {}
    return result


def capture_ydoc_root_sizes(ydoc: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for root_name in _ROOT_NAMES:
        try:
            node = ydoc.get_map(root_name)
        except Exception:
            continue
        payload = _coerce_map_value(node)
        if not payload:
            continue
        try:
            result[root_name] = _json_size(payload)
        except Exception:
            continue
    return result


def _ensure_webspace_state(webspace_id: str) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    state = _WEBSPACE_STATE.get(key)
    if state is None:
        state = {
            "updated_at": 0.0,
            "tx_total": 0,
            "roots": {},
            "owners": {},
            "snapshot_sizes": {},
            "owner_sizes": {},
        }
        _WEBSPACE_STATE[key] = state
    return state


def _normalize_source_bucket(source: str | None) -> str:
    token = str(source or "").strip().lower()
    if not token:
        return _UNATTRIBUTED_ROOT
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in token).strip("._")
    if not safe:
        return _UNATTRIBUTED_ROOT
    return f"{_UNATTRIBUTED_PREFIX}{safe}"


def _normalize_owner_bucket(owner: str | None) -> str:
    token = str(owner or "").strip().lower()
    if not token:
        return _UNKNOWN_OWNER
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in token).strip("._")
    if not safe:
        return _UNKNOWN_OWNER
    return f"{_OWNER_PREFIX}{safe}"


def _has_active_stream_subscription_locked(webspace_id: str) -> bool:
    return int(_ACTIVE_STREAM_SUBSCRIPTIONS.get(str(webspace_id or "").strip() or "default") or 0) > 0


def _mark_stream_subscription(webspace_id: str, *, active: bool) -> None:
    key = str(webspace_id or "").strip() or "default"
    with _LOCK:
        current = int(_ACTIVE_STREAM_SUBSCRIPTIONS.get(key) or 0)
        next_value = current + 1 if active else max(0, current - 1)
        if next_value > 0:
            _ACTIVE_STREAM_SUBSCRIPTIONS[key] = next_value
        else:
            _ACTIVE_STREAM_SUBSCRIPTIONS.pop(key, None)
            _LAST_STREAM_PUBLISH_AT.pop(key, None)
    if active:
        _ensure_stream_ticker_running()


def _zero_stale_row_metrics(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["avg_bps"] = 0.0
    normalized["peak_bps"] = 0.0
    normalized["avg_wps"] = 0.0
    normalized["peak_wps"] = 0.0
    normalized["recent_bytes"] = 0
    normalized["recent_writes"] = 0
    normalized["status"] = "idle"
    normalized["byte_status"] = "idle"
    normalized["write_status"] = "idle"
    return normalized


def _stream_payload_items_locked(webspace_id: str, *, now_ts: float, last_published_at: float = 0.0) -> list[dict[str, Any]]:
    state = _ensure_webspace_state(webspace_id)
    snapshot = _snapshot_webspace_locked(str(webspace_id or "").strip() or "default", state, now_ts=now_ts)
    rows: list[dict[str, Any]] = []
    for item in list(snapshot.get("owner_items") or []):
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "").strip()
        row = dict(item)
        if last_published_at > 0.0 and float(row.get("last_changed_at") or 0.0) <= last_published_at:
            row = _zero_stale_row_metrics(row)
        row["kind"] = "owner"
        row["id"] = owner or "unknown"
        row["display"] = owner or "unknown"
        rows.append(row)
    for item in list(snapshot.get("items") or []):
        if not isinstance(item, dict):
            continue
        root = str(item.get("root") or "").strip()
        row = dict(item)
        if last_published_at > 0.0 and float(row.get("last_changed_at") or 0.0) <= last_published_at:
            row = _zero_stale_row_metrics(row)
        row["kind"] = "root"
        row["id"] = root or "unknown"
        row["display"] = root or "unknown"
        rows.append(row)
    rows.sort(
        key=lambda entry: (
            0 if str(entry.get("kind") or "") == "owner" else 1,
            -float(entry.get("peak_bps") or 0.0),
            -float(entry.get("peak_wps") or 0.0),
            -float(entry.get("avg_bps") or 0.0),
            str(entry.get("display") or ""),
        )
    )
    if _STREAM_TOP_N > 0:
        return rows[:_STREAM_TOP_N]
    return rows


def _maybe_publish_stream_update(webspace_id: str, *, now_ts: float | None = None) -> None:
    now = time.time() if now_ts is None else float(now_ts)
    key = str(webspace_id or "").strip() or "default"
    with _LOCK:
        if not _has_active_stream_subscription_locked(key):
            return
        last_published = float(_LAST_STREAM_PUBLISH_AT.get(key) or 0.0)
        if _STREAM_PUBLISH_MIN_INTERVAL_SEC > 0.0 and last_published > 0.0:
            if now - last_published < _STREAM_PUBLISH_MIN_INTERVAL_SEC:
                return
        payload = _stream_payload_items_locked(key, now_ts=now, last_published_at=last_published)
        _LAST_STREAM_PUBLISH_AT[key] = now
    try:
        stream_publish(
            _STREAM_RECEIVER,
            payload,
            _meta={"webspace_id": key},
            ts=now,
        )
    except Exception:
        _log.debug("failed to publish load_mark stream update webspace=%s", key, exc_info=True)


def _stream_ticker_loop() -> None:
    while not _STREAM_TICKER_STOP.wait(_STREAM_TICK_INTERVAL_SEC):
        with _LOCK:
            keys = [key for key, count in _ACTIVE_STREAM_SUBSCRIPTIONS.items() if int(count or 0) > 0]
        for key in keys:
            try:
                _maybe_publish_stream_update(key, now_ts=time.time())
            except Exception:
                _log.debug("load_mark ticker publish failed webspace=%s", key, exc_info=True)


def _ensure_stream_ticker_running() -> None:
    global _STREAM_TICKER_THREAD
    with _LOCK:
        if _STREAM_TICKER_THREAD is not None and _STREAM_TICKER_THREAD.is_alive():
            return
        _STREAM_TICKER_STOP.clear()
        thread = threading.Thread(target=_stream_ticker_loop, name="adaos-yjs-load-mark-stream", daemon=True)
        _STREAM_TICKER_THREAD = thread
        thread.start()


def _ensure_bucket_state(container: dict[str, Any], bucket_name: str) -> dict[str, Any]:
    entry = container.get(bucket_name)
    if entry is None:
        entry = {
            "recent": {},
            "recent_bytes": 0,
            "recent_writes": {},
            "recent_write_total": 0,
            "lifetime_bytes": 0,
            "sample_total": 0,
            "updated_at": 0.0,
            "last_changed_at": 0.0,
            "current_size_bytes": 0,
            "last_source": None,
        }
        container[bucket_name] = entry
    return entry


def _prune_bucket_locked(bucket_state: dict[str, Any], *, now_ts: float) -> None:
    recent = bucket_state.get("recent")
    if not isinstance(recent, dict):
        bucket_state["recent"] = {}
        bucket_state["recent_bytes"] = 0
        recent = {}
    cutoff_bucket = int(now_ts // _BUCKET_SEC) - int(_WINDOW_SEC // _BUCKET_SEC) - 1
    removed = 0
    for raw_bucket in list(recent.keys()):
        try:
            bucket = int(raw_bucket)
        except Exception:
            recent.pop(raw_bucket, None)
            continue
        if bucket <= cutoff_bucket:
            try:
                removed += int(recent.pop(raw_bucket, 0) or 0)
            except Exception:
                recent.pop(raw_bucket, None)
    bucket_state["recent_bytes"] = max(0, int(bucket_state.get("recent_bytes") or 0) - removed)
    recent_writes = bucket_state.get("recent_writes")
    if not isinstance(recent_writes, dict):
        bucket_state["recent_writes"] = {}
        bucket_state["recent_write_total"] = 0
        return
    removed_writes = 0
    for raw_bucket in list(recent_writes.keys()):
        try:
            bucket = int(raw_bucket)
        except Exception:
            recent_writes.pop(raw_bucket, None)
            continue
        if bucket <= cutoff_bucket:
            try:
                removed_writes += int(recent_writes.pop(raw_bucket, 0) or 0)
            except Exception:
                recent_writes.pop(raw_bucket, None)
    bucket_state["recent_write_total"] = max(0, int(bucket_state.get("recent_write_total") or 0) - removed_writes)


def _record_bucket_bytes_locked(
    container: dict[str, Any],
    *,
    bucket_name: str,
    bytes_written: int,
    now_ts: float,
    current_size_bytes: int,
    source: str | None,
) -> None:
    if bytes_written <= 0:
        return
    bucket_state = _ensure_bucket_state(container, bucket_name)
    _prune_bucket_locked(bucket_state, now_ts=now_ts)
    bucket = int(now_ts // _BUCKET_SEC)
    recent = bucket_state.setdefault("recent", {})
    recent[bucket] = int(recent.get(bucket) or 0) + int(bytes_written)
    recent_writes = bucket_state.setdefault("recent_writes", {})
    recent_writes[bucket] = int(recent_writes.get(bucket) or 0) + 1
    bucket_state["recent_bytes"] = int(bucket_state.get("recent_bytes") or 0) + int(bytes_written)
    bucket_state["recent_write_total"] = int(bucket_state.get("recent_write_total") or 0) + 1
    bucket_state["lifetime_bytes"] = int(bucket_state.get("lifetime_bytes") or 0) + int(bytes_written)
    bucket_state["sample_total"] = int(bucket_state.get("sample_total") or 0) + 1
    bucket_state["updated_at"] = float(now_ts)
    bucket_state["last_changed_at"] = float(now_ts)
    bucket_state["current_size_bytes"] = int(current_size_bytes)
    bucket_state["last_source"] = str(source or "").strip() or None


def _record_webspace_activity_locked(
    webspace_state: dict[str, Any],
    *,
    now_ts: float,
) -> None:
    webspace_state["updated_at"] = float(now_ts)
    webspace_state["tx_total"] = int(webspace_state.get("tx_total") or 0) + 1


def _maybe_log_owner_pressure(
    webspace_id: str,
    *,
    owner_bucket: str,
    owner_state: dict[str, Any],
    now_ts: float,
) -> None:
    _prune_bucket_locked(owner_state, now_ts=now_ts)
    recent = owner_state.get("recent")
    recent_writes = owner_state.get("recent_writes")
    if not isinstance(recent, dict):
        recent = {}
    if not isinstance(recent_writes, dict):
        recent_writes = {}
    peak_bucket_bytes = max((int(value or 0) for value in recent.values()), default=0)
    peak_bucket_writes = max((int(value or 0) for value in recent_writes.values()), default=0)
    avg_bps = round(float(owner_state.get("recent_bytes") or 0) / float(_WINDOW_SEC), 3)
    peak_bps = round(float(peak_bucket_bytes) / float(_BUCKET_SEC), 3)
    avg_wps = round(float(owner_state.get("recent_write_total") or 0) / float(_WINDOW_SEC), 3)
    peak_wps = round(float(peak_bucket_writes) / float(_BUCKET_SEC), 3)
    severity = None
    if peak_bps >= float(_CRITICAL_BPS) or avg_bps >= float(_CRITICAL_BPS) or peak_wps >= float(_CRITICAL_WPS) or avg_wps >= float(_CRITICAL_WPS):
        severity = "critical"
    elif peak_bps >= float(_HIGH_BPS) or avg_bps >= float(_HIGH_BPS) or peak_wps >= float(_HIGH_WPS) or avg_wps >= float(_HIGH_WPS):
        severity = "high"
    if not severity:
        return
    alert_key = f"{webspace_id}:{owner_bucket}:{severity}"
    last_at = float(_OWNER_ALERTS.get(alert_key) or 0.0)
    if _OWNER_ALERT_MIN_INTERVAL_SEC > 0.0 and last_at > 0.0 and now_ts - last_at < _OWNER_ALERT_MIN_INTERVAL_SEC:
        return
    _OWNER_ALERTS[alert_key] = now_ts
    logging.getLogger(f"adaos.yjs.owner.{owner_bucket.removeprefix(_OWNER_PREFIX)}").warning(
        "YJS owner flow above threshold webspace=%s owner=%s severity=%s avg_bps=%s peak_bps=%s avg_wps=%s peak_wps=%s recent_bytes=%s recent_writes=%s source=%s",
        webspace_id,
        owner_bucket,
        severity,
        avg_bps,
        peak_bps,
        avg_wps,
        peak_wps,
        int(owner_state.get("recent_bytes") or 0),
        int(owner_state.get("recent_write_total") or 0),
        owner_state.get("last_source"),
    )


def _distribute_bytes_by_delta(
    *,
    before_sizes: dict[str, int],
    after_sizes: dict[str, int],
    total_bytes: int,
) -> list[tuple[str, int, int]]:
    deltas: list[tuple[str, int, int]] = []
    for root_name in sorted(set(before_sizes) | set(after_sizes)):
        before = int(before_sizes.get(root_name) or 0)
        after = int(after_sizes.get(root_name) or 0)
        delta = abs(after - before)
        if delta <= 0:
            continue
        deltas.append((root_name, after, delta))
    if not deltas:
        return []
    if total_bytes <= 0:
        return [(root_name, after, delta) for root_name, after, delta in deltas]
    weight_total = sum(delta for _root_name, _after, delta in deltas)
    if weight_total <= 0:
        share = max(1, int(total_bytes / max(1, len(deltas))))
        assigned = 0
        result: list[tuple[str, int, int]] = []
        for index, (root_name, after, _delta) in enumerate(deltas):
            chunk = share if index < len(deltas) - 1 else max(1, total_bytes - assigned)
            assigned += chunk
            result.append((root_name, after, int(chunk)))
        return result
    remaining = int(total_bytes)
    result = []
    for index, (root_name, after, delta) in enumerate(deltas):
        if index >= len(deltas) - 1:
            chunk = max(1, remaining)
        else:
            chunk = max(1, int(round(float(total_bytes) * (float(delta) / float(weight_total)))))
            chunk = min(chunk, remaining)
        remaining -= chunk
        result.append((root_name, after, int(chunk)))
    return result


def record_root_flow(
    webspace_id: str,
    *,
    before_sizes: dict[str, int] | None,
    after_sizes: dict[str, int] | None,
    total_bytes: int,
    now_ts: float | None = None,
    source: str | None = None,
    owner: str | None = None,
) -> None:
    key = str(webspace_id or "").strip() or "default"
    before = {str(name): int(size or 0) for name, size in (before_sizes or {}).items() if str(name).strip()}
    after = {str(name): int(size or 0) for name, size in (after_sizes or {}).items() if str(name).strip()}
    now = time.time() if now_ts is None else float(now_ts)
    distributed = _distribute_bytes_by_delta(before_sizes=before, after_sizes=after, total_bytes=int(total_bytes or 0))
    with _LOCK:
        webspace_state = _ensure_webspace_state(key)
        for root_name, current_size, bytes_written in distributed:
            _record_bucket_bytes_locked(
                webspace_state.setdefault("roots", {}),
                bucket_name=root_name,
                bytes_written=bytes_written,
                now_ts=now,
                current_size_bytes=current_size,
                source=source,
            )
            owner_bucket = _normalize_owner_bucket(owner)
            owner_sizes = webspace_state.setdefault("owner_sizes", {})
            owner_previous = int(owner_sizes.get(owner_bucket) or 0)
            owner_current = max(owner_previous, owner_previous + bytes_written)
            owner_sizes[owner_bucket] = owner_current
            _record_bucket_bytes_locked(
                webspace_state.setdefault("owners", {}),
                bucket_name=owner_bucket,
                bytes_written=bytes_written,
                now_ts=now,
                current_size_bytes=owner_current,
                source=source,
            )
            _maybe_log_owner_pressure(
                key,
                owner_bucket=owner_bucket,
                owner_state=_ensure_bucket_state(webspace_state.setdefault("owners", {}), owner_bucket),
                now_ts=now,
            )
        _record_webspace_activity_locked(webspace_state, now_ts=now)
        webspace_state["snapshot_sizes"] = dict(after)
        webspace_state["updated_at"] = float(now)


def record_detached_ydoc_update(
    webspace_id: str,
    *,
    before_sizes: dict[str, int] | None,
    ydoc: Any,
    total_bytes: int,
    now_ts: float | None = None,
    source: str | None = None,
    owner: str | None = None,
) -> None:
    try:
        after_sizes = capture_ydoc_root_sizes(ydoc)
    except Exception:
        _log.debug("capture_ydoc_root_sizes failed for detached update webspace=%s", webspace_id, exc_info=True)
        return
    record_root_flow(
        webspace_id,
        before_sizes=before_sizes,
        after_sizes=after_sizes,
        total_bytes=int(total_bytes or 0),
        now_ts=now_ts,
        source=source or "detached_ydoc",
        owner=owner,
    )


def record_detached_root_update(
    webspace_id: str,
    *,
    root_names: list[str] | tuple[str, ...],
    total_bytes: int,
    now_ts: float | None = None,
    source: str | None = None,
    owner: str | None = None,
) -> None:
    names = [str(name or "").strip() for name in root_names if str(name or "").strip()]
    if not names:
        return
    now = time.time() if now_ts is None else float(now_ts)
    bytes_total = max(0, int(total_bytes or 0))
    if bytes_total <= 0:
        return
    with _LOCK:
        webspace_state = _ensure_webspace_state(webspace_id)
        snapshot_sizes = (
            dict(webspace_state.get("snapshot_sizes"))
            if isinstance(webspace_state.get("snapshot_sizes"), dict)
            else {}
        )
        share = max(1, int(bytes_total / max(1, len(names))))
        assigned = 0
        for index, root_name in enumerate(names):
            bytes_written = share if index < len(names) - 1 else max(1, bytes_total - assigned)
            assigned += bytes_written
            previous_size = int(snapshot_sizes.get(root_name) or 0)
            current_size = max(previous_size, previous_size + bytes_written)
            snapshot_sizes[root_name] = current_size
            _record_bucket_bytes_locked(
                webspace_state.setdefault("roots", {}),
                bucket_name=root_name,
                bytes_written=bytes_written,
                now_ts=now,
                current_size_bytes=current_size,
                source=source or "detached_root",
            )
            owner_bucket = _normalize_owner_bucket(owner)
            owner_sizes = webspace_state.setdefault("owner_sizes", {})
            owner_previous = int(owner_sizes.get(owner_bucket) or 0)
            owner_current = max(owner_previous, owner_previous + bytes_written)
            owner_sizes[owner_bucket] = owner_current
            _record_bucket_bytes_locked(
                webspace_state.setdefault("owners", {}),
                bucket_name=owner_bucket,
                bytes_written=bytes_written,
                now_ts=now,
                current_size_bytes=owner_current,
                source=source or "detached_root",
            )
            _maybe_log_owner_pressure(
                str(webspace_id or "").strip() or "default",
                owner_bucket=owner_bucket,
                owner_state=_ensure_bucket_state(webspace_state.setdefault("owners", {}), owner_bucket),
                now_ts=now,
            )
        _record_webspace_activity_locked(webspace_state, now_ts=now)
        webspace_state["snapshot_sizes"] = snapshot_sizes
        webspace_state["updated_at"] = float(now)


def record_write_update(
    webspace_id: str,
    *,
    total_bytes: int,
    root_names: list[str] | tuple[str, ...] | None = None,
    now_ts: float | None = None,
    source: str | None = None,
    owner: str | None = None,
) -> None:
    names = [str(name or "").strip() for name in (root_names or ()) if str(name or "").strip()]
    if names:
        record_detached_root_update(
            webspace_id,
            root_names=names,
            total_bytes=total_bytes,
            now_ts=now_ts,
            source=source or "ystore_write",
            owner=owner,
        )
        _maybe_publish_stream_update(webspace_id, now_ts=now_ts)
        return

    now = time.time() if now_ts is None else float(now_ts)
    bytes_total = max(0, int(total_bytes or 0))
    if bytes_total <= 0:
        return
    with _LOCK:
        webspace_state = _ensure_webspace_state(webspace_id)
        snapshot_sizes = (
            dict(webspace_state.get("snapshot_sizes"))
            if isinstance(webspace_state.get("snapshot_sizes"), dict)
            else {}
        )
        bucket_name = _normalize_source_bucket(source)
        previous_size = int(snapshot_sizes.get(bucket_name) or 0)
        current_size = max(previous_size, previous_size + bytes_total)
        snapshot_sizes[bucket_name] = current_size
        _record_bucket_bytes_locked(
            webspace_state.setdefault("roots", {}),
            bucket_name=bucket_name,
            bytes_written=bytes_total,
            now_ts=now,
            current_size_bytes=current_size,
            source=source or "ystore_write",
        )
        owner_bucket = _normalize_owner_bucket(owner)
        owner_sizes = webspace_state.setdefault("owner_sizes", {})
        owner_previous = int(owner_sizes.get(owner_bucket) or 0)
        owner_current = max(owner_previous, owner_previous + bytes_total)
        owner_sizes[owner_bucket] = owner_current
        _record_bucket_bytes_locked(
            webspace_state.setdefault("owners", {}),
            bucket_name=owner_bucket,
            bytes_written=bytes_total,
            now_ts=now,
            current_size_bytes=owner_current,
            source=source or "ystore_write",
        )
        _maybe_log_owner_pressure(
            str(webspace_id or "").strip() or "default",
            owner_bucket=owner_bucket,
            owner_state=_ensure_bucket_state(webspace_state.setdefault("owners", {}), owner_bucket),
            now_ts=now,
        )
        _record_webspace_activity_locked(webspace_state, now_ts=now)
        webspace_state["snapshot_sizes"] = snapshot_sizes
        webspace_state["updated_at"] = float(now)
    _maybe_publish_stream_update(webspace_id, now_ts=now)


def record_live_room_activity(
    webspace_id: str,
    *,
    now_ts: float | None = None,
) -> None:
    key = str(webspace_id or "").strip() or "default"
    now = time.time() if now_ts is None else float(now_ts)
    with _LOCK:
        webspace_state = _ensure_webspace_state(key)
        _record_webspace_activity_locked(webspace_state, now_ts=now)


def _status_for_rate(avg_bps: float, peak_bps: float) -> str:
    if peak_bps >= float(_CRITICAL_BPS) or avg_bps >= float(_CRITICAL_BPS):
        return "critical"
    if peak_bps >= float(_HIGH_BPS) or avg_bps >= float(_HIGH_BPS):
        return "high"
    if peak_bps > 0.0 or avg_bps > 0.0:
        return "nominal"
    return "idle"


def _status_for_write_rate(avg_wps: float, peak_wps: float) -> str:
    if peak_wps >= float(_CRITICAL_WPS) or avg_wps >= float(_CRITICAL_WPS):
        return "critical"
    if peak_wps >= float(_HIGH_WPS) or avg_wps >= float(_HIGH_WPS):
        return "high"
    if peak_wps > 0.0 or avg_wps > 0.0:
        return "nominal"
    return "idle"


def _snapshot_bucket_collection_locked(collection: dict[str, Any], *, key_name: str, now_ts: float) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    overall_state = "idle"
    for bucket_name, bucket_state in sorted(collection.items()):
        if not isinstance(bucket_state, dict):
            continue
        _prune_bucket_locked(bucket_state, now_ts=now_ts)
        recent = bucket_state.get("recent")
        if not isinstance(recent, dict):
            recent = {}
        recent_writes = bucket_state.get("recent_writes")
        if not isinstance(recent_writes, dict):
            recent_writes = {}
        peak_bucket_bytes = max((int(value or 0) for value in recent.values()), default=0)
        peak_bucket_writes = max((int(value or 0) for value in recent_writes.values()), default=0)
        avg_bps = round(float(bucket_state.get("recent_bytes") or 0) / float(_WINDOW_SEC), 3)
        peak_bps = round(float(peak_bucket_bytes) / float(_BUCKET_SEC), 3)
        avg_wps = round(float(bucket_state.get("recent_write_total") or 0) / float(_WINDOW_SEC), 3)
        peak_wps = round(float(peak_bucket_writes) / float(_BUCKET_SEC), 3)
        byte_status = _status_for_rate(avg_bps, peak_bps)
        write_status = _status_for_write_rate(avg_wps, peak_wps)
        status = "critical" if "critical" in {byte_status, write_status} else "high" if "high" in {byte_status, write_status} else "nominal" if "nominal" in {byte_status, write_status} else "idle"
        if status == "critical":
            overall_state = "critical"
        elif status == "high" and overall_state != "critical":
            overall_state = "high"
        elif status == "nominal" and overall_state not in {"critical", "high"}:
            overall_state = "nominal"
        items.append(
            {
                key_name: bucket_name,
                "avg_bps": avg_bps,
                "peak_bps": peak_bps,
                "avg_wps": avg_wps,
                "peak_wps": peak_wps,
                "recent_bytes": int(bucket_state.get("recent_bytes") or 0),
                "recent_writes": int(bucket_state.get("recent_write_total") or 0),
                "lifetime_bytes": int(bucket_state.get("lifetime_bytes") or 0),
                "sample_total": int(bucket_state.get("sample_total") or 0),
                "write_total": int(bucket_state.get("sample_total") or 0),
                "current_size_bytes": int(bucket_state.get("current_size_bytes") or 0),
                "status": status,
                "byte_status": byte_status,
                "write_status": write_status,
                "last_source": bucket_state.get("last_source"),
                "last_changed_at": bucket_state.get("last_changed_at") or None,
                "last_changed_ago_s": round(max(0.0, now_ts - float(bucket_state.get("last_changed_at") or 0.0)), 3)
                if float(bucket_state.get("last_changed_at") or 0.0) > 0.0
                else None,
            }
        )
    items.sort(key=lambda entry: (-float(entry.get("peak_bps") or 0.0), -float(entry.get("peak_wps") or 0.0), -float(entry.get("avg_bps") or 0.0), str(entry.get(key_name) or "")))
    return items, overall_state


def _snapshot_webspace_locked(key: str, webspace_state: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    roots = webspace_state.get("roots")
    if not isinstance(roots, dict):
        roots = {}
    owners = webspace_state.get("owners")
    if not isinstance(owners, dict):
        owners = {}
    items, overall_state = _snapshot_bucket_collection_locked(roots, key_name="root", now_ts=now_ts)
    owner_items, owner_state = _snapshot_bucket_collection_locked(owners, key_name="owner", now_ts=now_ts)
    if owner_state == "critical":
        overall_state = "critical"
    elif owner_state == "high" and overall_state != "critical":
        overall_state = "high"
    elif owner_state == "nominal" and overall_state not in {"critical", "high"}:
        overall_state = "nominal"
    return {
        "webspace_id": key,
        "window_sec": int(_WINDOW_SEC),
        "bucket_sec": int(_BUCKET_SEC),
        "thresholds": {
            "high_bps": int(_HIGH_BPS),
            "critical_bps": int(_CRITICAL_BPS),
        },
        "assessment": {
            "state": overall_state,
            "reason": (
                "recent_root_flow_above_critical_threshold"
                if overall_state == "critical"
                else "recent_root_flow_above_high_threshold"
                if overall_state == "high"
                else "recent_root_flow_detected"
                if overall_state == "nominal"
                else "no_recent_root_flow"
            ),
        },
        "updated_at": webspace_state.get("updated_at") or None,
        "updated_ago_s": round(max(0.0, now_ts - float(webspace_state.get("updated_at") or 0.0)), 3)
        if float(webspace_state.get("updated_at") or 0.0) > 0.0
        else None,
        "recent_bytes_total": int(sum(int(item.get("recent_bytes") or 0) for item in items)),
        "recent_writes_total": int(sum(int(item.get("recent_writes") or 0) for item in items)),
        "tx_total": int(webspace_state.get("tx_total") or 0),
        "root_total": len(items),
        "active_root_total": sum(1 for item in items if float(item.get("peak_bps") or 0.0) > 0.0),
        "owner_total": len(owner_items),
        "active_owner_total": sum(1 for item in owner_items if float(item.get("peak_bps") or 0.0) > 0.0 or float(item.get("peak_wps") or 0.0) > 0.0),
        "items": items,
        "roots": {str(item.get("root") or ""): dict(item) for item in items if str(item.get("root") or "").strip()},
        "owner_items": owner_items,
        "owners": {str(item.get("owner") or ""): dict(item) for item in owner_items if str(item.get("owner") or "").strip()},
    }


def yjs_load_mark_snapshot(*, webspace_id: str | None = None, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    selected_webspace_id = str(webspace_id or "").strip() or None
    with _LOCK:
        keys = [selected_webspace_id] if selected_webspace_id else sorted(_WEBSPACE_STATE)
        webspaces: dict[str, Any] = {}
        for key in keys:
            state = _ensure_webspace_state(key)
            webspaces[key] = _snapshot_webspace_locked(key, state, now_ts=now)
    overall_state = "idle"
    active_root_total = 0
    for item in webspaces.values():
        if not isinstance(item, dict):
            continue
        active_root_total += int(item.get("active_root_total") or 0)
        state = str((item.get("assessment") or {}).get("state") or "idle")
        if state == "critical":
            overall_state = "critical"
        elif state == "high" and overall_state != "critical":
            overall_state = "high"
        elif state == "nominal" and overall_state not in {"critical", "high"}:
            overall_state = "nominal"
    selected = {}
    if selected_webspace_id:
        selected = webspaces.get(selected_webspace_id) if isinstance(webspaces.get(selected_webspace_id), dict) else {}
    elif webspaces:
        selected_webspace_id = sorted(webspaces)[0]
        selected = webspaces.get(selected_webspace_id) if isinstance(webspaces.get(selected_webspace_id), dict) else {}
    return {
        "window_sec": int(_WINDOW_SEC),
        "bucket_sec": int(_BUCKET_SEC),
        "thresholds": {
            "high_bps": int(_HIGH_BPS),
            "critical_bps": int(_CRITICAL_BPS),
            "high_wps": float(_HIGH_WPS),
            "critical_wps": float(_CRITICAL_WPS),
        },
        "assessment": {
            "state": overall_state,
            "reason": (
                "selected_or_cached_webspaces_above_critical_threshold"
                if overall_state == "critical"
                else "selected_or_cached_webspaces_above_high_threshold"
                if overall_state == "high"
                else "recent_root_flow_detected"
                if overall_state == "nominal"
                else "no_recent_root_flow"
            ),
        },
        "selected_webspace_id": selected_webspace_id,
        "selected_webspace": dict(selected) if isinstance(selected, dict) else {},
        "webspace_total": len(webspaces),
        "active_root_total": active_root_total,
        "active_owner_total": sum(int((item or {}).get("active_owner_total") or 0) for item in webspaces.values() if isinstance(item, dict)),
        "webspaces": {str(key): dict(value) for key, value in webspaces.items() if isinstance(value, dict)},
    }


def _load_mark_write_listener(webspace_id: str, update: bytes, meta: dict[str, Any] | None = None) -> None:
    payload = bytes(update or b"")
    if not payload:
        return
    metadata = dict(meta or {})
    root_names = metadata.get("root_names")
    if not isinstance(root_names, (list, tuple)):
        root_names = None
    record_write_update(
        webspace_id,
        total_bytes=len(payload),
        root_names=root_names,
        now_ts=time.time(),
        source=str(metadata.get("source") or "ystore_write"),
        owner=str(metadata.get("owner") or "").strip() or None,
    )


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _STREAM_RECEIVER:
        return
    webspace_id = str(payload.get("webspace_id") or "").strip() or "default"
    action = str(payload.get("action") or "").strip().lower()
    if action == "subscribed":
        _mark_stream_subscription(webspace_id, active=True)
    elif action == "unsubscribed":
        _mark_stream_subscription(webspace_id, active=False)


try:
    add_ystore_write_listener(_load_mark_write_listener)
except Exception:
    pass


__all__ = [
    "capture_ydoc_root_sizes",
    "record_detached_root_update",
    "record_detached_ydoc_update",
    "record_live_room_activity",
    "record_write_update",
    "record_root_flow",
    "yjs_load_mark_snapshot",
]
