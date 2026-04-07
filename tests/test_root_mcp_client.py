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
    client.list_managed_targets(environment="test")
    client.call("development.list_descriptor_sets", request_id="req-1")

    assert config.headers()["Authorization"] == "Bearer access-123"
    assert config.headers()["X-AdaOS-Subnet-Id"] == "subnet:test-zone"
    assert config.headers()["X-AdaOS-Zone"] == "lab-a"
    assert stub.calls[0][1] == "/v1/root/mcp/foundation"
    assert stub.calls[1][2]["params"]["environment"] == "test"
    assert stub.calls[2][2]["json"]["tool_id"] == "development.list_descriptor_sets"
