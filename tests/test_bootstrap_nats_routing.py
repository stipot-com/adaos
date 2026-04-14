from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("nats", SimpleNamespace())
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

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


def test_hub_id_from_nats_user_extracts_canonical_hub_id() -> None:
    assert bootstrap_mod._hub_id_from_nats_user("hub_sn_92ffc943") == "sn_92ffc943"
    assert bootstrap_mod._hub_id_from_nats_user("hub_9d91f466-0349-475d-9887-2d2bb3c783ee") == "9d91f466-0349-475d-9887-2d2bb3c783ee"
    assert bootstrap_mod._hub_id_from_nats_user("alias_hub") is None


def test_canonical_hub_nats_identity_prefers_response_hub_id() -> None:
    hub_id, user = bootstrap_mod._canonical_hub_nats_identity(
        local_hub_id="local-stale",
        nats_user="hub_remote-live",
        response_hub_id="sn_92ffc943",
    )

    assert hub_id == "sn_92ffc943"
    assert user == "hub_sn_92ffc943"


def test_canonical_hub_nats_identity_falls_back_to_canonical_nats_user() -> None:
    hub_id, user = bootstrap_mod._canonical_hub_nats_identity(
        local_hub_id="local-stale",
        nats_user="hub_9d91f466-0349-475d-9887-2d2bb3c783ee",
        response_hub_id=None,
    )

    assert hub_id == "9d91f466-0349-475d-9887-2d2bb3c783ee"
    assert user == "hub_9d91f466-0349-475d-9887-2d2bb3c783ee"


def test_build_hub_route_ws_bases_ignores_remote_hub_url(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("ADAOS_RUNTIME_PORT", raising=False)
    monkeypatch.setattr(bootstrap_mod, "_discover_active_runtime_local_base", lambda **_: None)

    cfg = SimpleNamespace(hub_url="https://ru.api.inimatic.com/hubs/sn_b249afeb")

    assert bootstrap_mod._build_hub_route_ws_bases(cfg=cfg) == [
        "ws://127.0.0.1:8778",
        "ws://127.0.0.1:8777",
    ]


def test_build_hub_route_ws_bases_prefers_local_env_base(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SELF_BASE_URL", "http://127.0.0.1:8779")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8780")
    monkeypatch.setattr(bootstrap_mod, "_discover_active_runtime_local_base", lambda **_: None)

    cfg = SimpleNamespace(hub_url="https://ru.api.inimatic.com/hubs/sn_b249afeb")

    assert bootstrap_mod._build_hub_route_ws_bases(cfg=cfg) == [
        "ws://127.0.0.1:8779",
        "ws://127.0.0.1:8780",
        "ws://127.0.0.1:8778",
        "ws://127.0.0.1:8777",
    ]


def test_build_hub_route_http_bases_prefers_supervisor_active_runtime(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("ADAOS_RUNTIME_PORT", raising=False)
    monkeypatch.setattr(
        bootstrap_mod,
        "_discover_active_runtime_local_base",
        lambda **_: "http://127.0.0.1:8777",
    )

    cfg = SimpleNamespace(hub_url="https://ru.api.inimatic.com/hubs/sn_b249afeb")

    assert bootstrap_mod._build_hub_route_http_bases(
        path_norm="/api/ws/test",
        method="GET",
        cfg=cfg,
    )[:2] == [
        "http://127.0.0.1:8777",
        "http://127.0.0.1:8778",
    ]


def test_build_hub_route_ws_bases_prefers_supervisor_active_runtime(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("ADAOS_RUNTIME_PORT", raising=False)
    monkeypatch.setattr(
        bootstrap_mod,
        "_discover_active_runtime_local_base",
        lambda **_: "http://127.0.0.1:8777",
    )

    cfg = SimpleNamespace(hub_url="https://ru.api.inimatic.com/hubs/sn_b249afeb")

    assert bootstrap_mod._build_hub_route_ws_bases(cfg=cfg)[:2] == [
        "ws://127.0.0.1:8777",
        "ws://127.0.0.1:8778",
    ]


def test_bootstrap_shutdown_stops_scheduler(monkeypatch) -> None:
    calls: list[str] = []

    class _Supervisor:
        async def shutdown(self) -> None:
            calls.append("supervisor")

    async def _emit(*args, **kwargs) -> None:
        calls.append(str(args[0]))

    async def _stop_scheduler() -> None:
        calls.append("scheduler")

    monkeypatch.setattr(bootstrap_mod.bus, "emit", _emit)
    monkeypatch.setattr(bootstrap_mod, "get_service_supervisor", lambda: _Supervisor())
    monkeypatch.setattr(bootstrap_mod, "stop_scheduler", _stop_scheduler)

    svc = bootstrap_mod.BootstrapService(
        SimpleNamespace(config=SimpleNamespace(role="member")),
        heartbeat=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        subnet_registry=SimpleNamespace(),
    )

    asyncio.run(svc.shutdown())

    assert "scheduler" in calls
    assert calls[0] == "sys.stopping"
    assert calls[-1] == "sys.stopped"


def test_runtime_candidate_mode_follows_transition_role(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_mod, "runtime_transition_role", lambda: "candidate")
    assert bootstrap_mod._runtime_candidate_mode() is True

    monkeypatch.setattr(bootstrap_mod, "runtime_transition_role", lambda: "active")
    assert bootstrap_mod._runtime_candidate_mode() is False
