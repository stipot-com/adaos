from __future__ import annotations

from adaos.services.root_mcp.client import RootMcpClient, RootMcpClientConfig


class _StubRootHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        return {"ok": True, "path": path}


def test_root_mcp_client_uses_root_url_scope_and_bearer_headers() -> None:
    stub = _StubRootHttpClient()
    config = RootMcpClientConfig(
        root_url="https://root.example.test",
        subnet_id="subnet:test-zone",
        access_token="access-123",
        zone="lab-a",
    )
    client = RootMcpClient(config=config, http=stub)  # type: ignore[arg-type]

    client.foundation()
    client.list_contracts(plane_id="profile_ops")
    client.list_planes()
    client.get_plane("profile_ops")
    client.list_descriptors()
    client.get_descriptor("capability_registry")
    client.get_adaos_dev_architecture_catalog()
    client.get_adaos_dev_sdk_metadata(level="mini")
    client.get_adaos_dev_template_catalog()
    client.get_adaos_dev_public_skill_registry()
    client.get_adaos_dev_public_scenario_registry()
    client.get_profileops_status("hub:test-zone")
    client.list_profileops_sessions("hub:test-zone", state="finished", suspected_only=True)
    client.get_profileops_session("hub:test-zone", "mem-001")
    client.list_profileops_incidents("hub:test-zone")
    client.list_profileops_artifacts("hub:test-zone", "mem-001")
    client.get_profileops_artifact("hub:test-zone", "mem-001", "art-1", max_bytes=1024)
    client.start_profileops_session("hub:test-zone", profile_mode="trace_profile")
    client.stop_profileops_session("hub:test-zone", "mem-001")
    client.retry_profileops_session("hub:test-zone", "mem-001")
    client.publish_profileops_session("hub:test-zone", "mem-001")
    client.list_managed_targets(environment="test")
    client.upsert_managed_target({"target_id": "hub:test-zone"})
    client.get_managed_target("hub:test-zone")
    client.issue_access_token({"audience": "codex-vscode"})
    client.list_access_tokens(active_only=True)
    client.revoke_access_token("tok-1", reason="rotate")
    client.issue_session_lease({"audience": "codex-vscode", "target_id": "hub:test-zone"})
    client.list_session_leases(active_only=True, capability_profile="ProfileOpsRead")
    client.get_session_lease("sess-1")
    client.revoke_session_lease("sess-1", reason="rotate-session")
    client.get_operational_surface("hub:test-zone")
    client.get_target_activity_log("hub:test-zone", limit=25, errors_only=True)
    client.get_target_capability_usage_summary("hub:test-zone", limit=150)
    client.issue_target_access_token("hub:test-zone", audience="codex-vscode", note="web-client")
    client.list_target_access_tokens("hub:test-zone", active_only=True)
    client.revoke_target_access_token("hub:test-zone", "tok-2", reason="rotate-target")
    client.issue_target_mcp_session("hub:test-zone", audience="codex-vscode", capability_profile="ProfileOpsRead")
    client.list_target_mcp_sessions("hub:test-zone", active_only=True, capability_profile="ProfileOpsRead")
    client.revoke_target_mcp_session("hub:test-zone", "sess-2", reason="rotate-target-session")
    client.deploy_target_ref("hub:test-zone", ref="refs/heads/test-main", note="pilot")
    client.rollback_last_test_deploy("hub:test-zone")
    client.get_yjs_load_mark_history(limit=25, webspace_id="desktop", kind="owner", bucket_id="_by_owner/unknown", status="high")
    client.call("development.list_descriptor_sets", request_id="req-1")

    assert config.headers()["Authorization"] == "Bearer access-123"
    assert config.headers()["X-AdaOS-Subnet-Id"] == "subnet:test-zone"
    assert config.headers()["X-AdaOS-Zone"] == "lab-a"
    assert stub.calls[0][1] == "/v1/root/mcp/foundation"
    assert stub.calls[1][1] == "/v1/root/mcp/contracts"
    assert stub.calls[1][2]["params"]["plane_id"] == "profile_ops"
    assert stub.calls[2][2]["json"]["tool_id"] == "development.list_planes"
    assert stub.calls[3][2]["json"]["tool_id"] == "development.get_plane"
    assert stub.calls[3][2]["json"]["arguments"]["plane_id"] == "profile_ops"
    assert stub.calls[4][1] == "/v1/root/mcp/descriptors"
    assert stub.calls[5][1] == "/v1/root/mcp/descriptors/capability_registry"
    assert stub.calls[6][2]["json"]["tool_id"] == "adaos_dev.get_architecture_catalog"
    assert stub.calls[7][2]["json"]["tool_id"] == "adaos_dev.get_sdk_metadata"
    assert stub.calls[7][2]["json"]["arguments"]["level"] == "mini"
    assert stub.calls[8][2]["json"]["tool_id"] == "adaos_dev.get_template_catalog"
    assert stub.calls[9][2]["json"]["tool_id"] == "adaos_dev.get_public_skill_registry"
    assert stub.calls[10][2]["json"]["tool_id"] == "adaos_dev.get_public_scenario_registry"
    assert stub.calls[11][2]["json"]["tool_id"] == "hub.memory.get_status"
    assert stub.calls[12][2]["json"]["tool_id"] == "hub.memory.list_sessions"
    assert stub.calls[12][2]["json"]["arguments"]["state"] == "finished"
    assert stub.calls[13][2]["json"]["tool_id"] == "hub.memory.get_session"
    assert stub.calls[13][2]["json"]["arguments"]["session_id"] == "mem-001"
    assert stub.calls[14][2]["json"]["tool_id"] == "hub.memory.list_incidents"
    assert stub.calls[15][2]["json"]["tool_id"] == "hub.memory.list_artifacts"
    assert stub.calls[16][2]["json"]["tool_id"] == "hub.memory.get_artifact"
    assert stub.calls[16][2]["json"]["arguments"]["artifact_id"] == "art-1"
    assert stub.calls[17][2]["json"]["tool_id"] == "hub.memory.start_profile"
    assert stub.calls[17][2]["json"]["arguments"]["profile_mode"] == "trace_profile"
    assert stub.calls[18][2]["json"]["tool_id"] == "hub.memory.stop_profile"
    assert stub.calls[18][2]["json"]["arguments"]["session_id"] == "mem-001"
    assert stub.calls[19][2]["json"]["tool_id"] == "hub.memory.retry_profile"
    assert stub.calls[20][2]["json"]["tool_id"] == "hub.memory.publish_profile"
    assert stub.calls[21][2]["params"]["environment"] == "test"
    assert stub.calls[22][1] == "/v1/root/mcp/targets"
    assert stub.calls[22][2]["json"]["target_id"] == "hub:test-zone"
    assert stub.calls[23][1] == "/v1/root/mcp/targets/hub:test-zone"
    assert stub.calls[24][1] == "/v1/root/mcp/access-tokens"
    assert stub.calls[24][2]["json"]["audience"] == "codex-vscode"
    assert stub.calls[25][1] == "/v1/root/mcp/access-tokens"
    assert stub.calls[25][2]["params"]["active_only"] is True
    assert stub.calls[26][1] == "/v1/root/mcp/access-tokens/tok-1/revoke"
    assert stub.calls[26][2]["json"]["reason"] == "rotate"
    assert stub.calls[27][1] == "/v1/root/mcp/sessions"
    assert stub.calls[27][2]["json"]["target_id"] == "hub:test-zone"
    assert stub.calls[28][1] == "/v1/root/mcp/sessions"
    assert stub.calls[28][2]["params"]["capability_profile"] == "ProfileOpsRead"
    assert stub.calls[29][1] == "/v1/root/mcp/sessions/sess-1"
    assert stub.calls[30][1] == "/v1/root/mcp/sessions/sess-1/revoke"
    assert stub.calls[30][2]["json"]["reason"] == "rotate-session"
    assert stub.calls[31][2]["json"]["tool_id"] == "hub.get_operational_surface"
    assert stub.calls[31][2]["json"]["arguments"]["target_id"] == "hub:test-zone"
    assert stub.calls[32][2]["json"]["tool_id"] == "hub.get_activity_log"
    assert stub.calls[32][2]["json"]["arguments"]["errors_only"] is True
    assert stub.calls[33][2]["json"]["tool_id"] == "hub.get_capability_usage_summary"
    assert stub.calls[33][2]["json"]["arguments"]["limit"] == 150
    assert stub.calls[34][2]["json"]["tool_id"] == "hub.issue_access_token"
    assert stub.calls[34][2]["json"]["arguments"]["note"] == "web-client"
    assert stub.calls[35][2]["json"]["tool_id"] == "hub.list_access_tokens"
    assert stub.calls[35][2]["json"]["arguments"]["active_only"] is True
    assert stub.calls[36][2]["json"]["tool_id"] == "hub.revoke_access_token"
    assert stub.calls[36][2]["json"]["arguments"]["token_id"] == "tok-2"
    assert stub.calls[37][2]["json"]["tool_id"] == "hub.issue_mcp_session"
    assert stub.calls[37][2]["json"]["arguments"]["capability_profile"] == "ProfileOpsRead"
    assert stub.calls[38][2]["json"]["tool_id"] == "hub.list_mcp_sessions"
    assert stub.calls[38][2]["json"]["arguments"]["active_only"] is True
    assert stub.calls[39][2]["json"]["tool_id"] == "hub.revoke_mcp_session"
    assert stub.calls[39][2]["json"]["arguments"]["session_id"] == "sess-2"
    assert stub.calls[40][2]["json"]["tool_id"] == "hub.deploy_ref"
    assert stub.calls[40][2]["json"]["arguments"]["ref"] == "refs/heads/test-main"
    assert stub.calls[41][2]["json"]["tool_id"] == "hub.rollback_last_test_deploy"
    assert stub.calls[42][1] == "/v1/root/mcp/yjs/load-mark/history"
    assert stub.calls[42][2]["params"]["webspace_id"] == "desktop"
    assert stub.calls[42][2]["params"]["kind"] == "owner"
    assert stub.calls[42][2]["params"]["bucket_id"] == "_by_owner/unknown"
    assert stub.calls[42][2]["params"]["status"] == "high"
    assert stub.calls[43][2]["json"]["tool_id"] == "development.list_descriptor_sets"
