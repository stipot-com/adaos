from __future__ import annotations

import importlib
import sys
import types

import requests
from typer.testing import CliRunner


def _sidecar_runtime_payload() -> dict:
    return {
        "runtime": {
            "status": "ready",
            "phase": "nats_transport_sidecar",
            "transport_owner": "sidecar",
            "lifecycle_manager": "supervisor",
            "local_listener_state": "ready",
            "remote_session_state": "ready",
            "control_ready": "ready",
            "route_ready": "planned",
            "scope": {
                "planned_next_boundaries": ["browser_events_ws", "browser_yjs_ws"],
            },
            "continuity_contract": {
                "current_support": "planned",
                "hub_runtime_update": "preserve_sidecar",
            },
            "progress": {
                "target": "first_browser_realtime_tunnel",
                "state": "in_progress",
                "completed_milestones": 2,
                "milestone_total": 4,
                "current_milestone": "browser_events_ws_handoff",
                "next_blocker": "browser route websocket still terminates in the runtime FastAPI app",
            },
            "route_tunnel_contract": {
                "current_support": "planned",
                "ws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "delegation_mode": "not_implemented",
                    "blockers": ["browser route websocket still terminates in the runtime FastAPI app"],
                },
                "yws": {
                    "current_owner": "runtime",
                    "planned_owner": "sidecar",
                    "delegation_mode": "not_implemented",
                    "blockers": ["Yjs websocket/session ownership still lives in the runtime gateway"],
                },
            },
        },
        "process": {
            "listener_pid": 12345,
            "managed_pid": 12345,
            "adopted_listener": False,
        },
    }


def _import_hub_cli():
    sys.modules.setdefault("y_py", types.ModuleType("y_py"))
    sys.modules.setdefault("ypy_websocket", types.ModuleType("ypy_websocket"))
    sys.modules.setdefault("ypy_websocket.ystore", types.ModuleType("ypy_websocket.ystore"))
    ystore = sys.modules["ypy_websocket.ystore"]
    if not hasattr(ystore, "BaseYStore"):
        ystore.BaseYStore = object
    if not hasattr(ystore, "YDocNotFound"):
        class _YDocNotFound(Exception):
            pass
        ystore.YDocNotFound = _YDocNotFound
    return importlib.import_module("adaos.apps.cli.commands.hub")


