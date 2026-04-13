from __future__ import annotations

import importlib

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


def test_hub_root_sidecar_status_prints_route_tunnel_summary(monkeypatch) -> None:
    hub_cli = importlib.import_module("adaos.apps.cli.commands.hub")
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
    hub_cli = importlib.import_module("adaos.apps.cli.commands.hub")
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
