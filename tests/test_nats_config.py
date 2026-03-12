from __future__ import annotations

from adaos.services.nats_config import normalize_nats_ws_url, order_nats_ws_candidates


def test_normalize_nats_ws_url_adds_default_path() -> None:
    assert normalize_nats_ws_url("wss://nats.inimatic.com") == "wss://nats.inimatic.com/nats"


def test_normalize_nats_ws_url_converts_https_to_wss() -> None:
    assert normalize_nats_ws_url("https://api.inimatic.com") == "wss://api.inimatic.com/nats"


def test_normalize_nats_ws_url_keeps_existing_path() -> None:
    assert normalize_nats_ws_url("wss://api.inimatic.com/nats") == "wss://api.inimatic.com/nats"


def test_normalize_nats_ws_url_can_return_none() -> None:
    assert normalize_nats_ws_url(None, fallback=None) is None


def test_order_nats_ws_candidates_keeps_explicit_first_for_custom_url() -> None:
    candidates = [
        "wss://api.inimatic.com/nats",
        "wss://nats.inimatic.com/nats",
        "wss://example.com/nats",
    ]
    ordered = order_nats_ws_candidates(
        candidates,
        explicit_url="wss://example.com/nats",
        prefer_dedicated="0",
    )
    assert ordered == [
        "wss://example.com/nats",
        "wss://api.inimatic.com/nats",
        "wss://nats.inimatic.com/nats",
    ]


def test_order_nats_ws_candidates_prefer_dedicated_can_override_public_explicit() -> None:
    candidates = ["wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"]
    ordered = order_nats_ws_candidates(
        candidates,
        explicit_url="wss://api.inimatic.com/nats",
        prefer_dedicated="1",
    )
    assert ordered == ["wss://nats.inimatic.com/nats", "wss://api.inimatic.com/nats"]


def test_order_nats_ws_candidates_uses_preference_without_explicit() -> None:
    candidates = ["wss://nats.inimatic.com/nats", "wss://api.inimatic.com/nats"]
    ordered = order_nats_ws_candidates(
        candidates,
        explicit_url=None,
        prefer_dedicated="0",
    )
    assert ordered == ["wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"]
