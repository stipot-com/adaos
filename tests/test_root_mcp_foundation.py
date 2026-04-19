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
    assert foundation_payload["foundation"]["registries"]["access_token_registry"]["available"] is True
    assert foundation_payload["foundation"]["infra_access_skill"]["state"]["skill_name"] == "infra_access_skill"

    contracts = client.get("/v1/root/mcp/contracts", headers=scoped_headers)
    assert contracts.status_code == 200
    contract_items = contracts.json()["contracts"]
    contract_ids = {item["id"] for item in contract_items}
    assert "development.get_descriptor_set" in contract_ids
    assert "operations.list_contracts" in contract_ids
    assert "operations.list_managed_targets" in contract_ids
    assert "development.export_sdk" not in contract_ids
    assert "hub.get_operational_surface" in contract_ids
    assert "hub.get_activity_log" in contract_ids
    assert "hub.get_capability_usage_summary" in contract_ids
    assert "hub.list_access_tokens" in contract_ids
    assert "hub.revoke_access_token" in contract_ids
    get_logs = next(item for item in contract_items if item["id"] == "hub.get_logs")
    assert get_logs["availability"] == "enabled"
    assert get_logs["metadata"]["published_by"] == "skill:infra_access_skill"
    restart_service = next(item for item in contract_items if item["id"] == "hub.restart_service")
    assert restart_service["availability"] == "enabled"
    run_tests = next(item for item in contract_items if item["id"] == "hub.run_allowed_tests")
    assert run_tests["availability"] == "enabled"
    implemented = next(item for item in contract_items if item["id"] == "hub.get_status")
    assert implemented["availability"] == "enabled"
    assert implemented["metadata"]["published_by"] == "root"
    deploy_ref = next(item for item in contract_items if item["id"] == "hub.deploy_ref")
    assert deploy_ref["availability"] == "enabled"
    assert deploy_ref["metadata"]["published_by"] == "skill:infra_access_skill"
    rollback = next(item for item in contract_items if item["id"] == "hub.rollback_last_test_deploy")
    assert rollback["availability"] == "enabled"

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
    assert envelope["meta"]["trace"]["request"]["argument_keys"] == ["descriptor_id"]
    assert envelope["meta"]["trace"]["routing"]["mode"] == "root.descriptor_registry"
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
    event = next(item for item in events if item["event_id"] == envelope["audit_event_id"])
    assert event["execution_adapter"] == "root.descriptor_registry"
    assert event["meta"]["trace"]["request"]["argument_keys"] == ["descriptor_id"]


