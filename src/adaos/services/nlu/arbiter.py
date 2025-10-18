"""Assemble NLU candidates, apply heuristics and emit structured interpretation."""

from __future__ import annotations

from typing import Any, Dict, List

from adaos.services.eventbus import EVENT_NLU_INTERPRETATION
from adaos.services.nlu.context import DialogContext
from adaos.services.nlu import normalizer
from adaos.integrations.ovos.padatious_adapter import parse_candidates

_WEATHER_INTENT = "weather.show"
_WEATHER_SKILL = "weather_skill"
_DEFAULT_ENTITY_CONF = 0.9


def _sanitize_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    slots = dict(raw.get("slots", {}))
    slots.pop("place_raw", None)
    candidate = {
        "intent": raw.get("intent"),
        "score": float(raw.get("score", 0.0) or 0.0),
        "slots": slots,
    }
    if "skill" in raw:
        candidate["skill"] = raw["skill"]
    elif candidate["intent"] == _WEATHER_INTENT:
        candidate["skill"] = _WEATHER_SKILL
    return candidate


def _entities_from_slots(date_norm: str | None, location_norm: tuple[str, float] | None) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    if date_norm:
        entities.append({"type": "datetime", "value": date_norm, "conf": _DEFAULT_ENTITY_CONF})
    if location_norm:
        entities.append({"type": "location", "value": location_norm[0], "conf": location_norm[1]})
    return entities


def _normalize_location_from_slots(slots: Dict[str, Any]) -> tuple[str, float] | None:
    if "place" in slots and slots["place"]:
        norm = normalizer.normalize_city_ru(str(slots["place"]))
        if norm:
            slots["place"] = norm[0]
            return norm
    if "place_raw" in slots:
        norm = normalizer.normalize_city_ru(str(slots["place_raw"]))
        if norm:
            slots["place"] = norm[0]
            slots.pop("place_raw", None)
            return norm
    return None


def arbitrate(text: str, lang: str, user_home_city: str | None, ctx: DialogContext) -> Dict[str, Any]:
    date_norm = normalizer.normalize_date_ru(text, today=normalizer.now_berlin())
    location_from_text = normalizer.normalize_city_ru(text)

    candidates_raw = parse_candidates(text, lang)
    if not candidates_raw:
        candidates_raw = [{"intent": _WEATHER_INTENT, "score": 0.5, "slots": {}}]

    best_raw = max(candidates_raw, key=lambda c: float(c.get("score", 0.0) or 0.0))
    slots = dict(best_raw.get("slots", {}))

    if date_norm:
        slots["date"] = date_norm
    loc_from_slots = _normalize_location_from_slots(slots)
    if location_from_text:
        slots["place"] = location_from_text[0]
        location_norm = location_from_text
    elif loc_from_slots:
        location_norm = loc_from_slots
    else:
        location_norm = None
        slots.pop("place_raw", None)

    entities = _entities_from_slots(date_norm, location_norm)

    last = ctx.get_last()
    if best_raw.get("intent") == _WEATHER_INTENT:
        if not slots.get("date"):
            if last and last.intent == _WEATHER_INTENT and last.slots.get("date"):
                slots["date"] = last.slots["date"]
            else:
                slots["date"] = normalizer.today_iso_berlin()
        if not slots.get("place"):
            if last and last.intent == _WEATHER_INTENT and last.slots.get("place"):
                slots["place"] = last.slots["place"]
            elif user_home_city:
                slots["place"] = user_home_city

    slots.pop("place_raw", None)

    chosen = {
        "intent": best_raw.get("intent"),
        "skill": _WEATHER_SKILL if best_raw.get("intent") == _WEATHER_INTENT else best_raw.get("skill"),
        "score": float(best_raw.get("score", 0.0) or 0.0),
        "slots": slots,
    }

    candidates = [_sanitize_candidate(c) for c in candidates_raw]

    event = {
        "type": EVENT_NLU_INTERPRETATION,
        "trace_id": normalizer.new_uuid(),
        "payload": {
            "raw": {"text": text, "lang": lang},
            "entities": entities,
            "candidates": candidates,
            "chosen": chosen,
            "dialog_act": "EXECUTE",
        },
    }

    if chosen.get("intent"):
        ctx.set_last(str(chosen["intent"]), chosen["slots"])
    return event
