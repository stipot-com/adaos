"""Minimal Padatious/registry-backed adapter."""

from __future__ import annotations

import re
from typing import Any, Dict, List

try:  # pragma: no cover - padatious is optional in CI
    from padatious.intent_container import IntentContainer  # type: ignore
except Exception:  # pragma: no cover - any failure falls back to regex
    IntentContainer = None  # type: ignore


from adaos.services.nlu.registry import registry as nlu_registry


class OvosCandidate(dict):
    """Simple candidate structure with intent, score and slots."""

    def __init__(self, intent: str, score: float, slots: Dict[str, Any] | None = None):
        super().__init__(intent=intent, score=score, slots=slots or {})


_SLOT_RE = re.compile(r"\{([^{}]+)\}")
_TOKEN_RE = re.compile(r"[\w\-ёЁ]+", re.IGNORECASE)
_WEATHER_RE = re.compile(r"\b(кака[яой]\s+)?погод[ауеы]\b", re.IGNORECASE)
_PLACE_RE   = re.compile(r"(?:\bв|\bпо)\s+([a-zA-Zа-яА-ЯёЁ\-]+)", re.IGNORECASE)


def _keywords(utterance: str) -> List[str]:
    stripped = _SLOT_RE.sub(" ", utterance)
    return [token.lower() for token in _TOKEN_RE.findall(stripped)]


def _heuristic_match(text: str, utterance: str) -> bool:
    tokens = _keywords(utterance)
    if not tokens:
        return False
    lowered = text.lower()
    return all(token in lowered for token in tokens)


def parse_candidates(text: str, lang: str = "ru") -> List[Dict]:
    # 1) попробовать взять интенты из реестра (если он есть)
    try:
        from adaos.services.nlu.registry import NLURegistry
        reg = NLURegistry.get()
    except Exception:
        reg = {}

    cands: List[Dict] = []

    # 2) если в реестре нет ничего подходящего — включаем фолбэк
    if not reg or "weather_skill" not in reg or not reg["weather_skill"].intents:
        if _WEATHER_RE.search(text):
            slots = {}
            m = _PLACE_RE.search(text)
            if m:
                slots["place_raw"] = m.group(1)
            cands.append({
                "intent": "weather.show",
                "skill": "weather_skill",
                "score": 0.82,
                "slots": slots
            })
        return cands

    # 3) (опционально) на будущее: матчить utterances из реестра
    # TODO: быстрый шаблонный матч по reg["weather_skill"].intents

    return cands