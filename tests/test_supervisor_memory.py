from __future__ import annotations

from fastapi.testclient import TestClient

from adaos.apps import supervisor
from adaos.services.supervisor_memory import (
    MemoryOperationEvent,
    MemorySessionSummary,
    MemoryTelemetrySample,
    append_memory_session_operation,
    append_memory_telemetry_sample,
    ensure_memory_store,
    read_memory_session_operations,
    read_memory_telemetry_tail,
    read_memory_runtime_state,
    read_memory_session_index,
    supervisor_memory_runtime_state_path,
    supervisor_memory_session_operations_path,
    supervisor_memory_sessions_index_path,
    write_memory_session_index,
)


def test_memory_store_initializes_runtime_and_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    ensure_memory_store()

    assert supervisor_memory_runtime_state_path().exists()
    assert supervisor_memory_sessions_index_path().exists()
    runtime = read_memory_runtime_state()
    index = read_memory_session_index()

    assert runtime["contract_version"] == "1"
    assert runtime["authority"] == "supervisor"
    assert runtime["current_profile_mode"] == "normal"
    assert index["contract_version"] == "1"
    assert index["sessions"] == []


def test_memory_session_summary_normalizes_artifact_refs() -> None:
    payload = MemorySessionSummary.from_dict(
        {
            "session_id": "mem-001",
            "profile_mode": "sampled_profile",
            "session_state": "planned",
            "trigger_source": "operator",
            "artifact_refs": [
                {"artifact_id": "trace-1", "kind": "snapshot", "size_bytes": "128"},
                {"artifact_id": "report-1", "kind": "summary", "published_ref": "root://report-1"},
            ],
        }
    ).to_dict()

    assert payload["session_id"] == "mem-001"
    assert payload["profile_mode"] == "sampled_profile"
    assert payload["session_state"] == "planned"
    assert payload["trigger_source"] == "operator"
    assert payload["artifact_refs"][0]["size_bytes"] == 128
    assert payload["artifact_refs"][1]["published_ref"] == "root://report-1"


def test_memory_session_operations_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    append_memory_session_operation(
        "mem-001",
        MemoryOperationEvent(
            event_id="op-1",
            event="tool_invoked",
            emitted_at=11.0,
            session_id="mem-001",
            profile_mode="sampled_profile",
            sequence=1,
            details={"action": "profile_start"},
        ),
    )

    path = supervisor_memory_session_operations_path("mem-001")
    items = read_memory_session_operations("mem-001", limit=10)

    assert path.exists()
    assert len(items) == 1
    assert items[0]["event"] == "tool_invoked"
    assert items[0]["details"]["action"] == "profile_start"