def test_infra_access_skill_surface_reads_config_and_webui(monkeypatch, tmp_path) -> None:
    from adaos.services.root_mcp import infra_access_skill

    skill_dir = tmp_path / "skills" / "infra_access_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "name: infra_access_skill",
                "version: 0.2.0",
                "entry: handlers/main.py",
                "description: Infra access surface",
                "dependencies: []",
                "events: {}",
                "tools: []",
                "exports: {}",
            ]
        ),
        encoding="utf-8",
    )
    (skill_dir / "config.json").write_text(
        '{"enabled": true, "execution_mode": "reported_only", "capabilities": ["hub.get_status"], "token_management": {"enabled": true, "issuer_mode": "root_mcp"}, "observability": {"enabled": true, "channels": ["root_mcp.audit"]}}',
        encoding="utf-8",
    )
    (skill_dir / "webui.json").write_text(
        '{"apps":[{"id":"infra_access_app","title":"Infra Access","launchModal":"infra_access_modal"}],"widgets":[{"id":"infra_access_tokens","type":"ui.list","title":"Tokens"}],"registry":{"modals":{"infra_access_modal":{"title":"Infra Access","schema":{"id":"infra_access_modal","layout":{"type":"single","areas":[{"id":"main","role":"main"}]},"widgets":[]}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(infra_access_skill, "resolve_skill_dir", lambda: skill_dir)
    monkeypatch.delenv("ADAOS_INFRA_ACCESS_CAPABILITIES", raising=False)
    monkeypatch.delenv("ADAOS_INFRA_ACCESS_SKILL_ENABLED", raising=False)
    monkeypatch.delenv("ADAOS_INFRA_ACCESS_EXECUTION_MODE", raising=False)

    surface = infra_access_skill.build_operational_surface()

    assert surface["enabled"] is True
    assert surface["skill"]["version"] == "0.2.0"
    assert surface["webui"]["available"] is True
    assert "infra_access_app" in surface["webui"]["app_ids"]
    assert surface["token_management"]["enabled"] is True
    assert surface["token_management"]["issuer_mode"] == "root_mcp"
    assert "hub.get_operational_surface" in surface["capabilities"]
    assert "hub.get_activity_log" in surface["capabilities"]
    assert "hub.get_capability_usage_summary" in surface["capabilities"]
    assert "hub.issue_access_token" in surface["capabilities"]
    assert "hub.list_access_tokens" in surface["capabilities"]
    assert "hub.revoke_access_token" in surface["capabilities"]
    assert surface["webui"]["data_sources"][0]["tool_id"] == "hub.get_operational_surface"
    assert any(item["tool_id"] == "hub.get_activity_log" for item in surface["webui"]["data_sources"])
    assert surface["observability"]["activity_tools"] == ["hub.get_activity_log", "hub.get_capability_usage_summary"]
    assert surface["token_management"]["manage_tools"] == [
        "hub.issue_access_token",
        "hub.list_access_tokens",
        "hub.revoke_access_token",
    ]
    assert surface["observability"]["channels"] == ["root_mcp.audit"]


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


def test_root_mcp_access_token_lifecycle_management(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import tokens as token_registry

    monkeypatch.setattr(token_registry, "_tokens_path", lambda: tmp_path / "access_tokens.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}

    issued = client.post(
        "/v1/root/mcp/access-tokens",
        headers=owner_headers,
        json={
            "audience": "web-client",
            "subnet_id": "subnet:web",
            "zone": "lab-ui",
            "note": "web management session",
        },
    )
    assert issued.status_code == 200
    token = issued.json()["token"]
    assert issued.json()["audit_event_id"]
    assert token["audience"] == "web-client"

    listed = client.get(
        "/v1/root/mcp/access-tokens",
        headers=owner_headers,
        params={"active_only": "true"},
    )
    assert listed.status_code == 200
    listed_tokens = listed.json()["tokens"]
    assert len(listed_tokens) == 1
    assert listed_tokens[0]["token_id"] == token["token_id"]
    assert "token_hash" not in listed_tokens[0]

    revoked = client.post(
        f"/v1/root/mcp/access-tokens/{token['token_id']}/revoke",
        headers=owner_headers,
        json={"reason": "rotate"},
    )
    assert revoked.status_code == 200
    revoked_payload = revoked.json()
    assert revoked_payload["token"]["status"] == "revoked"
    assert revoked_payload["audit_event_id"]

    denied = client.get(
        "/v1/root/mcp/descriptors",
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert denied.status_code == 401

    audit = client.get(
        "/v1/root/mcp/audit",
        headers=owner_headers,
        params={"tool_id": "root.access_tokens.revoke"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert events
    assert events[0]["tool_id"] == "root.access_tokens.revoke"

    issue_audit = client.get(
        "/v1/root/mcp/audit",
        headers=owner_headers,
        params={"tool_id": "root.access_tokens.issue"},
    )
    assert issue_audit.status_code == 200
    issue_events = issue_audit.json()["events"]
    assert issue_events
    assert issue_events[0]["redactions"] == ["result.access_token"]


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
    logs_dir = tmp_path / "base" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "adaos.log").write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    (logs_dir / "service.alpha.log").write_text("svc-1\nsvc-2\n", encoding="utf-8")

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
                "execution_mode": "local_process",
                "token_management": {
                    "enabled": True,
                    "issuer_mode": "root_mcp",
                    "web_client_ready": True,
                },
                "webui": {
                    "available": True,
                    "app_ids": ["infra_access_app"],
                    "widget_ids": ["infra_access_tokens"],
                },
                "observability": {
                    "enabled": True,
                    "channels": ["root_mcp.audit", "hub.control_report"],
                },
                "capabilities": [
                    "hub.get_status",
                    "hub.get_runtime_summary",
                    "hub.get_operational_surface",
                    "hub.get_activity_log",
                    "hub.get_capability_usage_summary",
                    "hub.get_logs",
                    "hub.run_healthchecks",
                    "hub.issue_access_token",
                    "hub.list_access_tokens",
                    "hub.revoke_access_token",
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
    assert report_payload["report_verified"] is False
    assert report_payload["audit_event_id"]

    reports = client.get(
        "/v1/hubs/control/reports",
        headers={**owner_headers, "X-AdaOS-Subnet-Id": "subnet-test-1", "X-AdaOS-Zone": "lab-b"},
        params={"hub_id": target_id},
    )
    assert reports.status_code == 200
    report_items = reports.json()["reports"]
    assert len(report_items) == 1
    assert report_items[0]["target"]["target_id"] == target_id
    assert report_items[0]["ingest_auth"]["method"] == "hub_control_report_unverified"

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
    assert status_payload["response"]["meta"]["routing_mode"] == "root.control_report_projection"
    assert status_payload["response"]["meta"]["target_surface_enabled"] is True

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
    assert runtime_payload["response"]["meta"]["report_verified"] is False

    surface_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_operational_surface",
            "arguments": {"target_id": target_id},
        },
    )
    assert surface_call.status_code == 200
    surface_payload = surface_call.json()
    assert surface_payload["ok"] is True
    assert surface_payload["response"]["result"]["operational_surface"]["published_by"] == "skill:infra_access_skill"
    assert surface_payload["response"]["result"]["token_management"]["enabled"] is True
    assert surface_payload["response"]["result"]["webui"]["available"] is True
    assert any(item["tool_id"] == "hub.get_activity_log" for item in surface_payload["response"]["result"]["operational_surface"]["webui"]["data_sources"])
    assert surface_payload["response"]["meta"]["routing_mode"] == "root.control_report_projection"

    activity_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_activity_log",
            "arguments": {"target_id": target_id, "limit": 10},
        },
    )
    assert activity_call.status_code == 200
    activity_payload = activity_call.json()
    assert activity_payload["ok"] is True
    assert activity_payload["response"]["meta"]["routing_mode"] == "root.audit_projection.activity"
    assert activity_payload["response"]["result"]["activity"]["items"]
    assert any(item["tool_id"] == "hub.control_report.ingest" for item in activity_payload["response"]["result"]["activity"]["items"])

    usage_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_capability_usage_summary",
            "arguments": {"target_id": target_id, "limit": 50},
        },
    )
    assert usage_call.status_code == 200
    usage_payload = usage_call.json()
    assert usage_payload["ok"] is True
    assert usage_payload["response"]["meta"]["routing_mode"] == "root.audit_projection.capability_usage"
    assert any(item["tool_id"] == "hub.control_report.ingest" for item in usage_payload["response"]["result"]["usage"]["tools"])

    logs_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_logs",
            "arguments": {"target_id": target_id, "tail": 2},
        },
    )
    assert logs_call.status_code == 200
    logs_payload = logs_call.json()
    assert logs_payload["ok"] is True
    assert logs_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.logs"
    assert logs_payload["response"]["result"]["logs"]["files"]
    assert any(item["name"] == "adaos.log" for item in logs_payload["response"]["result"]["logs"]["files"])

    healthchecks_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.run_healthchecks",
            "arguments": {"target_id": target_id},
        },
    )
    assert healthchecks_call.status_code == 200
    healthchecks_payload = healthchecks_call.json()
    assert healthchecks_payload["ok"] is True
    assert healthchecks_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.healthchecks"
    assert healthchecks_payload["response"]["result"]["healthchecks"]["checks"]
    assert healthchecks_payload["response"]["result"]["healthchecks"]["mode"] == "local_process"

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
    assert token_payload["response"]["result"]["target_id"] == target_id
    assert token_payload["response"]["result"]["issuer_mode"] == "root_mcp"
    assert token_payload["response"]["result"]["subnet_id"] == "subnet-test-1"
    assert token_payload["response"]["result"]["zone"] == "lab-b"
    assert token_payload["response"]["meta"]["routing_mode"] == "root.access_token_issuer"
    assert token_payload["response"]["meta"]["redactions"] == ["result.access_token"]

    issued_token_id = token_payload["response"]["result"]["token_id"]

    list_tokens_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.list_access_tokens",
            "arguments": {"target_id": target_id, "active_only": True},
        },
    )
    assert list_tokens_call.status_code == 200
    list_tokens_payload = list_tokens_call.json()
    assert list_tokens_payload["ok"] is True
    assert list_tokens_payload["response"]["meta"]["routing_mode"] == "root.access_token_registry"
    assert any(item["token_id"] == issued_token_id for item in list_tokens_payload["response"]["result"]["tokens"])


