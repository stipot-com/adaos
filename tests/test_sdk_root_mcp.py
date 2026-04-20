from __future__ import annotations

from adaos.sdk.data import root_mcp as sdk_root_mcp


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def foundation(self) -> dict:
        self.calls.append(("foundation", {}))
        return {
            "result": {
                "client": {
                    "preferred_bootstrap": "root-issued MCP Session Lease",
                }
            }
        }

    def get_descriptor(self, descriptor_id: str, *, level: str = "std") -> dict:
        self.calls.append(("get_descriptor", {"descriptor_id": descriptor_id, "level": level}))
        return {
            "result": {
                "session_registry": {
                    "capability_profiles": ["ProfileOpsRead", "ProfileOpsControl"],
                }
            }
        }

    def list_access_tokens(self, **kwargs) -> dict:
        self.calls.append(("list_access_tokens", dict(kwargs)))
        return {"result": {"tokens": []}}

    def list_session_leases(self, **kwargs) -> dict:
        self.calls.append(("list_session_leases", dict(kwargs)))
        return {"result": {"sessions": []}}

    def recent_audit(self, **kwargs) -> dict:
        self.calls.append(("recent_audit", dict(kwargs)))
        return {"result": {"events": []}}

    def issue_session_lease(self, payload: dict) -> dict:
        self.calls.append(("issue_session_lease", dict(payload)))
        return {"result": {"session_id": "sess-1", "access_token": "secret"}}


def test_sdk_root_mcp_prefers_rest_surfaces(monkeypatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(
        sdk_root_mcp,
        "get_local_target_context",
        lambda **kwargs: {
            "root_url": "https://root.test",
            "target_id": "hub:test-subnet",
            "subnet_id": "subnet:test-subnet",
            "zone": "lab-a",
        },
    )
    monkeypatch.setattr(sdk_root_mcp, "get_management_client", lambda **kwargs: stub)

    surface = sdk_root_mcp.get_local_operational_surface()
    tokens = sdk_root_mcp.list_local_access_tokens(active_only=True)
    sessions = sdk_root_mcp.list_local_mcp_sessions(active_only=True)
    audit = sdk_root_mcp.get_local_activity_log(limit=7)
    issued = sdk_root_mcp.issue_local_codex_mcp_session(capability_profile="ProfileOpsRead", ttl_seconds=600)

    assert surface["response"]["result"]["operational_surface"]["token_management"]["session_capability_profiles"] == [
        "ProfileOpsRead",
        "ProfileOpsControl",
    ]
    assert tokens["result"]["tokens"] == []
    assert sessions["result"]["sessions"] == []
    assert audit["result"]["events"] == []
    assert issued["result"]["session_id"] == "sess-1"

    assert stub.calls[0][0] == "foundation"
    assert stub.calls[1] == ("get_descriptor", {"descriptor_id": "mcp_session_profile", "level": "std"})
    assert stub.calls[2] == ("list_access_tokens", {"limit": 50, "target_id": "hub:test-subnet", "active_only": True})
    assert stub.calls[3] == ("list_session_leases", {"limit": 50, "target_id": "hub:test-subnet", "active_only": True})
    assert stub.calls[4] == ("recent_audit", {"limit": 7, "target_id": "hub:test-subnet"})
    assert stub.calls[5][0] == "issue_session_lease"
    assert stub.calls[5][1]["target_id"] == "hub:test-subnet"
    assert stub.calls[5][1]["capability_profile"] == "ProfileOpsRead"