def test_memory_telemetry_tail_round_trips_samples(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    append_memory_telemetry_sample(
        MemoryTelemetrySample(
            sampled_at=10.0,
            slot="A",
            runtime_instance_id="rt-a-a-1",
            managed_pid=999,
            process_rss_bytes=123,
            family_rss_bytes=456,
        )
    )

    tail = read_memory_telemetry_tail(limit=5)

    assert len(tail) == 1
    assert tail[0]["slot"] == "A"
    assert tail[0]["family_rss_bytes"] == 456


def test_supervisor_manager_memory_status_reports_live_rss(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "_proc_details", lambda proc, cwd_hint=None: {"managed_pid": 4321})
    monkeypatch.setattr(supervisor, "_process_family_rss_bytes", lambda pid: (111, 222))
    write_memory_session_index(
        {
            "contract_version": "1",
            "sessions": [{"session_id": "mem-001", "profile_mode": "normal"}],
            "updated_at": 10.0,
        }
    )

    payload = manager.memory_status()

    assert payload["selected_profiler_adapter"] == "tracemalloc"
    assert payload["active_slot"] == "A"
    assert payload["managed_pid"] == 4321
    assert payload["current_process_rss_bytes"] == 111
    assert payload["current_family_rss_bytes"] == 222
    assert payload["sessions_total"] == 1
    assert payload["last_session_id"] == "mem-001"
    assert payload["profile_control_mode"] == "phase1_intent_only"
    assert payload["operation_log_contract_version"] == "1"


def test_supervisor_manager_profile_intent_flow_updates_session_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(supervisor, "read_core_update_status", lambda: {"state": "idle", "phase": ""})
    monkeypatch.setattr(supervisor, "_read_update_attempt", lambda: None)

    started = manager.start_memory_profile(profile_mode="sampled_profile", reason="operator.request")
    session_id = started["session"]["session_id"]

    assert started["control_mode"] == "phase1_intent_only"
    assert started["session"]["session_state"] == "planned"
    assert started["runtime"]["requested_profile_mode"] == "sampled_profile"
    assert started["runtime"]["requested_session_id"] == session_id

    details = manager.memory_session(session_id)
    assert details is not None
    assert details["operations"][0]["details"]["action"] == "profile_start"

    published = manager.publish_memory_profile(session_id, reason="operator.publish")
    assert published["session"]["publish_state"] == "publish_requested"
    assert published["runtime"]["publish_request_session_id"] == session_id

    stopped = manager.stop_memory_profile(session_id, reason="operator.stop")
    assert stopped["session"]["session_state"] == "cancelled"
    assert stopped["runtime"]["requested_session_id"] is None
    assert stopped["runtime"]["requested_profile_mode"] is None


def test_supervisor_memory_endpoints_expose_read_only_phase1_surfaces(monkeypatch) -> None:
    class _Manager:
        def memory_status(self) -> dict:
            return {"contract_version": "1", "current_profile_mode": "normal"}

        def memory_sessions(self) -> dict:
            return {"ok": True, "sessions": [{"session_id": "mem-001"}], "total": 1}

        def memory_session(self, session_id: str) -> dict | None:
            if session_id == "mem-001":
                return {"ok": True, "session": {"session_id": session_id}}
            return None

        def start_memory_profile(self, *, profile_mode: str, reason: str, trigger_source: str = "operator") -> dict:
            return {
                "ok": True,
                "control_mode": "phase1_intent_only",
                "session": {"session_id": "mem-001", "profile_mode": profile_mode, "trigger_source": trigger_source},
            }

        def stop_memory_profile(self, session_id: str, *, reason: str) -> dict:
            return {"ok": True, "session": {"session_id": session_id, "session_state": "cancelled"}}

        def publish_memory_profile(self, session_id: str, *, reason: str) -> dict:
            return {"ok": True, "session": {"session_id": session_id, "publish_state": "publish_requested"}}

    monkeypatch.setattr(supervisor, "_manager", lambda: _Manager())
    client = TestClient(supervisor.app)
    headers = {"X-AdaOS-Token": "dev-local-token"}

    status_response = client.get("/api/supervisor/memory/status", headers=headers)
    sessions_response = client.get("/api/supervisor/memory/sessions", headers=headers)
    session_response = client.get("/api/supervisor/memory/sessions/mem-001", headers=headers)
    missing_response = client.get("/api/supervisor/memory/sessions/missing", headers=headers)
    start_response = client.post(
        "/api/supervisor/memory/profile/start",
        headers=headers,
        json={"profile_mode": "sampled_profile", "reason": "operator.request"},
    )
    stop_response = client.post(
        "/api/supervisor/memory/profile/mem-001/stop",
        headers=headers,
        json={"reason": "operator.stop"},
    )
    publish_response = client.post(
        "/api/supervisor/memory/publish",
        headers=headers,
        json={"session_id": "mem-001", "reason": "operator.publish"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["current_profile_mode"] == "normal"
    assert sessions_response.status_code == 200
    assert sessions_response.json()["total"] == 1
    assert session_response.status_code == 200
    assert session_response.json()["session"]["session_id"] == "mem-001"
    assert missing_response.status_code == 404
    assert start_response.status_code == 200
    assert start_response.json()["session"]["profile_mode"] == "sampled_profile"
    assert stop_response.status_code == 200
    assert stop_response.json()["session"]["session_state"] == "cancelled"
    assert publish_response.status_code == 200
    assert publish_response.json()["session"]["publish_state"] == "publish_requested"
