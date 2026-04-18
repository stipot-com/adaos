from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import y_py as Y

from adaos.services.agent_context import get_ctx
from adaos.domain import Event as DomainEvent
from adaos.services.eventbus import emit as bus_emit

_log = logging.getLogger("adaos.weather.observer")

_YDOC_OBSERVERS: Dict[str, Tuple[int, int]] = {}
_YDOC_LOOPS: Dict[str, asyncio.AbstractEventLoop | None] = {}
_PENDING_DOC_CHECKS: Dict[str, bool] = {}
_LAST_CITY_IN_DOC: Dict[str, Optional[str]] = {}
_LAST_DOC_CHECK_AT: Dict[str, float] = {}
_OBSERVER_STATS: Dict[str, Dict[str, Any]] = {}


def _stats_entry(webspace_id: str) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    stats = _OBSERVER_STATS.get(key)
    if stats is not None:
        return stats
    stats = {
        "attach_total": 0,
        "callback_total": 0,
        "scheduled_total": 0,
        "inline_total": 0,
        "throttled_total": 0,
        "pending_skip_total": 0,
        "emit_check_total": 0,
        "emit_total": 0,
        "no_city_total": 0,
        "same_city_skip_total": 0,
        "loop_missing_total": 0,
        "loop_schedule_failed_total": 0,
        "error_total": 0,
        "last_attach_at": None,
        "last_callback_at": None,
        "last_check_at": None,
        "last_emit_at": None,
        "last_city": None,
    }
    _OBSERVER_STATS[key] = stats
    return stats


def weather_observer_snapshot(*, webspace_id: str | None = None) -> dict[str, Any]:
    selected_key = str(webspace_id or "").strip() or None
    keys: set[str] = set(_OBSERVER_STATS.keys()) | set(_YDOC_OBSERVERS.keys()) | set(_PENDING_DOC_CHECKS.keys())
    if selected_key:
        keys.add(selected_key)
    details: dict[str, Any] = {}
    pending_total = 0
    active_total = 0
    for key in sorted(keys):
        if selected_key and key != selected_key:
            continue
        attached = _YDOC_OBSERVERS.get(key)
        observer_loop = _YDOC_LOOPS.get(key)
        stats = dict(_stats_entry(key))
        pending = bool(_PENDING_DOC_CHECKS.get(key))
        pending_total += 1 if pending else 0
        active_total += 1 if attached is not None else 0
        details[key] = {
            "webspace_id": key,
            "active": bool(attached is not None),
            "ydoc_id": int(attached[0]) if attached is not None else None,
            "sub_id": int(attached[1]) if attached is not None else None,
            "loop_bound": bool(observer_loop is not None and not observer_loop.is_closed()),
            "pending": pending,
            **stats,
        }
    return {
        "active_observer_total": active_total,
        "pending_emit_total": pending_total,
        "webspaces": details,
        "selected": dict(details.get(selected_key) or {}) if selected_key else {},
    }


def _coerce_weather_mapping(value) -> dict:
    def _normalize(node):
        if isinstance(node, dict):
            return {str(k): _normalize(v) for k, v in node.items()}
        if isinstance(node, Y.YMap):
            keys = list(node.keys())
            return {str(k): _normalize(node.get(k)) for k in keys}
        if isinstance(node, Y.YArray):
            return [_normalize(it) for it in node]
        if node is None:
            return None
        return node

    if value is None:
        return {}

    try:
        return _normalize(value) or {}
    except Exception:
        pass

    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            json_value = to_json()
            return _normalize(json_value) or {}
        except Exception:
            pass
    return {}


def _current_city_from_doc(ydoc) -> Optional[str]:
    data = ydoc.get_map("data")
    weather = data.get("weather")
    mapping = _coerce_weather_mapping(weather)
    current = mapping.get("current") or {}
    if isinstance(current, dict):
        city = current.get("city")
        return str(city) if city else None
    return None


def _detach_after_transaction_observer(ydoc, *, sub_id: int | None, callback) -> bool:
    method = getattr(ydoc, "unobserve_after_transaction", None)
    if callable(method):
        for args in ((sub_id,), (callback,), (sub_id, callback)):
            try:
                method(*args)
                return True
            except TypeError:
                continue
            except Exception:
                return False
    fallback = getattr(ydoc, "unobserve", None)
    if callable(fallback):
        for args in ((sub_id,), (callback,), (sub_id, callback)):
            try:
                fallback(*args)
                return True
            except TypeError:
                continue
            except Exception:
                return False
    return False


