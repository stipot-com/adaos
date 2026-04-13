from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi import HTTPException

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

    async def _reconnect(*, transport: str | None = None, url_override: str | None = None):
        reconnect_calls.append((transport, url_override))
        return {"ok": True, "accepted": True}

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
