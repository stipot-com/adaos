from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

if "nats" not in sys.modules:
    sys.modules["nats"] = types.SimpleNamespace()
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

from adaos.apps.api import server as api_server


def test_ping_exposes_runtime_identity_for_candidate(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "candidate")
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-b-c-12345678")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8778")

    payload = asyncio.run(api_server.ping())

    assert payload["ok"] is True
    assert payload["service"] == "adaos-runtime"
    assert payload["runtime"]["transition_role"] == "candidate"
    assert payload["runtime"]["runtime_instance_id"] == "rt-b-c-12345678"
    assert payload["runtime"]["slot"] == "B"
    assert payload["runtime"]["runtime_port"] == 8778
    assert payload["runtime"]["admin_mutation_allowed"] is False


def test_private_network_access_middleware_allows_cross_origin_loopback_probe() -> None:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "http",
        "path": "/api/ping",
        "raw_path": b"/api/ping",
        "query_string": b"",
        "headers": [
            (b"origin", b"https://myinimatic.web.app"),
            (b"access-control-request-method", b"GET"),
            (b"access-control-request-private-network", b"true"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8777),
    }

    async def _call_next(_request):
        return Response(status_code=599)

    response = asyncio.run(
        api_server.private_network_access_middleware(Request(scope), _call_next)
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://myinimatic.web.app"
    assert response.headers["Access-Control-Allow-Methods"] == "GET"
    assert response.headers["Access-Control-Allow-Private-Network"] == "true"
    assert response.headers["Vary"] == "Origin"


@pytest.mark.parametrize(
    ("callable_name", "body"),
    [
        ("admin_update_start", lambda: api_server.CoreUpdateStartRequest(reason="test.update")),
        ("admin_update_cancel", lambda: api_server.CoreUpdateCancelRequest(reason="test.cancel")),
        ("admin_update_rollback", lambda: api_server.CoreUpdateRollbackRequest(reason="test.rollback")),
    ],
)
def test_candidate_runtime_rejects_mutating_update_calls(monkeypatch, callable_name, body) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "candidate")
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-b-c-12345678")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8778")

    fn = getattr(api_server, callable_name)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(fn(body()))

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail["error"] == "candidate_runtime_is_passive"
    assert detail["runtime"]["transition_role"] == "candidate"
    assert detail["runtime"]["runtime_instance_id"] == "rt-b-c-12345678"
    assert detail["runtime"]["admin_mutation_allowed"] is False


def test_admin_update_status_includes_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "candidate")
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-b-c-abcdef12")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8778")
    monkeypatch.setattr(api_server, "finalize_core_update_boot_status", lambda: None)
    monkeypatch.setattr(api_server, "read_core_update_status", lambda: {"state": "idle"})
    monkeypatch.setattr(api_server, "read_core_update_last_result", lambda: {"state": "succeeded"})
    monkeypatch.setattr(api_server, "read_core_update_plan", lambda: None)
    monkeypatch.setattr(api_server, "core_slot_status", lambda: {"active_slot": "B"})
    monkeypatch.setattr(api_server, "active_slot_manifest", lambda: {"slot": "B"})

    payload = asyncio.run(api_server.admin_update_status())

    assert payload["status"]["state"] == "idle"
    assert payload["runtime"]["transition_role"] == "candidate"
    assert payload["runtime"]["slot"] == "B"
    assert payload["runtime"]["runtime_port"] == 8778
    assert payload["runtime"]["admin_mutation_allowed"] is False