def test_root_memory_profile_reports_ingest_and_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import memory_reports as report_registry

    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "memory_profile_reports.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}

    report = client.post(
        "/v1/hub/memory_profile/report",
        json={
            "target_id": "hub:subnet-test-1",
            "subnet_id": "subnet-test-1",
            "zone": "lab-b",
            "reported_at": "2026-04-18T12:00:00Z",
            "_protocol": {"message_id": "mem-msg-1", "cursor": 3, "flow_id": "hub_root.memory_profile"},
            "session": {
                "session_id": "mem-001",
                "profile_mode": "trace_profile",
                "session_state": "finished",
                "suspected_leak": True,
                "artifact_refs": [{"artifact_id": "mem-001-final"}],
            },
            "operations_tail": [{"event": "tool_invoked"}],
            "telemetry_tail": [{"sampled_at": 1.0, "rss_growth_bytes": 64}],
        },
    )
    assert report.status_code == 200
    report_payload = report.json()
    assert report_payload["ok"] is True
    assert report_payload["duplicate"] is False
    assert report_payload["hub_id"] == "hub:subnet-test-1"
    assert report_payload["session_id"] == "mem-001"
    assert report_payload["audit_event_id"]

    reports = client.get(
        "/v1/hubs/memory_profile/reports",
        headers={**owner_headers, "X-AdaOS-Subnet-Id": "subnet-test-1", "X-AdaOS-Zone": "lab-b"},
        params={"hub_id": "hub:subnet-test-1", "session_id": "mem-001"},
    )
    assert reports.status_code == 200
    payload = reports.json()
    assert payload["ok"] is True
    assert payload["scope"]["subnet_id"] == "subnet-test-1"
    assert len(payload["reports"]) == 1
    stored = payload["reports"][0]
    assert stored["session_id"] == "mem-001"
    assert stored["hub_id"] == "hub:subnet-test-1"
    assert stored["report"]["session"]["profile_mode"] == "trace_profile"
    assert stored["report"]["telemetry_tail"][0]["rss_growth_bytes"] == 64

    filtered = client.get(
        "/v1/hubs/memory_profile/reports",
        headers={**owner_headers, "X-AdaOS-Subnet-Id": "subnet-test-1", "X-AdaOS-Zone": "lab-b"},
        params={"hub_id": "hub:subnet-test-1", "state": "finished", "suspected_only": "true"},
    )
    assert filtered.status_code == 200
    assert len(filtered.json()["reports"]) == 1

    report_item = client.get(
        "/v1/hubs/memory_profile/reports/mem-001",
        headers={**owner_headers, "X-AdaOS-Subnet-Id": "subnet-test-1", "X-AdaOS-Zone": "lab-b"},
    )
    assert report_item.status_code == 200
    report_payload = report_item.json()["report"]
    assert report_payload["session_id"] == "mem-001"
    assert report_payload["report"]["session"]["session_state"] == "finished"


