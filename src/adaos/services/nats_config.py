from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

from adaos.services.zone_hosts import zone_public_base_url

PUBLIC_NATS_WS_API = "wss://api.inimatic.com/nats"
PUBLIC_NATS_WS_DEDICATED = "wss://nats.inimatic.com/nats"
PUBLIC_NATS_TCP_API = "nats://api.inimatic.com:4222"
PUBLIC_NATS_TCP_DEDICATED = "nats://nats.inimatic.com:4222"
_DEFAULT_NATS_WS_FALLBACK = object()


def _zone_public_api_base() -> str | None:
    zone_id = str(os.getenv("ADAOS_ZONE_ID", "") or "").strip().lower()
    if zone_id:
        return zone_public_base_url(zone_id)

    raw_root = str(os.getenv("ROOT_BASE_URL", "") or "").strip()
    if raw_root:
        try:
            parsed = urlparse(raw_root)
            host = str(parsed.hostname or "").strip().lower()
            if host in {"api.inimatic.com", "ru.inimatic.com"} or host.endswith(".api.inimatic.com"):
                scheme = (parsed.scheme or "https").lower()
                if scheme not in ("http", "https"):
                    scheme = "https"
                return urlunparse(parsed._replace(scheme=scheme, path="", params="", query="", fragment=""))
        except Exception:
            pass
    return None


def public_nats_ws_api() -> str:
    base = _zone_public_api_base()
    if not base:
        return PUBLIC_NATS_WS_API
    try:
        parsed = urlparse(base)
        scheme = "wss" if (parsed.scheme or "").lower() != "http" else "ws"
        return urlunparse(parsed._replace(scheme=scheme, path="/nats", params="", query="", fragment=""))
    except Exception:
        return PUBLIC_NATS_WS_API


def nats_url_uses_websocket(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        parsed = urlparse(raw if "://" in raw else f"wss://{raw}")
        scheme = (parsed.scheme or "").lower()
        return scheme in ("http", "https", "ws", "wss")
    except Exception:
        low = raw.lower()
        return low.startswith(("http://", "https://", "ws://", "wss://")) or "://" not in raw


def normalize_nats_ws_url(
    value: str | None,
    *,
    fallback: str | None | object = _DEFAULT_NATS_WS_FALLBACK,
    default_path: str = "/nats",
) -> str | None:
    if fallback is _DEFAULT_NATS_WS_FALLBACK:
        fallback = public_nats_ws_api()
    raw = str(value or "").strip()
    if not raw:
        return fallback

    if not default_path.startswith("/"):
        default_path = "/" + default_path

    try:
        parsed = urlparse(raw)
        if not parsed.scheme and "://" not in raw:
            parsed = urlparse(f"wss://{raw}")

        scheme = (parsed.scheme or "").lower()
        if scheme == "http":
            parsed = parsed._replace(scheme="ws")
        elif scheme == "https":
            parsed = parsed._replace(scheme="wss")
        elif scheme not in ("ws", "wss"):
            return raw

        if not parsed.path:
            parsed = parsed._replace(path=default_path)
        return urlunparse(parsed)
    except Exception:
        return raw


def public_nats_ws_candidates(
    *,
    prefer_dedicated: str | None = "0",
    allow_dedicated_fallback: bool = True,
) -> list[str]:
    api_ws = public_nats_ws_api()
    pref = str(prefer_dedicated or "").strip()
    if pref == "1":
        return [PUBLIC_NATS_WS_DEDICATED, api_ws]
    if allow_dedicated_fallback:
        return [api_ws, PUBLIC_NATS_WS_DEDICATED]
    return [api_ws]


def public_nats_tcp_candidates(
    *,
    prefer_dedicated: str | None = "0",
    allow_dedicated_fallback: bool = True,
) -> list[str]:
    pref = str(prefer_dedicated or "").strip()
    if pref == "1":
        return [PUBLIC_NATS_TCP_DEDICATED, PUBLIC_NATS_TCP_API]
    if allow_dedicated_fallback:
        return [PUBLIC_NATS_TCP_API, PUBLIC_NATS_TCP_DEDICATED]
    return [PUBLIC_NATS_TCP_API]


def order_nats_ws_candidates(
    candidates: list[str],
    *,
    explicit_url: str | None,
    prefer_dedicated: str | None = "0",
) -> list[str]:
    out: list[str] = []
    for item in candidates:
        txt = str(item or "").strip()
        if txt and txt not in out:
            out.append(txt)

    explicit = normalize_nats_ws_url(explicit_url, fallback=None)
    # If explicit_url points to one of our known public WS endpoints, don't force it to the front.
    # These endpoints can have different reliability characteristics depending on the network; we still
    # want `prefer_dedicated` to win by default.
    known_public = {public_nats_ws_api(), PUBLIC_NATS_WS_API, PUBLIC_NATS_WS_DEDICATED}
    if explicit and explicit in out and explicit not in known_public:
        return [explicit] + [item for item in out if item != explicit]

    preferred = None
    pref = str(prefer_dedicated or "").strip()
    if pref == "1":
        preferred = PUBLIC_NATS_WS_DEDICATED
    elif pref == "0":
        preferred = public_nats_ws_api()
    if preferred and preferred in out and out and out[0] != preferred:
        return [preferred] + [item for item in out if item != preferred]
    return out