def test_hub_root_sidecar_status_prints_route_tunnel_summary(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "resolve_control_base_url", lambda: "http://127.0.0.1:8777")
    monkeypatch.setattr(hub_cli, "resolve_control_token", lambda: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json():
            return _sidecar_runtime_payload()

    monkeypatch.setattr(requests, "get", lambda url, headers, timeout: _Response())

    result = CliRunner().invoke(hub_cli.app, ["root", "sidecar", "status"])

    assert result.exit_code == 0
    assert "sidecar=ready" in result.output
    assert "continuity=planned:preserve_sidecar" in result.output
    assert "progress=2/4 target=first_browser_realtime_tunnel state=in_progress current=browser_events_ws_handoff" in result.output
    assert "progress_blocker=browser route websocket still terminates in the runtime FastAPI app" in result.output
    assert "route_tunnel=planned" in result.output
    assert "ws=runtime->sidecar:not_implemented" in result.output
    assert "yws=runtime->sidecar:not_implemented" in result.output
    assert "ws_blocker=browser route websocket still terminates in the runtime FastAPI app" in result.output
    assert "yws_blocker=Yjs websocket/session ownership still lives in the runtime gateway" in result.output


def test_hub_root_sidecar_restart_prints_route_tunnel_summary(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "resolve_control_base_url", lambda: "http://127.0.0.1:8777")
    monkeypatch.setattr(hub_cli, "resolve_control_token", lambda: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json():
            payload = _sidecar_runtime_payload()
            payload["restart"] = {"accepted": True, "reason": "manual"}
            return payload

    monkeypatch.setattr(requests, "post", lambda url, headers, json, timeout: _Response())

    result = CliRunner().invoke(hub_cli.app, ["root", "sidecar", "restart"])

    assert result.exit_code == 0
    assert "accepted=True" in result.output
    assert "sidecar=ready/ready" in result.output
    assert "progress=2/4 target=first_browser_realtime_tunnel state=in_progress current=browser_events_ws_handoff" in result.output
    assert "route_tunnel=planned" in result.output
    assert "ws=runtime->sidecar:not_implemented" in result.output
    assert "yws=runtime->sidecar:not_implemented" in result.output


def test_hub_root_reports_prints_memory_profile_rows(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "get_ctx", lambda: type("Ctx", (), {"config": type("Cfg", (), {"subnet_id": "subnet-test-1", "root_settings": type("Root", (), {"base_url": "https://root.test"})()})()})())
    monkeypatch.setattr(hub_cli, "_root_verify_from_conf", lambda conf: True)
    monkeypatch.setenv("ROOT_TOKEN", "root-token")

    class _Client:
        @staticmethod
        def list_profileops_sessions(
            target_id: str,
            *,
            state: str | None = None,
            suspected_only: bool = False,
        ) -> dict:
            assert target_id == "hub:subnet-test-1"
            assert state is None
            assert suspected_only is False
            return {
                "response": {"result": {
                    "sessions": [
                    {
                        "hub_id": "hub:subnet-test-1",
                        "session_id": "mem-001",
                        "reported_at": "2026-04-18T12:00:01Z",
                        "session": {
                            "session_id": "mem-001",
                            "profile_mode": "trace_profile",
                            "session_state": "finished",
                            "suspected_leak": True,
                            "artifact_refs": [{"artifact_id": "mem-001-final"}],
                        },
                    }
                ]}}
            }

    monkeypatch.setattr(hub_cli, "_root_mcp_client", lambda conf, root_base, root_token: _Client())

    result = CliRunner().invoke(hub_cli.app, ["root", "reports", "--kind", "memory-profile"])

    assert result.exit_code == 0
    assert "memory_profile reports:" in result.output
    assert "session=mem-001" in result.output
    assert "mode=trace_profile" in result.output
    assert "state=finished" in result.output
    assert "suspected=True" in result.output


def test_hub_root_memory_session_prints_remote_summary(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "get_ctx", lambda: type("Ctx", (), {"config": type("Cfg", (), {"subnet_id": "subnet-test-1", "root_settings": type("Root", (), {"base_url": "https://root.test"})()})()})())
    monkeypatch.setattr(hub_cli, "_root_verify_from_conf", lambda conf: True)
    monkeypatch.setenv("ROOT_TOKEN", "root-token")

    class _Client:
        @staticmethod
        def get_profileops_session(target_id: str, session_id: str) -> dict:
            assert target_id == "hub:subnet-test-1"
            assert session_id == "mem-001"
            return {
                "response": {"result": {"session": {
                    "hub_id": "hub:subnet-test-1",
                    "session_id": "mem-001",
                    "report": {
                        "reported_at": "2026-04-18T12:00:00Z",
                        "root_received_at": "2026-04-18T12:00:01Z",
                        "session": {
                            "session_id": "mem-001",
                            "profile_mode": "trace_profile",
                            "session_state": "finished",
                            "suspected_leak": True,
                            "baseline_rss_bytes": 128,
                            "peak_rss_bytes": 256,
                            "rss_growth_bytes": 64,
                            "retry_of_session_id": "mem-000",
                            "retry_root_session_id": "mem-root",
                            "retry_depth": 2,
                            "artifact_refs": [{"artifact_id": "mem-001-final"}],
                        },
                        "operations_tail": [{"event": "tool_invoked"}],
                        "telemetry_tail": [{"sampled_at": 1.0}],
                    },
                }}}
            }

    monkeypatch.setattr(hub_cli, "_root_mcp_client", lambda conf, root_base, root_token: _Client())

    result = CliRunner().invoke(hub_cli.app, ["root", "memory-session", "mem-001"])

    assert result.exit_code == 0
    assert "memory profile: hub=hub:subnet-test-1 session=mem-001 mode=trace_profile state=finished suspected=True" in result.output
    assert "memory rss: baseline=128 peak=256 growth=64" in result.output
    assert "memory remote: reported=2026-04-18T12:00:00Z received=2026-04-18T12:00:01Z artifacts=1 operations=1 telemetry=1" in result.output
    assert "first artifact: mem-001-final" in result.output
    assert "retry chain: from=mem-000 root=mem-root depth=2" in result.output


def test_hub_root_memory_artifact_prints_remote_artifact(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "get_ctx", lambda: type("Ctx", (), {"config": type("Cfg", (), {"subnet_id": "subnet-test-1", "root_settings": type("Root", (), {"base_url": "https://root.test"})()})()})())
    monkeypatch.setattr(hub_cli, "_root_verify_from_conf", lambda conf: True)
    monkeypatch.setenv("ROOT_TOKEN", "root-token")

    class _Client:
        @staticmethod
        def get_profileops_artifact(target_id: str, session_id: str, artifact_id: str, *, offset: int = 0, max_bytes: int = 256 * 1024) -> dict:
            assert target_id == "hub:subnet-test-1"
            assert session_id == "mem-001"
            assert artifact_id == "mem-001-final"
            assert offset == 0
            assert max_bytes == 256 * 1024
            return {
                "response": {"result": {
                    "session_id": "mem-001",
                    "artifact": {
                        "artifact_id": "mem-001-final",
                        "kind": "tracemalloc_final_snapshot",
                        "published_ref": "root://hub-memory-profile/mem-001/mem-001-final",
                        "fetch_strategy": "inline_content",
                        "source_api_path": "/api/supervisor/memory/sessions/mem-001/artifacts/mem-001-final",
                    },
                    "exists": True,
                    "delivery": {
                        "mode": "root_inline_content",
                        "relay_supported": True,
                        "relay_reason": "inline_content_available_at_root",
                    },
                    "transfer": {"encoding": "json", "chunk_bytes": 64, "remaining_bytes": 0, "truncated": False},
                    "content": {"top_allocations": []},
                }}
            }

    monkeypatch.setattr(hub_cli, "_root_mcp_client", lambda conf, root_base, root_token: _Client())

    result = CliRunner().invoke(hub_cli.app, ["root", "memory-artifact", "mem-001", "mem-001-final"])

    assert result.exit_code == 0
    assert "memory artifact: session=mem-001 id=mem-001-final kind=tracemalloc_final_snapshot exists=True" in result.output
    assert "published ref: root://hub-memory-profile/mem-001/mem-001-final" in result.output
    assert "fetch strategy: inline_content" in result.output
    assert "source api path: /api/supervisor/memory/sessions/mem-001/artifacts/mem-001-final" in result.output
    assert "delivery mode: root_inline_content" in result.output
    assert "relay: supported=True reason=inline_content_available_at_root" in result.output
    assert "transfer: encoding=json chunk=64 remaining=0 truncated=False" in result.output
    assert "content keys: top_allocations" in result.output


def test_hub_root_memory_artifacts_prints_remote_catalog(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "get_ctx", lambda: type("Ctx", (), {"config": type("Cfg", (), {"subnet_id": "subnet-test-1", "root_settings": type("Root", (), {"base_url": "https://root.test"})()})()})())
    monkeypatch.setattr(hub_cli, "_root_verify_from_conf", lambda conf: True)
    monkeypatch.setenv("ROOT_TOKEN", "root-token")

    class _Client:
        @staticmethod
        def list_profileops_artifacts(target_id: str, session_id: str) -> dict:
            assert target_id == "hub:subnet-test-1"
            assert session_id == "mem-001"
            return {
                "response": {"result": {
                    "session_id": "mem-001",
                    "artifact_policy": {
                        "delivery_mode": "inline_json_only",
                        "max_inline_bytes": 262144,
                    },
                    "artifacts": [
                    {
                        "artifact_id": "mem-001-final",
                        "kind": "tracemalloc_final_snapshot",
                        "publish_status": "inline_available",
                        "remote_available": True,
                        "fetch_strategy": "inline_content",
                        "size_bytes": 128,
                    },
                    {
                        "artifact_id": "mem-001-raw",
                        "kind": "heap_dump",
                        "publish_status": "kind_not_allowed",
                        "remote_available": False,
                        "fetch_strategy": "local_control_pull",
                        "size_bytes": 4096,
                    },
                ]}}
            }

    monkeypatch.setattr(hub_cli, "_root_mcp_client", lambda conf, root_base, root_token: _Client())

    result = CliRunner().invoke(hub_cli.app, ["root", "memory-artifacts", "mem-001"])

    assert result.exit_code == 0
    assert "memory artifacts: session=mem-001 count=2 delivery=inline_json_only limit=262144" in result.output
    assert "artifact: id=mem-001-final kind=tracemalloc_final_snapshot status=inline_available remote=True size=128" in result.output
    assert "artifact: id=mem-001-raw kind=heap_dump status=kind_not_allowed remote=False size=4096" in result.output


def test_hub_root_memory_artifact_pull_falls_back_to_local_control(monkeypatch) -> None:
    hub_cli = _import_hub_cli()
    monkeypatch.setattr(hub_cli, "get_ctx", lambda: type("Ctx", (), {"config": type("Cfg", (), {"subnet_id": "subnet-test-1", "root_settings": type("Root", (), {"base_url": "https://root.test"})()})()})())
    monkeypatch.setattr(hub_cli, "_root_verify_from_conf", lambda conf: True)
    monkeypatch.setattr(hub_cli, "resolve_control_base_url", lambda: "http://127.0.0.1:8777")
    monkeypatch.setattr(hub_cli, "_local_control_token", lambda base: "dev-token")
    monkeypatch.setenv("ROOT_TOKEN", "root-token")

    class _Client:
        @staticmethod
        def get_profileops_artifact(target_id: str, session_id: str, artifact_id: str, *, offset: int = 0, max_bytes: int = 256 * 1024) -> dict:
            assert target_id == "hub:subnet-test-1"
            assert session_id == "mem-001"
            assert artifact_id == "mem-001-raw"
            assert offset == 0
            assert max_bytes == 256 * 1024
            return {
                "response": {"result": {
                    "session_id": "mem-001",
                    "artifact": {
                        "artifact_id": "mem-001-raw",
                        "kind": "heap_dump",
                        "fetch_strategy": "local_control_pull",
                        "source_api_path": "/api/supervisor/memory/sessions/mem-001/artifacts/mem-001-raw",
                    },
                    "exists": False,
                    "delivery": {
                        "mode": "local_control_pull",
                        "relay_supported": False,
                        "relay_reason": "root_direct_relay_not_configured_for_target",
                        "source_api_path": "/api/supervisor/memory/sessions/mem-001/artifacts/mem-001-raw",
                    },
                    "transfer": {
                        "offset": 0,
                        "requested_max_bytes": 262144,
                        "size_bytes": 4096,
                        "chunk_bytes": 0,
                        "remaining_bytes": 0,
                        "truncated": False,
                        "encoding": "unavailable",
                        "pull_supported": False,
                    },
                    "content": None,
                }}
            }

    class _Response:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "ok": True,
                "exists": True,
                "artifact": {"artifact_id": "mem-001-raw", "kind": "heap_dump"},
                "transfer": {
                    "encoding": "base64",
                    "chunk_bytes": 256,
                    "remaining_bytes": 1024,
                    "truncated": True,
                },
                "content_base64": "AAEC",
            }

    def _fake_get(url, headers=None, timeout=None):
        assert url == "http://127.0.0.1:8777/api/supervisor/memory/sessions/mem-001/artifacts/mem-001-raw?offset=0&max_bytes=262144"
        assert headers == {"X-AdaOS-Token": "dev-token"}
        return _Response()

    monkeypatch.setattr(hub_cli, "_root_mcp_client", lambda conf, root_base, root_token: _Client())
    monkeypatch.setattr(hub_cli.requests, "get", _fake_get)

    result = CliRunner().invoke(hub_cli.app, ["root", "memory-artifact-pull", "mem-001", "mem-001-raw"])

    assert result.exit_code == 0
    assert "memory artifact pull: session=mem-001 id=mem-001-raw kind=heap_dump strategy=local_control_pull exists=True" in result.output
    assert "transfer: encoding=base64 chunk=256 remaining=1024 truncated=True" in result.output
    assert "relay: supported=False reason=root_direct_relay_not_configured_for_target" in result.output
    assert "delivery: current_hub_control" in result.output
    assert "base64 chars: 4" in result.output