def test_root_mcp_local_execution_write_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import service as root_mcp_service
    from adaos.services.root_mcp import targets as target_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")

    def fake_restart_local_service(*, service: str, allowed_services: list[str] | None = None):
        assert service == "alpha"
        assert allowed_services == ["alpha"]
        return {"mode": "local_process", "service": service, "status": {"running": True, "health_ok": True}}

    def fake_run_allowed_tests(*, target_id: str, allowed_test_paths: list[str] | None = None, requested_tests: list[str] | None = None, timeout_seconds: int = 120):
        assert target_id == "hub:subnet-test-5"
        assert allowed_test_paths == ["tests/test_sample_dummy.py"]
        assert requested_tests == ["tests/test_sample_dummy.py"]
        return {
            "target_id": target_id,
            "mode": "local_process",
            "selected_tests": requested_tests,
            "allowed_tests": allowed_test_paths,
            "status": "passed",
            "exit_code": 0,
        }

    def fake_read_test_results(*, target_id: str):
        assert target_id == "hub:subnet-test-5"
        return {"available": True, "target_id": target_id, "result": {"status": "passed", "selected_tests": ["tests/test_sample_dummy.py"]}}

    monkeypatch.setattr(root_mcp_service, "restart_local_service", fake_restart_local_service)
    monkeypatch.setattr(root_mcp_service, "run_allowed_tests", fake_run_allowed_tests)
    monkeypatch.setattr(root_mcp_service, "read_test_results", fake_read_test_results)

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-5"

    report = client.post(
        "/v1/hub/control/report",
        json={
            "target_id": target_id,
            "subnet_id": "subnet-test-5",
            "environment": "test",
            "zone": "lab-f",
            "reported_at": "2026-04-07T12:20:00Z",
            "lifecycle": {"node_state": "running"},
            "operational_surface": {
                "published_by": "skill:infra_access_skill",
                "enabled": True,
                "availability": "enabled",
                "execution_mode": "local_process",
                "allowed_services": ["alpha"],
                "allowed_test_paths": ["tests/test_sample_dummy.py"],
                "allowed_deploy_refs": ["refs/heads/test-main", "refs/tags/v1"],
                "capabilities": [
                    "hub.restart_service",
                    "hub.run_allowed_tests",
                    "hub.get_test_results",
                    "hub.deploy_ref",
                    "hub.rollback_last_test_deploy",
                ],
            },
        },
    )
    assert report.status_code == 200

    restart_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.restart_service",
            "arguments": {"target_id": target_id, "service": "alpha"},
        },
    )
    assert restart_call.status_code == 200
    restart_payload = restart_call.json()
    assert restart_payload["ok"] is True
    assert restart_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.service_restart"

    run_tests_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.run_allowed_tests",
            "arguments": {"target_id": target_id, "tests": ["tests/test_sample_dummy.py"]},
        },
    )
    assert run_tests_call.status_code == 200
    run_tests_payload = run_tests_call.json()
    assert run_tests_payload["ok"] is True
    assert run_tests_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.pytest"
    assert run_tests_payload["response"]["result"]["tests"]["status"] == "passed"

    get_results_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_test_results",
            "arguments": {"target_id": target_id},
        },
    )
    assert get_results_call.status_code == 200
    get_results_payload = get_results_call.json()
    assert get_results_payload["ok"] is True
    assert get_results_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.test_results"
    assert get_results_payload["response"]["result"]["test_results"]["available"] is True

    deploy_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.deploy_ref",
            "arguments": {"target_id": target_id, "ref": "refs/heads/test-main", "note": "pilot deploy"},
        },
    )
    assert deploy_call.status_code == 200
    deploy_payload = deploy_call.json()
    assert deploy_payload["ok"] is True
    assert deploy_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.deploy_ref"
    assert deploy_payload["response"]["result"]["deployment"]["state"]["current_ref"] == "refs/heads/test-main"
    assert deploy_payload["response"]["meta"]["trace"]["result"]["kind"] == "object"

    rollback_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.rollback_last_test_deploy",
            "arguments": {"target_id": target_id},
        },
    )
    assert rollback_call.status_code == 200
    rollback_payload = rollback_call.json()
    assert rollback_payload["ok"] is False
    assert rollback_payload["response"]["error"]["code"] == "invalid_request"

    second_deploy_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.deploy_ref",
            "arguments": {"target_id": target_id, "ref": "refs/tags/v1"},
        },
    )
    assert second_deploy_call.status_code == 200
    second_deploy_payload = second_deploy_call.json()
    assert second_deploy_payload["ok"] is True

    rollback_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.rollback_last_test_deploy",
            "arguments": {"target_id": target_id},
        },
    )
    assert rollback_call.status_code == 200
    rollback_payload = rollback_call.json()
    assert rollback_payload["ok"] is True
    assert rollback_payload["response"]["meta"]["routing_mode"] == "infra_access.local_process.rollback"
    assert rollback_payload["response"]["result"]["deployment"]["state"]["current_ref"] == "refs/heads/test-main"


