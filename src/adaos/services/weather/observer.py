from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Dict

import y_py as Y

from adaos.domain import Event
from adaos.services.agent_context import get_ctx

_log = logging.getLogger("adaos.weather.observer")
_OBSERVERS: Dict[str, int] = {}
_LAST_CITY: Dict[str, str | None] = {}


def _current_city(ydoc: Y.YDoc) -> str | None:
    data = ydoc.get_map("data")
    weather = data.get("weather")
    if isinstance(weather, dict):
        current = weather.get("current") or {}
        if isinstance(current, dict):
            city = current.get("city")
            return str(city) if city else None
    return None


def ensure_weather_observer(webspace_id: str, ydoc: Y.YDoc) -> None:
    if webspace_id in _OBSERVERS:
        return

    def _emit_current() -> None:
        city = _current_city(ydoc)
        if not city or _LAST_CITY.get(webspace_id) == city:
            return
        _LAST_CITY[webspace_id] = city
        try:
            ctx = get_ctx()
            ev = Event(
                type="weather.city_changed",
                payload={"webspace_id": webspace_id, "city": city},
                source="weather.observer",
                ts=time.time(),
            )
            ctx.bus.publish(ev)
        except Exception as exc:
            _log.warning("failed to publish weather.city_changed: %s", exc)

    def _maybe_emit(event: Y.YDocEvent | None = None) -> None:  # noqa: ARG001 - event unused
        def _run_safe() -> None:
            try:
                _emit_current()
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            threading.Thread(target=_run_safe, name="weather-observer", daemon=True).start()
        else:
            loop.call_soon(_run_safe)

    sub_id = ydoc.observe_after_transaction(_maybe_emit)
    _OBSERVERS[webspace_id] = sub_id
    _emit_current()
