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
    client.list_descriptors()
    client.get_descriptor("capability_registry")
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
    client.deploy_target_ref("hub:test-zone", ref="refs/heads/test-main", note="pilot")
    client.rollback_last_test_deploy("hub:test-zone")
    client.call("development.list_descriptor_sets", request_id="req-1")

    assert config.headers()["Authorization"] == "Bearer access-123"
    assert config.headers()["X-AdaOS-Subnet-Id"] == "subnet:test-zone"
    assert config.headers()["X-AdaOS-Zone"] == "lab-a"
    assert stub.calls[0][1] == "/v1/root/mcp/foundation"
    assert stub.calls[1][1] == "/v1/root/mcp/descriptors"
    assert stub.calls[2][1] == "/v1/root/mcp/descriptors/capability_registry"
    assert stub.calls[3][2]["params"]["environment"] == "test"
    assert stub.calls[4][1] == "/v1/root/mcp/targets"
    assert stub.calls[4][2]["json"]["target_id"] == "hub:test-zone"
    assert stub.calls[5][1] == "/v1/root/mcp/targets/hub:test-zone"
    assert stub.calls[6][1] == "/v1/root/mcp/access-tokens"
    assert stub.calls[6][2]["json"]["audience"] == "codex-vscode"
    assert stub.calls[7][1] == "/v1/root/mcp/access-tokens"
    assert stub.calls[7][2]["params"]["active_only"] is True
    assert stub.calls[8][1] == "/v1/root/mcp/access-tokens/tok-1/revoke"
    assert stub.calls[8][2]["json"]["reason"] == "rotate"
    assert stub.calls[9][1] == "/v1/root/mcp/sessions"
    assert stub.calls[9][2]["json"]["target_id"] == "hub:test-zone"
    assert stub.calls[10][1] == "/v1/root/mcp/sessions"
    assert stub.calls[10][2]["params"]["capability_profile"] == "ProfileOpsRead"
    assert stub.calls[11][1] == "/v1/root/mcp/sessions/sess-1"
    assert stub.calls[12][1] == "/v1/root/mcp/sessions/sess-1/revoke"
    assert stub.calls[12][2]["json"]["reason"] == "rotate-session"
    assert stub.calls[13][2]["json"]["tool_id"] == "hub.get_operational_surface"
    assert stub.calls[13][2]["json"]["arguments"]["target_id"] == "hub:test-zone"
    assert stub.calls[14][2]["json"]["tool_id"] == "hub.get_activity_log"
    assert stub.calls[14][2]["json"]["arguments"]["errors_only"] is True
    assert stub.calls[15][2]["json"]["tool_id"] == "hub.get_capability_usage_summary"
    assert stub.calls[15][2]["json"]["arguments"]["limit"] == 150
    assert stub.calls[16][2]["json"]["tool_id"] == "hub.issue_access_token"
    assert stub.calls[16][2]["json"]["arguments"]["note"] == "web-client"
    assert stub.calls[17][2]["json"]["tool_id"] == "hub.list_access_tokens"
    assert stub.calls[17][2]["json"]["arguments"]["active_only"] is True
    assert stub.calls[18][2]["json"]["tool_id"] == "hub.revoke_access_token"
    assert stub.calls[18][2]["json"]["arguments"]["token_id"] == "tok-2"
    assert stub.calls[19][2]["json"]["tool_id"] == "hub.deploy_ref"
    assert stub.calls[19][2]["json"]["arguments"]["ref"] == "refs/heads/test-main"
    assert stub.calls[20][2]["json"]["tool_id"] == "hub.rollback_last_test_deploy"
    assert stub.calls[21][2]["json"]["tool_id"] == "development.list_descriptor_sets"
