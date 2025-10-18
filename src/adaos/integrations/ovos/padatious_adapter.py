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


def _keywords(utterance: str) -> List[str]:
    stripped = _SLOT_RE.sub(" ", utterance)
    return [token.lower() for token in _TOKEN_RE.findall(stripped)]


def _heuristic_match(text: str, utterance: str) -> bool:
    tokens = _keywords(utterance)
    if not tokens:
        return False
    lowered = text.lower()
    return all(token in lowered for token in tokens)


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

    registry_state = nlu_registry.get()

    if not candidates:
        for skill, skill_spec in registry_state.items():
            for intent in skill_spec.intents:
                for utterance in intent.utterances:
                    tokens = _keywords(utterance)
                    if _heuristic_match(text_norm, utterance):
                        score = min(0.9, 0.6 + 0.05 * len(tokens))
                        candidate = OvosCandidate(intent.name, score=score, slots={})
                        candidate["skill"] = skill
                        candidates.append(candidate)
                        break

    if candidates:
        for candidate in candidates:
            if candidate.get("skill"):
                continue
            intent_name = candidate.get("intent")
            for skill, skill_spec in registry_state.items():
                if any(intent.name == intent_name for intent in skill_spec.intents):
                    candidate["skill"] = skill
                    break

    return candidates
