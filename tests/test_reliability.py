from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.services.reliability import (
    ReadinessStatus,
    mark_root_control_up,
    mark_route_ready,
    reliability_snapshot,
    reset_reliability_runtime_state,
    set_integration_readiness,
)
from adaos.services.runtime_lifecycle import reset_runtime_lifecycle


def _reset_state() -> None:
    reset_runtime_lifecycle()
    reset_reliability_runtime_state()


def test_hub_reliability_snapshot_exposes_taxonomy_and_disables_root_bound_capabilities_until_ready() -> None:
    _reset_state()

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert "command" in snapshot["model"]["message_taxonomy"]
    assert "must_not_lose" in snapshot["model"]["delivery_classes"]
    assert snapshot["model"]["authority_boundaries"]["root"]["owns"]
    assert any(item["flow_id"] == "hub_root.control.lifecycle" for item in snapshot["model"]["flow_inventory"])

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["hub_local_core"]["status"] == "ready"
    assert tree["root_control"]["status"] == "unknown"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["execute_local_scenarios"]["allowed"] is True
    assert matrix["new_root_backed_member_admission"]["allowed"] is False
    assert matrix["root_routed_browser_proxy"]["allowed"] is False


def test_hub_reliability_snapshot_enables_route_and_integration_capabilities_when_signals_are_ready() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["root_control"]["status"] == "ready"
    assert tree["route"]["status"] == "ready"
    assert tree["integration"]["telegram"]["status"] == "degraded"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["root_routed_browser_proxy"]["allowed"] is True
    assert matrix["telegram_action_completion"]["allowed"] is False

    set_integration_readiness(
        "telegram",
        status=ReadinessStatus.READY,
        summary="telegram delivery probe ok",
        observed=True,
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "ready"
    assert snapshot["runtime"]["degraded_matrix"]["telegram_action_completion"]["allowed"] is True


def test_member_reliability_snapshot_uses_connected_to_hub_for_route_and_sync() -> None:
    _reset_state()

    disconnected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="none",
        connected_to_hub=False,
    )
    assert disconnected["runtime"]["readiness_tree"]["root_control"]["status"] == "not_applicable"
    assert disconnected["runtime"]["readiness_tree"]["route"]["status"] == "down"
    assert disconnected["runtime"]["readiness_tree"]["sync"]["status"] == "down"

    connected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="ws",
        connected_to_hub=True,
    )
    assert connected["runtime"]["readiness_tree"]["route"]["status"] == "ready"
    assert connected["runtime"]["readiness_tree"]["sync"]["status"] == "ready"
    assert connected["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True


def test_node_reliability_endpoint_exposes_model_and_runtime_state(monkeypatch) -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    fake_bootstrap = types.ModuleType("adaos.services.bootstrap")
    fake_bootstrap.is_ready = lambda: True
    fake_bootstrap.load_config = lambda: SimpleNamespace(node_id="node-1", subnet_id="sn_1", role="hub")

    async def _fake_switch_role(*args, **kwargs):
        return fake_bootstrap.load_config()

    fake_bootstrap.switch_role = _fake_switch_role
    monkeypatch.setitem(sys.modules, "adaos.services.bootstrap", fake_bootstrap)

    fake_link_client_mod = types.ModuleType("adaos.services.subnet.link_client")
    fake_link_client_mod.get_member_link_client = lambda: SimpleNamespace(is_connected=lambda: False)
    monkeypatch.setitem(sys.modules, "adaos.services.subnet.link_client", fake_link_client_mod)

    node_api = importlib.import_module("adaos.apps.api.node_api")
    require_token = importlib.import_module("adaos.apps.api.auth").require_token

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    monkeypatch.setattr(node_api, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready", "draining": False})

    client = TestClient(app)
    response = client.get("/api/node/reliability")
    assert response.status_code == 200

    payload = response.json()
    assert payload["model"]["authority_boundaries"]["sidecar"]["must_not_own"]
    assert payload["runtime"]["readiness_tree"]["root_control"]["status"] == "ready"
    assert payload["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True
