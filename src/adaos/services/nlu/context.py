"""Minimal in-process dialog context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class LastIntent:
    intent: str
    skill: str | None
    slots: Dict[str, str]


class DialogContext:
    """Stores the last recognised intent for lightweight follow-ups."""

    def __init__(self) -> None:
        self._last: Optional[LastIntent] = None

    def set_last(self, intent: str, skill: str | None, slots: Dict[str, str]) -> None:
        self._last = LastIntent(intent=intent, skill=skill, slots=dict(slots))

    def get_last(self) -> Optional[LastIntent]:
        return self._last

    def clear(self) -> None:
        self._last = None
