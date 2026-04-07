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
    scoped_headers = {
        "X-Owner-Token": "owner-secret",
        "X-AdaOS-Subnet-Id": "subnet:test-zone",
        "X-AdaOS-Zone": "lab-a",
    }
    owner_headers = {"X-Owner-Token": "owner-secret"}

    foundation = client.get("/v1/root/mcp/foundation", headers=scoped_headers)
    assert foundation.status_code == 200
    foundation_payload = foundation.json()
    assert foundation_payload["ok"] is True
    assert foundation_payload["scope"]["subnet_id"] == "subnet:test-zone"
    assert foundation_payload["foundation"]["id"] == "root-mcp-foundation"
    assert foundation_payload["foundation"]["surfaces"]["development"]["enabled"] is True
    assert foundation_payload["foundation"]["managed_targets"]["preferred_target_surface"] == "infra_access_skill"
    assert foundation_payload["foundation"]["client"]["recommended_client"] == "RootMcpClient"

    contracts = client.get("/v1/root/mcp/contracts", headers=scoped_headers)
    assert contracts.status_code == 200
    contract_items = contracts.json()["contracts"]
    contract_ids = {item["id"] for item in contract_items}
    assert "development.get_descriptor_set" in contract_ids
    assert "operations.list_contracts" in contract_ids
    assert "operations.list_managed_targets" in contract_ids
    assert "development.export_sdk" not in contract_ids
    implemented = next(item for item in contract_items if item["id"] == "hub.get_status")
    assert implemented["availability"] == "enabled"
    assert implemented["metadata"]["published_by"] == "root"
    placeholder = next(item for item in contract_items if item["id"] == "hub.get_logs")
    assert placeholder["availability"] == "placeholder"
    assert placeholder["metadata"]["published_by"] == "skill:infra_access_skill"

    targets = client.get("/v1/root/mcp/targets", headers=owner_headers)
    assert targets.status_code == 200
    target_items = targets.json()["targets"]
    assert target_items
    first = target_items[0]
    assert first["operational_surface"]["published_by"] == "skill:infra_access_skill"
    assert "access_token" in first["access"]["client_config_fields"]

    descriptors = client.get("/v1/root/mcp/descriptors", headers=scoped_headers)
    assert descriptors.status_code == 200
    descriptor_items = descriptors.json()["descriptors"]
    descriptor_ids = {item["descriptor_id"] for item in descriptor_items}
    assert "capability_registry" in descriptor_ids
    assert "mcp_client_profile" in descriptor_ids

    capability_registry = client.get("/v1/root/mcp/descriptors/capability_registry", headers=scoped_headers)
    assert capability_registry.status_code == 200
    capability_payload = capability_registry.json()["descriptor"]["payload"]
    assert capability_payload["classes"]
    assert any(item["capability"] == "development.read.descriptors" for item in capability_payload["classes"])


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


