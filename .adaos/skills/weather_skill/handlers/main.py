"""Weather skill handlers for the runtime reference implementation."""

from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

import requests

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import secrets as skill_secrets
from adaos.sdk.data.bus import emit
from adaos.sdk.data.context import get_current_skill, set_current_skill
from adaos.sdk.data.i18n import _
from adaos.sdk.data.skill_memory import get as memory_get, set as memory_set


DEFAULT_API_ENDPOINT = "https://api.openweathermap.org/data/2.5/weather"


def _output(message: str) -> None:
    print(message)


def _load_config() -> Tuple[Optional[str], str, Optional[str]]:
    """Load the runtime configuration from the SDK stores."""

    api_key = skill_secrets.get("api_key")
    api_entry_point = memory_get("api_entry_point") or DEFAULT_API_ENDPOINT
    default_city = memory_get("default_city")

    if api_entry_point is None:
        api_entry_point = DEFAULT_API_ENDPOINT

    # Legacy support: migrate values from the local prep cache if present.
    try:
        skill = get_current_skill()
        if skill:
            prep_file = skill.path / "prep" / "prep_result.json"
            if prep_file.exists():
                data = json.loads(prep_file.read_text(encoding="utf-8"))
                resources = data.get("resources") or {}
                if not api_key and resources.get("api_key"):
                    api_key = resources["api_key"]
                    skill_secrets.set("api_key", api_key)
                if not default_city and resources.get("default_city"):
                    default_city = resources["default_city"]
                    memory_set("default_city", default_city)
                if resources.get("api_entry_point"):
                    api_entry_point = resources["api_entry_point"]
                    memory_set("api_entry_point", api_entry_point)
    except Exception:
        # Prep artefacts are optional; swallow errors to keep runtime resilient.
        pass

    return api_key, api_entry_point, default_city


def _resolve_city(requested_city: Optional[str]) -> Optional[str]:
    city = requested_city or memory_get("last_city") or memory_get("default_city")
    if city:
        memory_set("last_city", city)
    return city


def _fetch_weather(api_entry_point: str, api_key: str, city: str) -> Tuple[bool, Dict]:
    try:
        response = requests.get(
            api_entry_point,
            params={"q": city, "appid": api_key, "units": "metric", "lang": "en"},
            timeout=6,
        )
    except Exception as exc:  # pragma: no cover - network error surface only
        return False, {"error": _("runtime.weather.errors.request", reason=str(exc))}

    if response.status_code != 200:
        return False, {"error": _("runtime.weather.errors.status", status=response.status_code)}

    try:
        payload = response.json()
    except Exception:
        return False, {"error": _("runtime.weather.errors.invalid_json")}

    main = payload.get("main") or {}
    temp = main.get("temp")
    description = (payload.get("weather") or [{}])[0].get("description", "")
    if temp is None:
        return False, {"error": _("runtime.weather.errors.invalid_response")}

    return True, {"city": city, "temp": temp, "description": description}


def handle(topic: str, payload: dict) -> None:
    """Local development entrypoint for the skill."""

    set_current_skill("weather_skill")
    api_key, api_entry_point, default_city = _load_config()
    if not api_key:
        _output(_("prep.weather.missing_key"))
        return

    city = _resolve_city((payload or {}).get("city")) or default_city
    if not city:
        _output(_("prep.weather.api_error", city=""))
        return

    ok, data = _fetch_weather(api_entry_point, api_key, city)
    if not ok:
        _output(_("prep.weather.api_error", city=city))
        return

    _output(_("prep.weather.success", city=data["city"], temp=data["temp"], description=data["description"]))


def handle_intent(intent: str, entities: dict) -> None:
    city = (entities or {}).get("city")
    handle(intent or "nlp.intent.weather.get", {"city": city} if city else {})


@tool("get_weather")
def get_weather(city: Optional[str] = None) -> Dict:
    api_key, api_entry_point, default_city = _load_config()
    if not api_key:
        return {"ok": False, "error": _("runtime.weather.errors.missing_api_config")}

    target_city = city or default_city or memory_get("last_city")
    if not target_city:
        return {"ok": False, "error": _("runtime.weather.errors.missing_city")}

    ok, data = _fetch_weather(api_entry_point, api_key, target_city)
    if not ok:
        return {"ok": False, **data}

    return {"ok": True, **data}


@tool("setup")
def setup(payload: Optional[dict] = None) -> Dict:
    payload = payload or {}
    provided = (payload.get("api_key") or "").strip()
    if not provided:
        try:
            provided = input(_("prep.ask_api_key")).strip()
        except EOFError:
            provided = ""
    if not provided:
        return {"ok": False, "error": _("runtime.weather.setup.missing")}

    skill_secrets.set("api_key", provided)
    return {"ok": True, "message": _("runtime.weather.setup.saved")}


@subscribe("nlp.intent.weather.get")
async def on_weather_intent(evt) -> None:
    api_key, api_entry_point, default_city = _load_config()
    if not api_key:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.missing_key")},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    city = _resolve_city((evt.payload or {}).get("city")) or default_city
    if not city:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city="")},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    ok, data = _fetch_weather(api_entry_point, api_key, city)
    if not ok:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city=city)},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    await emit(
        "ui.notify",
        {
            "text": _(
                "prep.weather.success",
                city=data["city"],
                temp=data["temp"],
                description=data["description"],
            )
        },
        actor=evt.actor,
        source="weather_skill",
        trace_id=evt.trace_id,
    )


__all__ = [
    "handle",
    "handle_intent",
    "get_weather",
    "setup",
    "on_weather_intent",
]

