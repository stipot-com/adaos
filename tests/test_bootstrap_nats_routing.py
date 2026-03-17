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
