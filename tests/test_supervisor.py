from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from adaos.apps import supervisor
from adaos.services.core_update import read_plan, read_status, write_plan, write_status


def test_reconcile_update_status_marks_stale_attempt_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC", "60")
    monkeypatch.setattr(supervisor, "rollback_to_previous_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "rollback_installed_skill_runtimes",
        lambda: {"ok": True, "total": 1, "failed_total": 0, "rollback_total": 1, "skills": []},
    )

    monkeypatch.setattr(supervisor.time, "time", lambda: 120.0)
    write_status(
        {
            "state": "restarting",
            "phase": "shutdown",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.update",
        }
    )
    write_plan({"state": "pending_restart", "target_rev": "rev2026", "expires_at": 9999999999.0})
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "reason": "test.update",
            "requested_at": 0.0,
            "transitioned_at": 10.0,
            "updated_at": 10.0,
        }
    )

    monkeypatch.setattr(supervisor.time, "time", lambda: 240.0)
    payload = supervisor._reconcile_update_status({"ok": True, "status": read_status(), "_served_by": "supervisor_fallback"})

    assert payload["status"]["state"] == "failed"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["status"]["restored_slot"] == "A"
    assert payload["status"]["rollback"]["ok"] is True
    assert payload["status"]["skill_runtime_rollback"]["rollback_total"] == 1
    assert payload["_served_by"] == "supervisor_timeout_recovery"
    assert read_plan() is None
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"
    assert attempt["last_status"]["state"] == "failed"


def test_reconcile_update_status_completes_attempt_on_terminal_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {"state": "succeeded", "phase": "validate", "updated_at": 499.0},
            "_served_by": "runtime",
        }
    )

    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["last_status"]["state"] == "succeeded"


def test_reconcile_update_status_completes_awaiting_root_restart_attempt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "awaiting_root_restart",
            "action": "update",
            "requested_at": 450.0,
            "transitioned_at": 460.0,
            "updated_at": 460.0,
        }
    )

    payload = supervisor._reconcile_update_status(
        {
            "ok": True,
            "status": {
                "state": "succeeded",
                "phase": "validate",
                "root_restart_completed_at": 499.0,
                "updated_at": 499.0,
            },
            "_served_by": "runtime",
        }
    )

    attempt = payload.get("attempt")
    assert isinstance(attempt, dict)
    assert attempt["state"] == "completed"
    assert attempt["completion_reason"] == "root restart completed"
    assert attempt["last_status"]["root_restart_completed_at"] == 499.0


def test_supervisor_start_update_and_cancel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "active"
        assert attempt["action"] == "update"
        cancelled = await manager.cancel_update(reason="test.cancel")
        assert cancelled["accepted"] is True
        assert cancelled["status"]["state"] == "cancelled"
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "cancelled"

    asyncio.run(_exercise())


def test_supervisor_countdown_worker_writes_plan_and_requests_shutdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    shutdown_calls: list[dict] = []
    stop_calls: list[dict] = []

    async def _fake_sleep(_value: float) -> None:
        return None

    async def _fake_shutdown(*, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict:
        shutdown_calls.append(
            {
                "reason": reason,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
            }
        )
        return {"ok": True, "accepted": True}

    async def _fake_ensure_stopped(*, drain_timeout_sec: float, signal_delay_sec: float, reason: str) -> dict:
        stop_calls.append(
            {
                "reason": reason,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
            }
        )
        return {"ok": True, "forced": False, "reason": reason}

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_request_runtime_shutdown", _fake_shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _fake_ensure_stopped)

    asyncio.run(
        manager._countdown_update_worker(
            action="rollback",
            target_rev="",
            target_version="",
            reason="test.rollback",
            countdown_sec=0.0,
            drain_timeout_sec=5.0,
            signal_delay_sec=0.1,
        )
    )

    plan = read_plan()
    status = read_status()
    assert isinstance(plan, dict)
    assert plan["action"] == "rollback"
    assert status["state"] == "restarting"
    assert status["phase"] == "shutdown"
    assert shutdown_calls and shutdown_calls[0]["reason"] == "test.rollback"
    assert stop_calls and stop_calls[0]["reason"] == "test.rollback"


def test_supervisor_countdown_worker_marks_failed_when_shutdown_request_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _fake_sleep(_value: float) -> None:
        return None

    async def _fake_shutdown(*, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict:
        raise RuntimeError("runtime shutdown API unavailable")

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_request_runtime_shutdown", _fake_shutdown)
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "requested_at": 1.0,
            "transitioned_at": 2.0,
            "updated_at": 2.0,
        }
    )

    asyncio.run(
        manager._countdown_update_worker(
            action="update",
            target_rev="HEAD",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=5.0,
            signal_delay_sec=0.1,
        )
    )

    assert read_plan() is None
    status = read_status()
    assert status["state"] == "failed"
    assert status["phase"] == "shutdown"
    assert status["error_type"] == "RuntimeError"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "failed"


