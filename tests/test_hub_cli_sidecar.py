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
        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def root_memory_profile_reports(
            *,
            root_token: str,
            hub_id: str | None = None,
            session_id: str | None = None,
            session_state: str | None = None,
            suspected_only: bool | None = None,
        ) -> dict:
            assert root_token == "root-token"
            assert hub_id == "subnet-test-1"
            assert session_id is None
            assert session_state is None
            assert suspected_only is None
            return {
                "ok": True,
                "reports": [
                    {
                        "hub_id": "hub:subnet-test-1",
                        "session_id": "mem-001",
                        "report": {
                            "root_received_at": "2026-04-18T12:00:01Z",
                            "_protocol": {"message_id": "mem-msg-1", "cursor": 3},
                            "session": {
                                "session_id": "mem-001",
                                "profile_mode": "trace_profile",
                                "session_state": "finished",
                                "suspected_leak": True,
                                "artifact_refs": [{"artifact_id": "mem-001-final"}],
                            },
                        },
                    }
                ],
            }

    monkeypatch.setattr(hub_cli, "RootHttpClient", _Client)

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
        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def root_memory_profile_report(*, root_token: str, session_id: str) -> dict:
            assert root_token == "root-token"
            assert session_id == "mem-001"
            return {
                "ok": True,
                "report": {
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
                },
            }

    monkeypatch.setattr(hub_cli, "RootHttpClient", _Client)

    result = CliRunner().invoke(hub_cli.app, ["root", "memory-session", "mem-001"])

    assert result.exit_code == 0
    assert "memory profile: hub=hub:subnet-test-1 session=mem-001 mode=trace_profile state=finished suspected=True" in result.output
    assert "memory rss: baseline=128 peak=256 growth=64" in result.output
    assert "memory remote: reported=2026-04-18T12:00:00Z received=2026-04-18T12:00:01Z artifacts=1 operations=1 telemetry=1" in result.output
    assert "first artifact: mem-001-final" in result.output
    assert "retry chain: from=mem-000 root=mem-root depth=2" in result.output