def _ensure_city_observer(webspace_id: str, ydoc):
    key = str(webspace_id or "").strip() or "default"
    ydoc_id = id(ydoc)
    attached = _YDOC_OBSERVERS.get(key)
    if attached is not None and attached[0] == ydoc_id:
        return
    try:
        observer_loop = asyncio.get_running_loop()
    except RuntimeError:
        observer_loop = None
    _YDOC_LOOPS[key] = observer_loop
    stats = _stats_entry(key)
    stats["attach_total"] = int(stats.get("attach_total") or 0) + 1
    stats["last_attach_at"] = time.time()

    def _emit_event(city: str) -> None:
        try:
            ctx = get_ctx()
            ev = DomainEvent(
                type="weather.city_changed",
                payload={"webspace_id": key, "workspace_id": key, "city": city},
                source="weather_observer",
                ts=time.time(),
            )
            ctx.bus.publish(ev)
        except Exception:
            try:
                bus_emit(ctx.bus, "weather.city_changed", {"webspace_id": key, "city": city}, "weather_observer")
            except Exception:
                pass

    def _emit_current() -> None:
        stats["emit_check_total"] = int(stats.get("emit_check_total") or 0) + 1
        stats["last_check_at"] = time.time()
        city = _current_city_from_doc(ydoc)
        _log.debug("weather observer check webspace=%s city=%s", key, city)
        if not city:
            stats["no_city_total"] = int(stats.get("no_city_total") or 0) + 1
            return
        if _LAST_CITY_IN_DOC.get(key) == city:
            stats["same_city_skip_total"] = int(stats.get("same_city_skip_total") or 0) + 1
            return
        _LAST_CITY_IN_DOC[key] = city
        stats["emit_total"] = int(stats.get("emit_total") or 0) + 1
        stats["last_emit_at"] = time.time()
        stats["last_city"] = city
        _emit_event(city)

    def _maybe_emit(event=None) -> None:  # noqa: ARG001
        stats["callback_total"] = int(stats.get("callback_total") or 0) + 1
        stats["last_callback_at"] = time.time()
        now = time.monotonic()
        last = _LAST_DOC_CHECK_AT.get(key)
        if last is not None and (now - last) < 0.5:
            stats["throttled_total"] = int(stats.get("throttled_total") or 0) + 1
            return
        if _PENDING_DOC_CHECKS.get(key):
            stats["pending_skip_total"] = int(stats.get("pending_skip_total") or 0) + 1
            return
        _LAST_DOC_CHECK_AT[key] = now
        _PENDING_DOC_CHECKS[key] = True

        def _run_safe() -> None:
            try:
                _emit_current()
            except Exception:
                stats["error_total"] = int(stats.get("error_total") or 0) + 1
                _log.debug("weather observer callback failed webspace=%s", key, exc_info=True)
            finally:
                _PENDING_DOC_CHECKS.pop(key, None)

        target_loop = _YDOC_LOOPS.get(key)
        if target_loop is not None and not target_loop.is_closed():
            try:
                target_loop.call_soon_threadsafe(_run_safe)
                stats["scheduled_total"] = int(stats.get("scheduled_total") or 0) + 1
                return
            except RuntimeError:
                stats["loop_schedule_failed_total"] = int(stats.get("loop_schedule_failed_total") or 0) + 1
        else:
            stats["loop_missing_total"] = int(stats.get("loop_missing_total") or 0) + 1
        stats["inline_total"] = int(stats.get("inline_total") or 0) + 1
        _run_safe()

    sub_id = ydoc.observe_after_transaction(_maybe_emit)
    _YDOC_OBSERVERS[key] = (ydoc_id, sub_id)
    _emit_current()

    def _detach() -> None:
        try:
            _detach_after_transaction_observer(ydoc, sub_id=sub_id, callback=_maybe_emit)
        finally:
            forget_weather_room_observer(key, ydoc_id)

    return _detach


def _room_observer(webspace_id: str, ydoc):
    return _ensure_city_observer(webspace_id, ydoc)


try:
    from adaos.services.yjs.observers import register_room_observer

    register_room_observer(_room_observer)
except Exception:
    # Do not break boot if Yjs observers are not available.
    pass


__all__ = ["_room_observer", "weather_observer_snapshot"]


def forget_weather_room_observer(webspace_id: str, ydoc_id: int | None = None) -> None:
    key = str(webspace_id or "").strip() or "default"
    attached = _YDOC_OBSERVERS.get(key)
    if attached is not None:
        current_ydoc_id, _sub_id = attached
        if ydoc_id is None or current_ydoc_id == int(ydoc_id):
            _YDOC_OBSERVERS.pop(key, None)
            _YDOC_LOOPS.pop(key, None)
            _PENDING_DOC_CHECKS.pop(key, None)
            _LAST_CITY_IN_DOC.pop(key, None)
    _LAST_DOC_CHECK_AT.pop(key, None)

