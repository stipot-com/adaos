from __future__ import annotations

from adaos.services import bootstrap as bootstrap_mod


def test_nats_url_needs_public_ws_refresh_for_legacy_public_tcp_url() -> None:
    assert bootstrap_mod._nats_url_needs_public_ws_refresh("nats://nats.inimatic.com:4222") is True
    assert bootstrap_mod._nats_url_needs_public_ws_refresh("nats://api.inimatic.com:4222") is True


def test_nats_url_does_not_need_public_ws_refresh_for_local_or_ws_url() -> None:
    assert bootstrap_mod._nats_url_needs_public_ws_refresh("nats://127.0.0.1:4222") is False
    assert bootstrap_mod._nats_url_needs_public_ws_refresh("nats://localhost:4222") is False
    assert bootstrap_mod._nats_url_needs_public_ws_refresh("wss://nats.inimatic.com/nats") is False


def test_realtime_sidecar_fallback_candidates_disable_tcp_fallback_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK", raising=False)

    assert (
        bootstrap_mod._build_realtime_sidecar_fallback_candidates(
            ["nats://nats.inimatic.com:4222", "wss://nats.inimatic.com/nats"],
            local_candidate="nats://127.0.0.1:7422",
        )
        == []
    )


def test_realtime_sidecar_fallback_candidates_can_keep_raw_tcp_fallback(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK", "1")

    assert bootstrap_mod._build_realtime_sidecar_fallback_candidates(
        ["nats://nats.inimatic.com:4222", "wss://nats.inimatic.com/nats"],
        local_candidate="nats://127.0.0.1:7422",
    ) == ["nats://nats.inimatic.com:4222"]


def test_resolve_nats_log_server_prefers_current_attempt() -> None:
    assert (
        bootstrap_mod._resolve_nats_log_server(
            current_attempt="nats://127.0.0.1:7422",
            connected_server="nats://nats.inimatic.com:4222",
        )
        == "nats://127.0.0.1:7422"
    )


def test_hub_nats_prefer_dedicated_defaults_to_api_domain(monkeypatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)

    assert bootstrap_mod._hub_nats_prefer_dedicated() == "0"


def test_hub_nats_prefer_dedicated_respects_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("HUB_NATS_PREFER_DEDICATED", "1")

    assert bootstrap_mod._hub_nats_prefer_dedicated() == "1"


def test_normalize_hub_nats_ws_url_rewrites_public_dedicated_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)

    assert bootstrap_mod._normalize_hub_nats_ws_url("wss://nats.inimatic.com/nats") == "wss://api.inimatic.com/nats"


def test_normalize_hub_nats_ws_url_keeps_public_dedicated_on_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("HUB_NATS_PREFER_DEDICATED", "1")

    assert bootstrap_mod._normalize_hub_nats_ws_url("wss://nats.inimatic.com/nats") == "wss://nats.inimatic.com/nats"


def test_hub_public_ws_candidates_default_to_api_only(monkeypatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)

    assert bootstrap_mod._hub_public_ws_candidates(None) == ["wss://api.inimatic.com/nats"]


def test_hub_public_ws_candidates_rewrite_public_dedicated_default(monkeypatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)

    assert bootstrap_mod._hub_public_ws_candidates("wss://nats.inimatic.com/nats") == [
        "wss://api.inimatic.com/nats"
    ]


def test_hub_public_ws_candidates_can_opt_in_dedicated(monkeypatch) -> None:
    monkeypatch.setenv("HUB_NATS_PREFER_DEDICATED", "1")

    assert bootstrap_mod._hub_public_ws_candidates("wss://nats.inimatic.com/nats") == [
        "wss://nats.inimatic.com/nats",
        "wss://api.inimatic.com/nats",
    ]


def test_hub_route_force_close_no_upstream_defaults_enabled(monkeypatch) -> None:
    monkeypatch.delenv("HUB_ROUTE_FORCE_CLOSE_NO_UPSTREAM_S", raising=False)

    assert bootstrap_mod._hub_route_force_close_no_upstream_s() == 1.5


def test_hub_route_force_close_no_upstream_can_disable(monkeypatch) -> None:
    monkeypatch.setenv("HUB_ROUTE_FORCE_CLOSE_NO_UPSTREAM_S", "0")

    assert bootstrap_mod._hub_route_force_close_no_upstream_s() == 0.0
