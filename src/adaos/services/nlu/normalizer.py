"""Lightweight language normalisation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid


EUROPE_BERLIN_TZ = timezone(timedelta(hours=2))


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


def normalize_date_ru(text: str, today: datetime | None = None) -> str | None:
    """Normalize very small subset of Russian relative dates."""

    if not text:
        return None
    base = today or now_berlin()
    lowered = _normalize_text(text)
    for token, offset in _DATE_TOKENS.items():
        if token in lowered:
            return (base + timedelta(days=offset)).date().isoformat()
    return None
