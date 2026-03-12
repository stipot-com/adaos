from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_nats_ws_url(
    value: str | None,
    *,
    fallback: str | None = "wss://nats.inimatic.com/nats",
    default_path: str = "/nats",
) -> str | None:
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


def order_nats_ws_candidates(
    candidates: list[str],
    *,
    explicit_url: str | None,
    prefer_dedicated: str | None = "1",
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
    known_public = {"wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"}
    if explicit and explicit in out and explicit not in known_public:
        return [explicit] + [item for item in out if item != explicit]

    preferred = None
    pref = str(prefer_dedicated or "").strip()
    if pref == "1":
        preferred = "wss://nats.inimatic.com/nats"
    elif pref == "0":
        preferred = "wss://api.inimatic.com/nats"
    if preferred and preferred in out and out and out[0] != preferred:
        return [preferred] + [item for item in out if item != preferred]
    return out