def test_root_mcp_operational_tool_requires_published_skill_capability(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import targets as target_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-2"

    report = client.post(
        "/v1/hub/control/report",
        json={
            "target_id": target_id,
            "subnet_id": "subnet-test-2",
            "environment": "test",
            "zone": "lab-c",
            "reported_at": "2026-04-07T12:05:00Z",
            "lifecycle": {"node_state": "running"},
            "operational_surface": {
                "published_by": "skill:infra_access_skill",
                "enabled": True,
                "availability": "enabled",
                "capabilities": ["hub.get_status"],
            },
        },
    )
    assert report.status_code == 200

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
    assert runtime_payload["ok"] is False
    assert runtime_payload["response"]["error"]["code"] == "capability_not_published"


def test_root_mcp_local_execution_tools_require_local_process_route(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import targets as target_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-4"

    report = client.post(
        "/v1/hub/control/report",
        json={
            "target_id": target_id,
            "subnet_id": "subnet-test-4",
            "environment": "test",
            "zone": "lab-e",
            "reported_at": "2026-04-07T12:15:00Z",
            "lifecycle": {"node_state": "running"},
            "operational_surface": {
                "published_by": "skill:infra_access_skill",
                "enabled": True,
                "availability": "enabled",
                "execution_mode": "reported_only",
                "capabilities": ["hub.get_logs", "hub.run_healthchecks"],
            },
        },
    )
    assert report.status_code == 200

    logs_call = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_logs",
            "arguments": {"target_id": target_id, "tail": 10},
        },
    )
    assert logs_call.status_code == 200
    logs_payload = logs_call.json()
    assert logs_payload["ok"] is False
    assert logs_payload["response"]["error"]["code"] == "execution_route_unavailable"


