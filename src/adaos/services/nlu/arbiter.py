"""Assemble NLU candidates, apply skill-defined disambiguation rules."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from adaos.services.eventbus import EVENT_NLU_INTERPRETATION
from adaos.services.nlu.context import DialogContext
from adaos.services.nlu import normalizer
from adaos.integrations.ovos.padatious_adapter import parse_candidates
from adaos.services.nlu.registry import registry as nlu_registry, SkillNLU, IntentSpec
from adaos.sdk.data import memory as sdk_memory
from adaos.sdk.core.errors import SdkRuntimeNotInitialized

_DEFAULT_ENTITY_CONF = 0.9


logger = logging.getLogger(__name__)


def _sanitize_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    slots = dict(raw.get("slots", {}))
    candidate = {
        "intent": raw.get("intent"),
        "score": float(raw.get("score", 0.0) or 0.0),
        "slots": slots,
    }
    if raw.get("skill"):
        candidate["skill"] = raw["skill"]
    return candidate


def _entities_from_slots(date_norm: str | None, location_norm: tuple[str, float] | None) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    if date_norm:
        entities.append({"type": "datetime", "value": date_norm, "conf": _DEFAULT_ENTITY_CONF})
    if location_norm:
        entities.append({"type": "location", "value": location_norm[0], "conf": location_norm[1]})
    return entities


def _resolve_default(value: str) -> Any | None:
    if not isinstance(value, str):
        return value
    if value.startswith("@"):
        key = value[1:]
        if not key:
            return None
        try:
            stored = sdk_memory.get(key, None)
        except SdkRuntimeNotInitialized:
            return None
        return stored
    return value


def _apply_location_resolver(
    skill_spec: SkillNLU,
    intent_spec: IntentSpec,
    text: str,
    lang: str,
    slots: Dict[str, Any],
) -> tuple[str, float] | None:
    resolver = skill_spec.resolvers.get("location")
    if not resolver:
        return None
    resources = skill_spec.resources.get(lang, {})
    try:
        result = resolver(text=text, lang=lang, slots=dict(slots), resources=resources)
    except TypeError:
        # Backwards-compatible call signature: resolver(text, lang, slots)
        result = resolver(text, lang, dict(slots))  # type: ignore[misc]
    except Exception:  # pragma: no cover - resolver bugs
        logger.exception("NLU resolver failure for skill=%s", skill_spec.skill)
        return None
    if result is None:
        return None
    if isinstance(result, tuple):
        if not result:
            return None
        value = result[0]
        conf = float(result[1]) if len(result) > 1 else _DEFAULT_ENTITY_CONF
    else:
        value = result
        conf = _DEFAULT_ENTITY_CONF
    if value:
        slots["place"] = value
        return str(value), conf
    return None


def _apply_disambiguation(
    slots: Dict[str, Any],
    intent_spec: IntentSpec,
    last: Tuple[str, str | None, Dict[str, Any]] | None,
) -> None:
    disamb = intent_spec.disambiguation
    last_intent = None
    last_slots: Dict[str, Any] = {}
    if last:
        last_intent, _, last_slots = last
    if disamb.inherit and last_intent == intent_spec.name:
        for key in disamb.inherit:
            if key not in slots and last_slots.get(key) is not None:
                slots[key] = last_slots[key]
    for key, value in disamb.defaults.items():
        if key in slots and slots[key]:
            continue
        resolved = _resolve_default(value)
        if resolved is not None:
            slots[key] = resolved


def arbitrate(text: str, lang: str, ctx: DialogContext) -> Dict[str, Any]:
    date_norm = normalizer.normalize_date_ru(text, today=normalizer.now_berlin())
    registry_state = nlu_registry.get()

    candidates_raw = parse_candidates(text, lang)
    candidates_filtered = [c for c in candidates_raw if registry_state.get(str(c.get("skill")))]

    if not candidates_filtered:
        last = ctx.get_last()
        if last and last.skill and registry_state.get(last.skill):
            candidates_filtered = [
                {
                    "intent": last.intent,
                    "skill": last.skill,
                    "score": 0.5,
                    "slots": {},
                }
            ]
        else:
            candidates = [_sanitize_candidate(c) for c in candidates_raw]
            event = {
                "type": EVENT_NLU_INTERPRETATION,
                "trace_id": normalizer.new_uuid(),
                "payload": {
                    "raw": {"text": text, "lang": lang},
                    "entities": [],
                    "candidates": candidates,
                    "chosen": {},
                    "dialog_act": "EXECUTE",
                },
            }
            return event

    if not candidates_raw and candidates_filtered:
        candidates_raw = candidates_filtered

    best_raw = max(candidates_filtered, key=lambda c: float(c.get("score", 0.0) or 0.0))
    slots = dict(best_raw.get("slots", {}))
    if date_norm:
        slots["date"] = date_norm

    skill_name = str(best_raw.get("skill"))
    skill_spec = registry_state[skill_name]
    intent_name = str(best_raw.get("intent"))
    intent_spec = next((i for i in skill_spec.intents if i.name == intent_name), None)
    location_norm: tuple[str, float] | None = None
    if intent_spec is not None:
        location_norm = _apply_location_resolver(skill_spec, intent_spec, text, lang, slots)
        last = ctx.get_last()
        last_tuple = (last.intent, last.skill, last.slots) if last else None
        _apply_disambiguation(slots, intent_spec, last_tuple)
    else:
        last_tuple = None

    entities = _entities_from_slots(slots.get("date"), location_norm)

    chosen = {
        "intent": intent_name,
        "skill": skill_name,
        "score": float(best_raw.get("score", 0.0) or 0.0),
        "slots": slots,
    }

    candidates = [_sanitize_candidate(c) for c in candidates_filtered]

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

    ctx.set_last(intent_name, skill_name, slots)
    return event