def test_supervisor_manages_sidecar_helper(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    assert api_server._supervisor_manages_sidecar() is True

    monkeypatch.delenv("ADAOS_SUPERVISOR_ENABLED", raising=False)
    assert api_server._supervisor_manages_sidecar() is False


def test_candidate_runtime_can_be_promoted_to_active(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "candidate")
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-b-c-abcdef12")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8778")

    reconnect_calls: list[tuple[str | None, str | None]] = []
    call_order: list[str] = []

    class _ServiceSupervisor:
        async def start_all(self) -> None:
            call_order.append("services")

    async def _reconnect(*, transport: str | None = None, url_override: str | None = None):
        call_order.append("reconnect")
        reconnect_calls.append((transport, url_override))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(api_server, "get_service_supervisor", lambda: _ServiceSupervisor())
    monkeypatch.setattr(api_server, "request_hub_root_reconnect", _reconnect)

    payload = asyncio.run(
        api_server.admin_runtime_promote_active(
            api_server.RuntimePromoteActiveRequest(reason="test.cutover", reconnect_hub_root=True)
        )
    )

    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert payload["runtime"]["transition_role"] == "active"
    assert payload["runtime"]["runtime_instance_id"] == "rt-b-c-abcdef12"
    assert payload["runtime"]["admin_mutation_allowed"] is True
    assert payload["reconnect"]["ok"] is True
    assert reconnect_calls == [(None, None)]
    assert call_order == ["services", "reconnect"]


def test_promote_active_is_idempotent_for_active_runtime(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_RUNTIME_TRANSITION_ROLE", "active")
    monkeypatch.setenv("ADAOS_RUNTIME_INSTANCE_ID", "rt-a-a-abcdef12")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "A")
    monkeypatch.setenv("ADAOS_RUNTIME_PORT", "8777")

    payload = asyncio.run(
        api_server.admin_runtime_promote_active(
            api_server.RuntimePromoteActiveRequest(reason="test.cutover", reconnect_hub_root=True)
        )
    )

    assert payload["ok"] is True
    assert payload["accepted"] is False
    assert payload["runtime"]["transition_role"] == "active"
    assert payload["runtime"]["admin_mutation_allowed"] is True


def test_admin_root_mcp_logs_returns_local_logs_by_default(monkeypatch) -> None:
    monkeypatch.setattr(api_server, "normalize_log_category", lambda category: "adaos")
    monkeypatch.setattr(api_server, "get_ctx", lambda: types.SimpleNamespace(config=types.SimpleNamespace(subnet_id="sn_local")))
    monkeypatch.setattr(
        api_server,
        "list_local_logs",
        lambda **kwargs: {
            "category": kwargs["category"],
            "source_mode": kwargs["source_mode"],
            "items": [{"name": "adaos.log"}],
        },
    )

    payload = asyncio.run(api_server.admin_root_mcp_logs("adaos", limit=2, lines=50))

    assert payload["ok"] is True
    assert payload["logs"]["category"] == "adaos"
    assert payload["logs"]["source_mode"] == "node_local_logs_dir"
    assert payload["logs"]["items"][0]["name"] == "adaos.log"


def test_admin_root_mcp_logs_aggregates_active_subnet_logs(monkeypatch) -> None:
    monkeypatch.setattr(api_server, "normalize_log_category", lambda category: "yjs")
    monkeypatch.setattr(
        api_server,
        "get_ctx",
        lambda: types.SimpleNamespace(config=types.SimpleNamespace(subnet_id="sn_92ffc943")),
    )

    async def _aggregate_subnet_logs(**kwargs):
        assert kwargs["category"] == "yjs"
        assert kwargs["subnet_id"] == "sn_92ffc943"
        assert kwargs["limit"] == 4
        assert kwargs["lines"] == 120
        assert kwargs["contains"] == "desktop"
        assert kwargs["include_hub"] is False
        return {
            "category": "yjs",
            "scope": "subnet_active",
            "nodes": [{"node_id": "member:alpha", "ok": True}],
        }

    monkeypatch.setattr(api_server, "aggregate_subnet_logs", _aggregate_subnet_logs)

    payload = asyncio.run(
        api_server.admin_root_mcp_logs(
            "yjs",
            limit=4,
            lines=120,
            contains="desktop",
            scope="subnet_active",
            include_hub=False,
        )
    )

    assert payload["ok"] is True
    assert payload["logs"]["category"] == "yjs"
    assert payload["logs"]["scope"] == "subnet_active"
    assert payload["logs"]["nodes"][0]["node_id"] == "member:alpha"
