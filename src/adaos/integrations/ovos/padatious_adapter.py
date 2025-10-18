"""Minimal Padatious/regex adapter used for weather NLU."""

from __future__ import annotations

import re
from typing import Any, Dict, List

try:  # pragma: no cover - padatious is optional in CI
    from padatious.intent_container import IntentContainer  # type: ignore
except Exception:  # pragma: no cover - any failure falls back to regex
    IntentContainer = None  # type: ignore


class OvosCandidate(dict):
    """Simple candidate structure with intent, score and slots."""

    def __init__(self, intent: str, score: float, slots: Dict[str, Any] | None = None):
        super().__init__(intent=intent, score=score, slots=slots or {})


_WEATHER_RE = re.compile(r"(кака[яой]|что\s+по)?\s*погод[аеуы]", re.IGNORECASE)
_PLACE_RE = re.compile(r"(?:в|по)\s+([a-zA-Zа-яА-ЯёЁ\-]+)")


def _fallback_candidates(text: str) -> List[OvosCandidate]:
    slots: Dict[str, Any] = {}
    place_match = _PLACE_RE.search(text)
    if place_match:
        slots["place_raw"] = place_match.group(1)
    has_weather = bool(_WEATHER_RE.search(text))
    if has_weather or slots:
        score = 0.82 if has_weather else 0.72
        return [OvosCandidate("weather.show", score=score, slots=slots)]
    return []


def parse_candidates(text: str, lang: str = "ru") -> List[OvosCandidate]:
    """Return Padatious candidates or regex fallback."""

    if not text:
        return []
    text_norm = text.strip()
    candidates: List[OvosCandidate] = []

    container = None
    if IntentContainer is not None:  # pragma: no branch - rarely true in tests
        try:
            container = IntentContainer(f"adaos-{lang}")
        except Exception:
            container = None

    if container is not None:
        try:  # pragma: no cover - padatious path not covered in CI
            intents = container.calc_intents(text_norm)
        except Exception:
            intents = []
        for intent_data in intents:
            intent = intent_data.get("intent")
            if not intent:
                continue
            score = float(intent_data.get("confidence", 0.0))
            slots = dict(intent_data.get("matches", {}))
            candidates.append(OvosCandidate(intent, score, slots))

    if not candidates:
        candidates = _fallback_candidates(text_norm)

    return candidates
