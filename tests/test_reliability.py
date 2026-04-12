from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
import types

if "nats" not in sys.modules:
    sys.modules["nats"] = types.ModuleType("nats")
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from adaos.services.reliability import (
    ReadinessStatus,
    assess_transport_diagnostics,
    observe_hub_root_route_runtime,
    mark_root_control_down,
    mark_root_control_up,
    mark_route_ready,
    note_root_control_reconnect,
    reliability_snapshot,
    reset_reliability_runtime_state,
    set_integration_readiness,
)
from adaos.services.runtime_lifecycle import reset_runtime_lifecycle


def _reset_state() -> None:
    reset_runtime_lifecycle()
    reset_reliability_runtime_state()


def test_hub_reliability_snapshot_exposes_taxonomy_and_disables_root_bound_capabilities_until_ready() -> None:
    _reset_state()

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert "command" in snapshot["model"]["message_taxonomy"]
    assert "must_not_lose" in snapshot["model"]["delivery_classes"]
    assert snapshot["model"]["authority_boundaries"]["root"]["owns"]
    assert any(item["flow_id"] == "hub_root.control.lifecycle" for item in snapshot["model"]["flow_inventory"])

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["hub_local_core"]["status"] == "ready"
    assert tree["root_control"]["status"] == "unknown"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["execute_local_scenarios"]["allowed"] is True
    assert matrix["new_root_backed_member_admission"]["allowed"] is False
    assert matrix["root_routed_browser_proxy"]["allowed"] is False


def test_hub_reliability_snapshot_enables_route_and_integration_capabilities_when_signals_are_ready() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    tree = snapshot["runtime"]["readiness_tree"]
    assert tree["root_control"]["status"] == "ready"
    assert tree["route"]["status"] == "ready"
    assert tree["integration"]["telegram"]["status"] == "degraded"
    assert snapshot["runtime"]["channel_diagnostics"]["root_control"]["stability"]["state"] == "stable"

    matrix = snapshot["runtime"]["degraded_matrix"]
    assert matrix["root_routed_browser_proxy"]["allowed"] is True
    assert matrix["telegram_action_completion"]["allowed"] is False

    set_integration_readiness(
        "telegram",
        status=ReadinessStatus.READY,
        summary="telegram delivery probe ok",
        observed=True,
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "ready"
    assert snapshot["runtime"]["degraded_matrix"]["telegram_action_completion"]["allowed"] is True


def test_hub_reliability_marks_root_backed_integration_as_stale_when_root_control_is_lost() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    set_integration_readiness(
        "telegram",
        status=ReadinessStatus.READY,
        summary="telegram delivery probe ok",
        observed=True,
    )

    ready_snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )
    assert ready_snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "ready"

    mark_root_control_down(details={"kind": "disconnected"})
    stale_snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert any(item["flow_id"] == "hub_root.integration.llm" for item in stale_snapshot["model"]["flow_inventory"])
    assert stale_snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "down"
    assert stale_snapshot["runtime"]["readiness_tree"]["integration"]["telegram"]["status"] == "degraded"
    assert stale_snapshot["runtime"]["degraded_matrix"]["telegram_action_completion"]["allowed"] is False
    assert stale_snapshot["runtime"]["channel_diagnostics"]["root_control"]["stability"]["state"] == "down"


def test_hub_reliability_marks_flapping_root_channel_when_it_repeatedly_disconnects() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})
    mark_root_control_down(details={"kind": "disconnected"})
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_root_control_down(details={"kind": "disconnected"})
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "degraded"
    assert snapshot["runtime"]["readiness_tree"]["route"]["status"] == "degraded"
    diag = snapshot["runtime"]["channel_diagnostics"]["root_control"]
    assert diag["recent_non_ready_transitions_5m"] == 2
    assert diag["recent_transitions_5m"] >= 5
    assert diag["stability"]["state"] == "flapping"
    assert isinstance(diag["recent_history"], list) and len(diag["recent_history"]) >= 5