def test_root_mcp_targets_support_state_registry_and_scope(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import targets as target_registry

    registry_path = tmp_path / "managed_targets.json"
    monkeypatch.setattr(target_registry, "_registry_path", lambda: registry_path)
    target_registry.upsert_managed_target(
        {
            "target_id": "hub:test-extra",
            "title": "Extra Test Hub",
            "kind": "hub",
            "environment": "test",
            "status": "online",
            "zone": "lab-b",
            "subnet_id": "subnet:extra",
            "operational_surface": {"published_by": "skill:infra_access_skill", "enabled": True},
        }
    )

    client = _make_client()
    headers = {"X-Owner-Token": "owner-secret", "X-AdaOS-Subnet-Id": "subnet:extra", "X-AdaOS-Zone": "lab-b"}

    targets = client.get("/v1/root/mcp/targets", headers=headers)
    assert targets.status_code == 200
    target_items = targets.json()["targets"]
    assert any(item["target_id"] == "hub:test-extra" for item in target_items)
    assert all(item.get("subnet_id") == "subnet:extra" for item in target_items)

    target = client.get("/v1/root/mcp/targets/hub:test-extra", headers=headers)
    assert target.status_code == 200
    assert target.json()["target"]["target_id"] == "hub:test-extra"


def test_root_mcp_owner_can_register_target_and_issue_scoped_access_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import targets as target_registry
    from adaos.services.root_mcp import tokens as token_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(token_registry, "_tokens_path", lambda: tmp_path / "access_tokens.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}

    register = client.post(
        "/v1/root/mcp/targets",
        headers=owner_headers,
        json={
            "target_id": "hub:test-extra",
            "title": "Extra Test Hub",
            "kind": "hub",
            "environment": "test",
            "status": "online",
            "zone": "lab-b",
            "subnet_id": "subnet:extra",
            "operational_surface": {"published_by": "skill:infra_access_skill", "enabled": True},
        },
    )
    assert register.status_code == 200
    assert register.json()["target"]["target_id"] == "hub:test-extra"

    issued = client.post(
        "/v1/root/mcp/access-tokens",
        headers=owner_headers,
        json={
            "audience": "codex-vscode",
            "target_id": "hub:test-extra",
            "note": "scoped external client",
        },
    )
    assert issued.status_code == 200
    token_payload = issued.json()["token"]
    assert token_payload["subnet_id"] == "subnet:extra"
    assert token_payload["zone"] == "lab-b"
    assert token_payload["target_ids"] == ["hub:test-extra"]
    assert "development.read.descriptors" in token_payload["capabilities"]

    token_headers = {"Authorization": f"Bearer {token_payload['access_token']}"}

    descriptors = client.get("/v1/root/mcp/descriptors", headers=token_headers)
    assert descriptors.status_code == 200

    targets = client.get("/v1/root/mcp/targets", headers=token_headers)
    assert targets.status_code == 200
    target_items = targets.json()["targets"]
    assert [item["target_id"] for item in target_items] == ["hub:test-extra"]
    assert targets.json()["scope"]["subnet_id"] == "subnet:extra"
    assert targets.json()["scope"]["zone"] == "lab-b"

    target = client.get("/v1/root/mcp/targets/hub:test-extra", headers=token_headers)
    assert target.status_code == 200
    assert target.json()["target"]["target_id"] == "hub:test-extra"

    call = client.post(
        "/v1/root/mcp/call",
        headers=token_headers,
        json={
            "tool_id": "development.get_descriptor_set",
            "arguments": {"descriptor_id": "mcp_client_profile"},
        },
    )
    assert call.status_code == 200
    assert call.json()["ok"] is True


def test_root_mcp_control_reports_enable_operational_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import targets as target_registry
    from adaos.services.root_mcp import tokens as token_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")
    monkeypatch.setattr(token_registry, "_tokens_path", lambda: tmp_path / "access_tokens.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-1"

    report = client.post(
        "/v1/hub/control/report",
        json={
            "target_id": target_id,
            "title": "Test Hub 1",
            "subnet_id": "subnet-test-1",
            "role": "hub",
            "environment": "test",
            "zone": "lab-b",
            "reported_at": "2026-04-07T12:00:00Z",
            "lifecycle": {
                "node_state": "running",
                "reason": "healthy",
                "draining": False,
                "accepting_new_work": True,
            },
            "root_control": {
                "status": "ok",
                "summary": "connected",
            },
            "route": {
                "status": "ok",
                "summary": "root-proxy",
            },
            "transport": {
                "requested_transport": "root_proxy",
                "effective_transport": "root_proxy",
                "selected_server": "root-a",
                "assessment_state": "ok",
            },
            "runtime": {
                "active_slot": "slot-a",
                "git_commit": "abcdef123456",
                "target_rev": "refs/heads/main",
            },
            "operational_surface": {
                "published_by": "skill:infra_access_skill",
                "enabled": True,
                "availability": "enabled",
                "capabilities": [
                    "hub.get_status",
                    "hub.get_runtime_summary",
                    "hub.issue_access_token",
                ],
            },
        },
    )
    assert report.status_code == 200
    report_payload = report.json()
    assert report_payload["ok"] is True
    assert report_payload["duplicate"] is False
    assert report_payload["target_id"] == target_id
    assert report_payload["auth"]["method"] == "hub_control_report_unverified"

    reports = client.get(
        "/v1/hubs/control/reports",
        headers={**owner_headers, "X-AdaOS-Subnet-Id": "subnet-test-1", "X-AdaOS-Zone": "lab-b"},
        params={"hub_id": target_id},
    )
    assert reports.status_code == 200
    report_items = reports.json()["reports"]
    assert len(report_items) == 1
    assert report_items[0]["target"]["target_id"] == target_id

    status_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_status",
            "arguments": {"target_id": target_id},
        },
    )
    assert status_call.status_code == 200
    status_payload = status_call.json()
    assert status_payload["ok"] is True
    assert status_payload["response"]["result"]["target"]["target_id"] == target_id
    assert status_payload["response"]["result"]["lifecycle"]["node_state"] == "running"

    runtime_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_runtime_summary",
            "arguments": {"target_id": target_id},
        },
    )
    assert runtime_call.status_code == 200
    runtime_payload = runtime_call.json()
    assert runtime_payload["ok"] is True
    assert runtime_payload["response"]["result"]["runtime"]["active_slot"] == "slot-a"
    assert runtime_payload["response"]["result"]["transport"]["effective_transport"] == "root_proxy"

    token_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.issue_access_token",
            "arguments": {"target_id": target_id, "audience": "codex-vscode"},
        },
    )
    assert token_call.status_code == 200
    token_payload = token_call.json()
    assert token_payload["ok"] is True
    assert token_payload["response"]["result"]["target_ids"] == [target_id]
    assert token_payload["response"]["result"]["subnet_id"] == "subnet-test-1"
    assert token_payload["response"]["result"]["zone"] == "lab-b"