def test_root_mcp_token_tools_require_target_token_management(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import targets as target_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-4b"

    report = client.post(
        "/v1/hub/control/report",
        json={
            "target_id": target_id,
            "subnet_id": "subnet-test-4b",
            "environment": "test",
            "zone": "lab-e",
            "reported_at": "2026-04-07T12:16:00Z",
            "lifecycle": {"node_state": "running"},
            "operational_surface": {
                "published_by": "skill:infra_access_skill",
                "enabled": True,
                "availability": "enabled",
                "capabilities": ["hub.issue_access_token", "hub.list_access_tokens", "hub.revoke_access_token"],
            },
        },
    )
    assert report.status_code == 200

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
    assert token_payload["ok"] is False
    assert token_payload["response"]["error"]["code"] == "token_management_unavailable"


def test_root_mcp_can_require_verified_control_reports(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_ROOT_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("ADAOS_ROOT_HUB_REPORT_TOKEN", "hub-report-secret")
    monkeypatch.setenv("ADAOS_ROOT_MCP_REQUIRE_VERIFIED_REPORTS", "1")
    from adaos.services.root_mcp import reports as report_registry
    from adaos.services.root_mcp import targets as target_registry

    monkeypatch.setattr(target_registry, "_registry_path", lambda: tmp_path / "managed_targets.json")
    monkeypatch.setattr(report_registry, "_reports_path", lambda: tmp_path / "control_reports.json")

    client = _make_client()
    owner_headers = {"X-Owner-Token": "owner-secret"}
    target_id = "hub:subnet-test-3"
    base_report = {
        "target_id": target_id,
        "subnet_id": "subnet-test-3",
        "environment": "test",
        "zone": "lab-d",
        "reported_at": "2026-04-07T12:10:00Z",
        "lifecycle": {"node_state": "running"},
        "operational_surface": {
            "published_by": "skill:infra_access_skill",
            "enabled": True,
            "availability": "enabled",
            "capabilities": ["hub.get_status"],
        },
    }

    unverified = client.post("/v1/hub/control/report", json=base_report)
    assert unverified.status_code == 200
    assert unverified.json()["report_verified"] is False

    denied = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_status",
            "arguments": {"target_id": target_id},
        },
    )
    assert denied.status_code == 200
    denied_payload = denied.json()
    assert denied_payload["ok"] is False
    assert denied_payload["response"]["error"]["code"] == "report_unverified"

    verified = client.post(
        "/v1/hub/control/report",
        headers={"X-AdaOS-Hub-Report-Token": "hub-report-secret"},
        json={**base_report, "reported_at": "2026-04-07T12:11:00Z"},
    )
    assert verified.status_code == 200
    assert verified.json()["auth"]["method"] == "hub_report_token"
    assert verified.json()["report_verified"] is True

    allowed = client.post(
        "/v1/root/mcp/call",
        headers=owner_headers,
        json={
            "tool_id": "hub.get_status",
            "arguments": {"target_id": target_id},
        },
    )
    assert allowed.status_code == 200
    allowed_payload = allowed.json()
    assert allowed_payload["ok"] is True
    assert allowed_payload["response"]["meta"]["report_verified"] is True


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
            "tool_id": "hub.deploy_ref",
            "request_id": "req-root-mcp-2",
            "arguments": {"target_id": target_id, "ref": "refs/heads/main"},
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is False
    envelope = payload["response"]
    assert envelope["tool_id"] == "hub.deploy_ref"
    assert envelope["status"] == "error"
    assert envelope["error"]["code"] == "capability_not_published"
    assert "hub.deploy_ref" not in envelope["meta"]["published_capabilities"]


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