def test_hub_reliability_marks_root_channel_unstable_after_reconnect_incident_without_explicit_down() -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats", "ws_tag": "tag-a"})
    note_root_control_reconnect(
        details={"server": "wss://api.inimatic.com/nats", "previous_ws_tag": "tag-a", "ws_tag": "tag-b"}
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    diag = snapshot["runtime"]["channel_diagnostics"]["root_control"]
    assert snapshot["runtime"]["readiness_tree"]["root_control"]["status"] == "degraded"
    assert diag["recent_non_ready_transitions_5m"] == 1
    assert diag["stability"]["state"] in {"unstable", "flapping"}
    assert any(item["status"] == "reconnect" for item in diag["recent_history"])


def test_hub_reliability_snapshot_exposes_route_reset_runtime_details() -> None:
    _reset_state()
    observe_hub_root_route_runtime(
        last_reset_at=1_774_017_180.0,
        last_reset_reason="nats_reconnected",
        last_reset_closed_tunnels=3,
        last_reset_dropped_pending=11,
        last_reset_notified_browser=2,
        reset_total=4,
    )

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    route_runtime = snapshot["runtime"]["hub_root_protocol"]["route_runtime"]
    assert route_runtime["last_reset_reason"] == "nats_reconnected"
    assert route_runtime["last_reset_closed_tunnels"] == 3
    assert route_runtime["last_reset_dropped_pending"] == 11
    assert route_runtime["last_reset_notified_browser"] == 2
    assert route_runtime["reset_total"] == 4
    assert route_runtime["last_reset_ago_s"] is not None


def test_assess_transport_diagnostics_marks_unstable_on_reader_termination_and_tag_change() -> None:
    now_ts = 1_774_017_180.0
    assessment = assess_transport_diagnostics(
        [
            {
                "ts": now_ts - 12.0,
                "source": "periodic",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": None,
            },
            {
                "ts": now_ts - 3.0,
                "source": "periodic",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": True},
                "err": None,
            },
            {
                "ts": now_ts - 1.0,
                "source": "periodic",
                "ws_tag": "tag-b",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": None,
            },
        ],
        now_ts=now_ts,
    )

    assert assessment["state"] in {"unstable", "flapping", "down"}
    assert assessment["recent_tag_changes_5m"] == 1
    assert assessment["recent_incidents_5m"] >= 1
    assert "reading_task_terminated" in assessment["last_incident_reasons"]


def test_assess_transport_diagnostics_marks_flapping_on_repeated_error_callbacks() -> None:
    now_ts = 1_774_017_300.0
    assessment = assess_transport_diagnostics(
        [
            {
                "ts": now_ts - 240.0,
                "source": "error_cb",
                "ws_tag": "tag-a",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": "UnexpectedEOF: nats: unexpected EOF",
            },
            {
                "ts": now_ts - 120.0,
                "source": "error_cb",
                "ws_tag": "tag-b",
                "nc_connected": True,
                "reading_task": {"done": False},
                "err": "UnexpectedEOF: nats: unexpected EOF",
            },
            {
                "ts": now_ts - 5.0,
                "source": "periodic",
                "ws_tag": "tag-c",
                "nc_connected": True,
                "reading_task": {"done": True},
                "err": None,
            },
        ],
        now_ts=now_ts,
    )

    assert assessment["state"] in {"flapping", "down"}
    assert assessment["recent_error_records_5m"] >= 1
    assert assessment["recent_tag_changes_15m"] >= 2
    assert assessment["recent_hard_incidents_5m"] >= 1


def test_member_reliability_snapshot_uses_connected_to_hub_for_route_and_sync() -> None:
    _reset_state()

    disconnected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="none",
        connected_to_hub=False,
    )
    assert disconnected["runtime"]["readiness_tree"]["root_control"]["status"] == "not_applicable"
    assert disconnected["runtime"]["readiness_tree"]["route"]["status"] == "down"
    assert disconnected["runtime"]["readiness_tree"]["sync"]["status"] == "down"

    connected = reliability_snapshot(
        node_id="node-2",
        subnet_id="sn_1",
        role="member",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="ws",
        connected_to_hub=True,
    )
    assert connected["runtime"]["readiness_tree"]["route"]["status"] == "ready"
    assert connected["runtime"]["readiness_tree"]["sync"]["status"] == "ready"
    assert connected["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True


def test_node_reliability_endpoint_exposes_model_and_runtime_state(monkeypatch) -> None:
    _reset_state()
    mark_root_control_up(details={"server": "wss://api.inimatic.com/nats"})
    mark_route_ready(details={"subject": "route.to_hub.*"})

    fake_bootstrap = types.ModuleType("adaos.services.bootstrap")
    fake_bootstrap.is_ready = lambda: True
    fake_bootstrap.load_config = lambda: SimpleNamespace(node_id="node-1", subnet_id="sn_1", role="hub")
    fake_bootstrap.request_hub_root_reconnect = lambda *args, **kwargs: {"ok": True}

    async def _fake_switch_role(*args, **kwargs):
        return fake_bootstrap.load_config()

    fake_bootstrap.switch_role = _fake_switch_role
    monkeypatch.setitem(sys.modules, "adaos.services.bootstrap", fake_bootstrap)

    fake_link_client_mod = types.ModuleType("adaos.services.subnet.link_client")
    fake_link_client_mod.get_member_link_client = lambda: SimpleNamespace(is_connected=lambda: False)
    monkeypatch.setitem(sys.modules, "adaos.services.subnet.link_client", fake_link_client_mod)

    sys.modules.pop("adaos.apps.api.node_api", None)
    node_api = importlib.import_module("adaos.apps.api.node_api")
    require_token = importlib.import_module("adaos.apps.api.auth").require_token

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    monkeypatch.setattr(
        node_api,
        "current_reliability_payload",
        lambda: reliability_snapshot(
            node_id="node-1",
            subnet_id="sn_1",
            role="hub",
            local_ready=True,
            node_state="ready",
            draining=False,
            route_mode="hub",
            connected_to_hub=None,
        ),
    )

    client = TestClient(app)
    response = client.get("/api/node/reliability")
    assert response.status_code == 200

    payload = response.json()
    assert payload["model"]["authority_boundaries"]["sidecar"]["must_not_own"]
    assert payload["runtime"]["readiness_tree"]["root_control"]["status"] == "ready"
    assert payload["runtime"]["degraded_matrix"]["root_routed_browser_proxy"]["allowed"] is True


def test_reliability_snapshot_times_out_slow_sync_and_media_sections(monkeypatch) -> None:
    _reset_state()

    def _slow_sync(*, role: str, webspace_id: str | None = None):
        import time as _time

        _time.sleep(0.2)
        return {"available": True, "assessment": {"state": "nominal", "reason": "ok"}}

    def _slow_media(*, role: str, route_mode: str | None, connected_to_hub: bool | None):
        import time as _time

        _time.sleep(0.2)
        return {"available": True, "assessment": {"state": "nominal", "reason": "ok"}}

    monkeypatch.setattr("adaos.services.reliability.yjs_sync_runtime_snapshot", _slow_sync)
    monkeypatch.setattr("adaos.services.reliability.media_plane_runtime_snapshot", _slow_media)
    monkeypatch.setenv("ADAOS_RELIABILITY_RUNTIME_SECTION_TIMEOUT_SEC", "0.05")

    snapshot = reliability_snapshot(
        node_id="node-1",
        subnet_id="sn_1",
        role="hub",
        local_ready=True,
        node_state="ready",
        draining=False,
        route_mode="hub",
        connected_to_hub=None,
    )

    assert snapshot["runtime"]["sync_runtime"]["available"] is False
    assert snapshot["runtime"]["sync_runtime"]["_timed_out"] is True
    assert snapshot["runtime"]["media_runtime"]["available"] is False
    assert snapshot["runtime"]["media_runtime"]["_timed_out"] is True


def test_node_reliability_cli_prints_runtime_summary(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(
        node_cli,
        "_control_get_json",
        lambda **kwargs: (
            200,
            {
                "node": {"node_id": "node-1", "role": "hub", "ready": True, "node_state": "ready"},
                "runtime": {
                    "readiness_tree": {
                        "hub_local_core": {"status": "ready"},
                        "root_control": {"status": "ready"},
                        "route": {"status": "degraded"},
                        "sync": {"status": "ready"},
                        "media": {"status": "unknown"},
                        "integration": {
                            "telegram": {"status": "ready"},
                            "github": {"status": "degraded"},
                            "llm": {"status": "unknown"},
                        },
                    },
                    "channel_diagnostics": {
                        "root_control": {"stability": {"state": "flapping", "score": 62}, "recent_non_ready_transitions_5m": 2},
                        "route": {"stability": {"state": "degraded", "score": 71}, "recent_non_ready_transitions_5m": 1},
                    },
                    "degraded_matrix": {
                        "new_root_backed_member_admission": {"allowed": True},
                        "root_routed_browser_proxy": {"allowed": False},
                        "telegram_action_completion": {"allowed": True},
                        "github_action_completion": {"allowed": False},
                        "llm_action_completion": {"allowed": False},
                        "core_update_coordination_via_root": {"allowed": True},
                    },
                },
            },
        ),
    )

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert "root_control: ready" in result.output
    assert "integration.telegram: ready" in result.output
    assert "diag.root_control: flapping score=62 recent_non_ready_5m=2" in result.output
    assert "root_routed_browser_proxy: blocked" in result.output


def test_node_reliability_cli_reports_timeout_detail(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))
    monkeypatch.setattr(
        node_cli,
        "_control_get_json",
        lambda **kwargs: (None, {"error": "timeout", "detail": "Read timed out"}),
    )

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 2
    assert "timed out" in result.output


def test_node_reliability_cli_falls_back_to_supervisor_transition(monkeypatch) -> None:
    node_cli = importlib.import_module("adaos.apps.cli.commands.node")
    monkeypatch.setattr(node_cli, "load_config", lambda: SimpleNamespace(token="dev-token", role="hub", hub_url=None))

    calls: list[str] = []

    def _fake_get_json(**kwargs):
        calls.append(str(kwargs.get("path") or ""))
        if kwargs.get("path") == "/api/node/reliability":
            return None, {"error": "connection_error", "detail": "connection refused"}
        return (
            200,
            {
                "ok": True,
                "status": {
                    "state": "succeeded",
                    "phase": "root_promoted",
                    "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
                },
                "attempt": {"state": "awaiting_root_restart"},
                "runtime": {"active_slot": "A"},
            },
        )

    monkeypatch.setattr(node_cli, "_control_get_json", _fake_get_json)

    result = CliRunner().invoke(node_cli.app, ["reliability"])

    assert result.exit_code == 0
    assert "/api/node/reliability" in calls
    assert "/api/supervisor/public/update-status" in calls
    assert "runtime_restarting_under_supervisor: yes" in result.output
    assert "supervisor.attempt: awaiting_root_restart" in result.output