def test_root_mcp_access_token_rejects_scope_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import targets as target_registry
    from adaos.services.root_mcp import tokens as token_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(token_registry, "_tokens_path", lambda: tmp_path / "access_tokens.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    client.post(
        "/v1/root/mcp/targets",
        headers=owner_headers,
        json={
            "target_id": "hub:test-extra",
            "title": "Extra Test Hub",
            "kind": "hub",
            "environment": "test",
            "status": "online",
            "zone": "lab-b",
            "subnet_id": "subnet:extra",
        },
    )
    issued = client.post(
        "/v1/root/mcp/access-tokens",
        headers=owner_headers,
        json={"audience": "codex-vscode", "target_id": "hub:test-extra"},
    )
    token = issued.json()["token"]["access_token"]

    resp = client.get(
        "/v1/root/mcp/foundation",
        headers={
            "Authorization": f"Bearer {token}",
            "X-AdaOS-Zone": "other-zone",
        },
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "zone_mismatch"


def test_root_mcp_placeholder_tool_returns_structured_error(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    client = _make_client()
    headers = {"X-Owner-Token": "owner-secret"}
    targets = client.get("/v1/root/mcp/targets", headers=headers)
    assert targets.status_code == 200
    target_id = targets.json()["targets"][0]["target_id"]

    resp = client.post(
        "/v1/root/mcp/call",
        headers=headers,
        json={
            "tool_id": "hub.get_logs",
            "request_id": "req-root-mcp-2",
            "arguments": {"target_id": target_id, "tail": 50},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    envelope = payload["response"]
    assert envelope["tool_id"] == "hub.get_logs"
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


def test_root_mcp_bearer_is_read_only_by_default(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_ROOT_BEARER_TOKEN", "bearer-secret")
    client = _make_client()
    headers = {"Authorization": "Bearer bearer-secret"}

    descriptors = client.get("/v1/root/mcp/descriptors", headers=headers)
    assert descriptors.status_code == 200

    targets = client.get("/v1/root/mcp/targets", headers=headers)
    assert targets.status_code == 200
    target_id = targets.json()["targets"][0]["target_id"]

    resp = client.post(
        "/v1/root/mcp/call",
        headers=headers,
        json={
            "tool_id": "hub.deploy_ref",
            "request_id": "req-root-mcp-4",
            "arguments": {"target_id": target_id, "ref": "refs/heads/main"},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["response"]["error"]["code"] == "forbidden"