def test_ensure_runtime_stopped_for_update_forces_hung_process(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    timeline = {"now": 0.0}

    class _Proc:
        def __init__(self) -> None:
            self._alive = True
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self):
            return None if self._alive else 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self._alive = False

    proc = _Proc()
    manager._proc = proc

    async def _fake_sleep(value: float) -> None:
        timeline["now"] += max(0.1, float(value))

    monkeypatch.setattr(supervisor.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(supervisor.time, "time", lambda: timeline["now"])

    result = asyncio.run(
        manager._ensure_runtime_stopped_for_update(
            drain_timeout_sec=1.0,
            signal_delay_sec=0.1,
            reason="test.hung_shutdown",
        )
    )

    assert result["ok"] is True
    assert result["forced"] is True
    assert proc.terminate_calls >= 1
    assert proc.kill_calls == 1
    assert proc.poll() == 0


def test_runtime_state_payload_reports_listener_and_api_readiness(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": True, "issues": [], "repo_dir": "/slots/B/repo", "venv_dir": "/slots/B/venv"},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["active_slot"] == "B"
    assert payload["managed_alive"] is True
    assert payload["listener_running"] is True
    assert payload["runtime_api_ready"] is False
    assert payload["runtime_state"] == "starting"
    assert payload["managed_executable"] == "python"
    assert payload["managed_matches_active_slot"] is True
    assert payload["slot_structure"]["ok"] is True
    assert payload["managed_cmdline"][1:3] == ["-m", "adaos.apps.autostart_runner"]


def test_runtime_state_payload_reports_slot_mismatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["/wrong/python", "-m", "adaos.apps.autostart_runner"]
        cwd = "/wrong"

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["/expected/python", "-m", "adaos.apps.autostart_runner"],
            "cwd": "/expected",
        },
    )
    monkeypatch.setattr(
        supervisor,
        "validate_slot_structure",
        lambda slot: {"slot": slot, "ok": False, "issues": ["nested_slot_dir:/slots/A/A"]},
    )
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: False)

    payload = manager.status()

    assert payload["runtime_state"] == "spawned"
    assert payload["managed_matches_active_slot"] is False


def test_runtime_state_payload_surfaces_root_promotion_requirement(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    manager._proc = _Proc()
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)

    payload = manager.status()

    assert payload["root_promotion_required"] is True
    assert "src/adaos/apps/supervisor.py" in payload["bootstrap_update"]["changed_paths"]


def test_supervisor_promote_root_marks_update_succeeded(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "repo_dir": str(tmp_path / "slots" / "B" / "repo"),
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
            "backup_dir": str(tmp_path / "backup"),
            "promoted_paths": ["src/adaos/apps/supervisor.py"],
            "removed_paths": [],
            "restart_required": True,
        },
    )
    supervisor._write_update_attempt({"state": "active", "action": "update", "updated_at": 1.0})
    write_status({"state": "validated", "phase": "root_promotion_pending", "target_slot": "B"})

    payload = asyncio.run(manager.promote_root(reason="test.root_promotion"))

    assert payload["accepted"] is True
    assert payload["status"]["state"] == "succeeded"
    assert payload["status"]["phase"] == "root_promoted"
    assert payload["root_promotion"]["restart_required"] is True
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"
    assert attempt["last_status"]["phase"] == "root_promoted"


def test_public_update_status_payload_is_browser_safe() -> None:
    payload = supervisor._public_update_status_payload(
        {
            "status": {
                "state": "restarting",
                "phase": "shutdown",
                "message": "countdown completed; pending update written",
                "target_rev": "rev2026",
                "target_version": "0.1.0+1.abc",
                "updated_at": 123.0,
                "error": "hidden",
            },
            "runtime": {
                "active_slot": "A",
                "runtime_state": "spawned",
                "listener_running": False,
                "runtime_api_ready": False,
                "root_promotion_required": True,
                "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
                "managed_cmdline": ["hidden"],
            },
            "_served_by": "supervisor_fallback",
        }
    )

    assert payload["ok"] is True
    assert payload["status"]["state"] == "restarting"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["runtime"]["active_slot"] == "A"
    assert payload["runtime"]["root_promotion_required"] is True
    assert payload["_served_by"] == "supervisor_fallback"
    assert "managed_cmdline" not in payload["runtime"]
    assert "error" not in payload["status"]


def test_public_update_status_endpoint_is_unauthenticated(monkeypatch) -> None:
    class _Manager:
        def public_update_status(self) -> dict:
            return {
                "ok": True,
                "status": {"state": "restarting", "phase": "shutdown"},
                "runtime": {"runtime_state": "spawned"},
            }

    monkeypatch.setattr(supervisor, "_manager", lambda: _Manager())
    client = TestClient(supervisor.app)

    response = client.get("/api/supervisor/public/update-status")

    assert response.status_code == 200
    assert response.json()["status"]["state"] == "restarting"


def test_spawn_runtime_locked_prefers_active_slot_manifest(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 4242

        @staticmethod
        def poll():
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": "/slot/repo",
            "env": {"PYTHONPATH": "/slot/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"A": {"path": "/slots/A"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_runtime_locked())

    assert captured["args"][0] == "/slot/python"
    assert captured["kwargs"]["cwd"] == "/slot/repo"
    assert captured["kwargs"]["env"]["PYTHONPATH"] == "/slot/repo/src"
    assert captured["kwargs"]["env"]["ADAOS_ACTIVE_CORE_SLOT"] == "A"
