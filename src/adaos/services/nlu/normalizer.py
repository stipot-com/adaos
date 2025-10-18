"""Lightweight helpers for RU weather queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Tuple, Optional
import uuid

EUROPE_BERLIN_TZ = timezone(timedelta(hours=2))

# минимальный словарь городов и падежных форм
CITY_MAP = {
    "берлин": "Berlin, DE",
    "берлине": "Berlin, DE",
    "лондон": "London, GB",
    "лондоне": "London, GB",
}

# Дополнительные варианты, которые можно расширять позже
CITY_CONFIDENCE = 0.9


def now_berlin() -> datetime:
    """Return current datetime converted to the fixed Berlin timezone."""

    return datetime.now(timezone.utc).astimezone(EUROPE_BERLIN_TZ)


def today_iso_berlin() -> str:
    """Return today's date in ISO format for the Berlin timezone."""

    return now_berlin().date().isoformat()


def new_uuid() -> str:
    """Generate a new UUID string."""

    return str(uuid.uuid4())


_DATE_TOKENS = {
    "сегодня": 0,
    "завтра": 1,
}


def _normalize_text(text: str) -> str:
    return text.strip().lower()


def normalize_date_ru(text: str, today: datetime | None = None) -> Optional[str]:
    """Normalize very small subset of Russian relative dates."""

    if not text:
        return None
    base = today or now_berlin()
    lowered = _normalize_text(text)
    for token, offset in _DATE_TOKENS.items():
        if token in lowered:
            return (base + timedelta(days=offset)).date().isoformat()
    return None


_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ\-]+", re.IGNORECASE)


def _iter_tokens(text: str):
    for match in _WORD_RE.finditer(text):
        yield match.group(0).lower()


def normalize_city_ru(text: str) -> Optional[Tuple[str, float]]:
    """Return canonical city name and confidence if recognised."""

    if not text:
        return None
    lowered = text.lower()
    if lowered in CITY_MAP:
        return CITY_MAP[lowered], CITY_CONFIDENCE
    for token in _iter_tokens(text):
        canonical = CITY_MAP.get(token)
        if canonical:
            return canonical, CITY_CONFIDENCE
    return None
