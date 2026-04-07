from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    sys.modules.setdefault("y_py", types.ModuleType("y_py"))
    from adaos.apps.api import root_endpoints

    app = FastAPI()
    app.include_router(root_endpoints.router)
    return TestClient(app)


def test_root_mcp_foundation_and_contracts(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    client = _make_client()
    headers = {
        "X-Owner-Token": "owner-secret",
        "X-AdaOS-Subnet-Id": "subnet:test-zone",
        "X-AdaOS-Zone": "lab-a",
    }

    foundation = client.get("/v1/root/mcp/foundation", headers=headers)
    assert foundation.status_code == 200
    foundation_payload = foundation.json()
    assert foundation_payload["ok"] is True
    assert foundation_payload["scope"]["subnet_id"] == "subnet:test-zone"
    assert foundation_payload["foundation"]["id"] == "root-mcp-foundation"
    assert foundation_payload["foundation"]["surfaces"]["development"]["enabled"] is True
    assert foundation_payload["foundation"]["managed_targets"]["preferred_target_surface"] == "infra_access_skill"
    assert foundation_payload["foundation"]["client"]["recommended_client"] == "RootMcpClient"

    contracts = client.get("/v1/root/mcp/contracts", headers=headers)
    assert contracts.status_code == 200
    contract_items = contracts.json()["contracts"]
    contract_ids = {item["id"] for item in contract_items}
    assert "development.get_descriptor_set" in contract_ids
    assert "operations.list_contracts" in contract_ids
    assert "operations.list_managed_targets" in contract_ids
    assert "development.export_sdk" not in contract_ids
    placeholder = next(item for item in contract_items if item["id"] == "hub.get_status")
    assert placeholder["availability"] == "placeholder"
    assert placeholder["metadata"]["published_by"] == "skill:infra_access_skill"

    targets = client.get("/v1/root/mcp/targets", headers=headers)
    assert targets.status_code == 200
    target_items = targets.json()["targets"]
    assert target_items
    first = target_items[0]
    assert first["operational_surface"]["published_by"] == "skill:infra_access_skill"
    assert "access_token" in first["access"]["client_config_fields"]


def test_root_mcp_call_records_audit(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    client = _make_client()
    headers = {
        "X-Owner-Token": "owner-secret",
        "X-AdaOS-Subnet-Id": "subnet:test-zone",
        "X-AdaOS-Zone": "lab-a",
    }

    resp = client.post(
        "/v1/root/mcp/call",
        headers=headers,
        json={
            "tool_id": "development.get_descriptor_set",
            "request_id": "req-root-mcp-1",
            "arguments": {"descriptor_id": "system_model_vocabulary"},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    envelope = payload["response"]
    assert envelope["request_id"] == "req-root-mcp-1"
    assert envelope["tool_id"] == "development.get_descriptor_set"
    assert envelope["status"] == "ok"
    assert envelope["meta"]["subnet_id"] == "subnet:test-zone"
    assert "descriptor" in envelope["result"]
    assert envelope["audit_event_id"]

    audit = client.get(
        "/v1/root/mcp/audit",
        headers=headers,
        params={"tool_id": "development.get_descriptor_set", "subnet_filter": "subnet:test-zone"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert events
    assert any(item["event_id"] == envelope["audit_event_id"] for item in events)
    assert all(item["meta"]["subnet_id"] == "subnet:test-zone" for item in events)


def test_root_mcp_placeholder_tool_returns_structured_error(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    client = _make_client()
    headers = {"X-Owner-Token": "owner-secret"}

    resp = client.post(
        "/v1/root/mcp/call",
        headers=headers,
        json={
            "tool_id": "hub.get_status",
            "request_id": "req-root-mcp-2",
            "arguments": {"target_id": "hub:test-alpha"},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    envelope = payload["response"]
    assert envelope["tool_id"] == "hub.get_status"
    assert envelope["status"] == "error"
    assert envelope["error"]["code"] == "tool_not_available"
    assert envelope["meta"]["availability"] == "placeholder"


def test_root_mcp_descriptor_not_found_is_structured(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    client = _make_client()
    headers = {"X-Owner-Token": "owner-secret"}

    resp = client.post(
        "/v1/root/mcp/call",
        headers=headers,
        json={
            "tool_id": "development.get_descriptor_set",
            "request_id": "req-root-mcp-3",
            "arguments": {"descriptor_id": "missing_descriptor"},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    envelope = payload["response"]
    assert envelope["status"] == "error"
    assert envelope["error"]["code"] == "not_found"
