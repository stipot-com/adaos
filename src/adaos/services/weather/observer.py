from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Optional

import y_py as Y

from adaos.services.agent_context import get_ctx
from adaos.services.yjs.doc import async_get_ydoc
from adaos.domain import Event as DomainEvent
from adaos.services.eventbus import emit as bus_emit

_log = logging.getLogger("adaos.weather.observer")

_YDOC_OBSERVERS: Dict[str, int] = {}
_LAST_CITY_IN_DOC: Dict[str, Optional[str]] = {}
_LAST_DOC_CHECK_AT: Dict[str, float] = {}


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


def _ensure_city_observer(webspace_id: str, ydoc) -> None:
    if webspace_id in _YDOC_OBSERVERS:
        return

    def _emit_event(city: str) -> None:
        try:
            ctx = get_ctx()
            ev = DomainEvent(
                type="weather.city_changed",
                payload={"webspace_id": webspace_id, "workspace_id": webspace_id, "city": city},
                source="weather_observer",
                ts=time.time(),
            )
            ctx.bus.publish(ev)
        except Exception:
            try:
                bus_emit(ctx.bus, "weather.city_changed", {"webspace_id": webspace_id, "city": city}, "weather_observer")
            except Exception:
                pass

    def _emit_current() -> None:
        city = _current_city_from_doc(ydoc)
        _log.debug("weather observer check webspace=%s city=%s", webspace_id, city)
        if not city:
            return
        if _LAST_CITY_IN_DOC.get(webspace_id) == city:
            return
        _LAST_CITY_IN_DOC[webspace_id] = city
        _emit_event(city)

    def _maybe_emit(event=None) -> None:  # noqa: ARG001
        now = time.time()
        last = _LAST_DOC_CHECK_AT.get(webspace_id)
        if last is not None and (now - last) < 0.5:
            return
        _LAST_DOC_CHECK_AT[webspace_id] = now

        def _run_safe() -> None:
            try:
                _emit_current()
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            import threading

            threading.Thread(
                target=_run_safe,
                name="weather-observer",
                daemon=True,
            ).start()
        else:
            loop.call_soon(_run_safe)

    sub_id = ydoc.observe_after_transaction(_maybe_emit)
    _YDOC_OBSERVERS[webspace_id] = sub_id
    _emit_current()


def _room_observer(webspace_id: str, ydoc) -> None:
    _ensure_city_observer(webspace_id, ydoc)


try:
    from adaos.services.yjs.observers import register_room_observer

    register_room_observer(_room_observer)
except Exception:
    # Do not break boot if Yjs observers are not available.
    pass


__all__ = ["_room_observer"]

