from __future__ import annotations

from typing import Final

CENTRAL_PUBLIC_HOST: Final[str] = "api.inimatic.com"
RU_PUBLIC_HOST: Final[str] = "ru.api.inimatic.com"
CANONICAL_ZONE_IDS: Final[tuple[str, ...]] = ("us", "eu", "ru", "in", "ch")
CENTRAL_ZONE_IDS: Final[tuple[str, ...]] = ("us", "eu", "in", "ch")

ZONE_ALIASES: Final[dict[str, str]] = {
    "api": "us",
    "central": "us",
    "default": "us",
    "global": "us",
    "world": "us",
    "usa": "us",
    "na": "us",
    "north-america": "us",
    "eu": "eu",
    "de": "eu",
    "europe": "eu",
    "europe-west": "eu",
    "ru": "ru",
    "rus": "ru",
    "russia": "ru",
    "ru-ru": "ru",
    "in": "in",
    "india": "in",
    "bharat": "in",
    "ch": "ch",
    "cn": "ch",
    "china": "ch",
}


def canonical_zone_id(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return ZONE_ALIASES.get(raw, raw if raw in CANONICAL_ZONE_IDS else None)


def default_zone_for_server(server_zone_id: str | None) -> str:
    canonical = canonical_zone_id(server_zone_id)
    return "ru" if canonical == "ru" else "us"


def supported_zone_ids_for_server(server_zone_id: str | None) -> tuple[str, ...]:
    canonical = canonical_zone_id(server_zone_id)
    return ("ru",) if canonical == "ru" else CENTRAL_ZONE_IDS


def resolve_zone_for_server(requested_zone_id: str | None, server_zone_id: str | None) -> str:
    canonical_requested = canonical_zone_id(requested_zone_id)
    supported = supported_zone_ids_for_server(server_zone_id)
    if canonical_requested in supported:
        return canonical_requested
    return default_zone_for_server(server_zone_id)


def zone_public_host(zone_id: str | None) -> str:
    canonical = canonical_zone_id(zone_id)
    return RU_PUBLIC_HOST if canonical == "ru" else CENTRAL_PUBLIC_HOST


def zone_public_base_url(zone_id: str | None, *, scheme: str = "https") -> str:
    normalized_scheme = str(scheme or "https").strip().lower()
    if normalized_scheme not in {"http", "https"}:
        normalized_scheme = "https"
    return f"{normalized_scheme}://{zone_public_host(zone_id)}"


def zone_public_nats_ws_url(zone_id: str | None, *, path: str = "/nats") -> str:
    normalized_path = str(path or "/nats").strip() or "/nats"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return f"wss://{zone_public_host(zone_id)}{normalized_path}"
