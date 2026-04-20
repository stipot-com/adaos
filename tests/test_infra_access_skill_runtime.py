from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


def _load_module():
    if "adaos.sdk.data.ctx" not in sys.modules:
        fake_ctx = types.ModuleType("adaos.sdk.data.ctx")

        class _FakeSubnet:
            def set(self, slot, value, *, webspace_id=None):
                return None

        fake_ctx.subnet = _FakeSubnet()
        fake_ctx.current_user = object()
        fake_ctx.selected_user = object()
        sys.modules["adaos.sdk.data.ctx"] = fake_ctx
    path = Path(__file__).resolve().parents[1] / ".adaos" / "workspace" / "skills" / "infra_access_skill" / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("test_infra_access_skill_handlers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_infra_access_skill_snapshot_and_projection(monkeypatch) -> None:
    module = _load_module()

    projected: list[tuple[str | None, dict]] = []
    module._CACHE["ts"] = 0.0
    module._CACHE["snapshot"] = None

    monkeypatch.setattr(
        module.sdk_root_mcp,
        "get_local_target_context",
        lambda **kwargs: {
            "root_url": "https://root.test",
            "target_id": "hub:test-subnet",
            "subnet_id": "subnet:test-subnet",
            "zone": "lab-a",
        },
    )
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "get_local_operational_surface",
        lambda **kwargs: {
            "response": {
                "result": {
                    "operational_surface": {
                        "token_management": {
                            "session_capability_profiles": ["ProfileOpsRead", "ProfileOpsControl"],
                        }
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "list_local_mcp_sessions",
        lambda **kwargs: {
            "response": {
                "result": {
                    "sessions": [
                        {
                            "session_id": "sess-1",
                            "target_id": "hub:test-subnet",
                            "capability_profile": "ProfileOpsRead",
                            "status": "active",
                            "expires_at": "2026-04-20T12:00:00+00:00",
                            "last_used_at": "2026-04-20T10:00:00+00:00",
                            "use_count": 2,
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "list_local_access_tokens",
        lambda **kwargs: {
            "response": {
                "result": {
                    "tokens": [
                        {
                            "token_id": "tok-1",
                            "primary_target_id": "hub:test-subnet",
                            "audience": "codex-vscode",
                            "status": "active",
                            "expires_at": "2026-04-20T13:00:00+00:00",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "get_local_activity_log",
        lambda **kwargs: {
            "response": {
                "result": {
                    "events": [
                        {
                            "event_id": "evt-1",
                            "tool_id": "hub.issue_mcp_session",
                            "status": "ok",
                            "created_at": "2026-04-20T09:00:00+00:00",
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(module.ctx_subnet, "set", lambda slot, value, webspace_id=None: projected.append((webspace_id, value)))

    snapshot = module.get_snapshot(webspace_id="default")

    assert snapshot["target_id"] == "hub:test-subnet"
    assert snapshot["summary"]["value"] == 2
    assert snapshot["tokens"][0]["title"] == "MCP session sess-1"
    assert snapshot["tokens"][1]["title"] == "Access token tok-1"
    assert snapshot["codex_help"][0]["content"]["step_2"].startswith("adaos dev root mcp prepare-codex")
    assert projected and projected[0][0] == "default"
    assert projected[0][1]["connections"]["mcp_http_url"] == "https://root.test/v1/root/mcp"


def test_infra_access_skill_issue_codex_connection(monkeypatch) -> None:
    module = _load_module()

    module._CACHE["ts"] = 0.0
    module._CACHE["snapshot"] = None
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "get_local_target_context",
        lambda **kwargs: {
            "root_url": "https://root.test",
            "target_id": "hub:test-subnet",
            "subnet_id": "subnet:test-subnet",
            "zone": "lab-a",
        },
    )
    monkeypatch.setattr(
        module.sdk_root_mcp,
        "issue_local_codex_mcp_session",
        lambda **kwargs: {
            "response": {
                "result": {
                    "session_id": "sess-new",
                    "access_token": "secret-token",
                    "expires_at": "2026-04-20T14:00:00+00:00",
                    "capability_profile": "ProfileOpsRead",
                }
            }
        },
    )
    monkeypatch.setattr(module, "_snapshot_or_cached", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(module, "_project", lambda snapshot, webspace_id=None: None)

    payload = module.issue_codex_connection(webspace_id="default")

    assert payload["session_id"] == "sess-new"
    assert payload["access_token"] == "secret-token"
    assert payload["mcp_http_url"] == "https://root.test/v1/root/mcp"
    assert "--apply-codex" in payload["codex_prepare_command"]
