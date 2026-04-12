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


def test_last_update_completion_at_ignores_idle_status() -> None:
    assert supervisor._last_update_completion_at({"state": "idle", "updated_at": 123.0}, None) == 0.0


def test_supervisor_start_update_and_cancel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "0")
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "prepared",
            "phase": "prepare",
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "plan": {"target_slot": "B"},
            "finished_at": 123.0,
        },
    )
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


def test_supervisor_prepare_failure_does_not_request_runtime_shutdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "0")
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "failed",
            "phase": "prepare",
            "message": "prepare exploded",
            "target_slot": "B",
            "plan": {"target_slot": "B"},
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _unexpected_shutdown(**kwargs):
        raise AssertionError("runtime shutdown must not be requested when prepare fails")

    monkeypatch.setattr(manager, "_request_runtime_shutdown", _unexpected_shutdown)

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
        task = manager._update_task
        assert task is not None
        await task
        status = read_status()
        assert status["state"] == "failed"
        assert status["phase"] == "prepare"
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "failed"

    asyncio.run(_exercise())


def test_prepare_worker_writes_prepared_restart_plan_and_reenables_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        supervisor,
        "prepare_pending_update",
        lambda plan: {
            "state": "prepared",
            "phase": "prepare",
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "plan": {"target_slot": "B"},
            "finished_at": 222.0,
        },
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    lifecycle_calls: list[str] = []
    desired_running_states: list[bool] = []
    activated_slots: list[str] = []

    async def _shutdown(**kwargs):
        lifecycle_calls.append("shutdown")
        return {"ok": True}

    async def _ensure_stopped(**kwargs):
        lifecycle_calls.append("stopped")
        return {"ok": True, "forced": False}

    monkeypatch.setattr(manager, "_request_runtime_shutdown", _shutdown)
    monkeypatch.setattr(manager, "_ensure_runtime_stopped_for_update", _ensure_stopped)
    monkeypatch.setattr(supervisor, "activate_slot", lambda slot: activated_slots.append(str(slot)))
    monkeypatch.setattr(manager, "_persist_runtime_state", lambda: desired_running_states.append(bool(manager._desired_running)))

    asyncio.run(
        manager._prepare_and_countdown_update_worker(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=0.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    plan = read_plan()
    assert isinstance(plan, dict)
    assert plan["state"] == "prepared_restart"
    assert plan["target_slot"] == "B"
    status = read_status()
    assert status["state"] == "restarting"
    assert status["phase"] == "launch"
    assert activated_slots == ["B"]
    assert lifecycle_calls == ["shutdown", "stopped"]
    assert False in desired_running_states
    assert desired_running_states[-1] is True


def test_supervisor_start_update_schedules_when_min_period_not_elapsed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "completed_at": 450.0,
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["planned"] is True
    status = read_status()
    assert status["state"] == "planned"
    assert status["planned_reason"] == "minimum_update_period"
    assert status["scheduled_for"] == 750.0
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "planned"
    assert attempt["scheduled_for"] == 750.0


def test_supervisor_start_update_refreshes_existing_planned_update(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.older",
            "scheduled_for": 750.0,
            "planned_reason": "minimum_update_period",
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.older",
            "scheduled_for": 750.0,
            "planned_reason": "minimum_update_period",
            "updated_at": 450.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.refresh",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["planned"] is True
    assert result["status"]["scheduled_for"] == 750.0
    assert result["status"]["message"] == "planned core update refreshed while waiting for scheduled window"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "planned"
    assert attempt["target_version"] == "1.2.3"
    assert attempt["scheduled_for"] == 750.0


def test_supervisor_start_update_queues_subsequent_transition_while_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.active",
            "scheduled_for": 530.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "reason": "test.active",
            "scheduled_for": 530.0,
            "updated_at": 500.0,
        }
    )

    result = asyncio.run(
        manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.3",
            reason="test.subsequent",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
    )

    assert result["accepted"] is True
    assert result["deferred"] is True
    assert result["subsequent_transition"] is True
    status = read_status()
    assert status["subsequent_transition"] is True
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["subsequent_transition"] is True
    assert attempt["subsequent_transition_request"]["target_version"] == "1.2.3"


def test_supervisor_monitor_runs_subsequent_transition_once_after_completion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    monkeypatch.setattr(supervisor.time, "time", lambda: 800.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "target_rev": "rev2026",
            "updated_at": 799.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "completed",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.2",
            "subsequent_transition": True,
            "subsequent_transition_requested_at": 780.0,
            "subsequent_transition_request": {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": "1.2.3",
                "reason": "test.subsequent",
                "countdown_sec": 15.0,
                "drain_timeout_sec": 10.0,
                "signal_delay_sec": 0.25,
                "requested_at": 780.0,
            },
            "updated_at": 799.0,
        }
    )
    calls: list[dict[str, object]] = []

    async def _capture(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "start_update", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert len(calls) == 1
    assert calls[0]["target_version"] == "1.2.3"
    assert calls[0]["bypass_min_period"] is True


def test_supervisor_start_update_queues_subsequent_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "scheduled_for": 9999999999.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "requested_at": 1.0,
            "updated_at": 1.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2027",
            target_version="2.0.0",
            reason="test.update.next",
            countdown_sec=45.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        assert result["deferred"] is True
        assert result["subsequent_transition"] is True
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["subsequent_transition"] is True
        assert attempt["subsequent_transition_request"]["target_rev"] == "rev2027"
        status = read_status()
        assert status["subsequent_transition"] is True

    asyncio.run(_exercise())


def test_supervisor_start_update_schedules_planned_update_when_min_period_not_elapsed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC", "300")
    monkeypatch.setattr(supervisor.time, "time", lambda: 150.0)
    write_status(
        {
            "state": "succeeded",
            "phase": "validate",
            "action": "update",
            "finished_at": 100.0,
            "updated_at": 100.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _exercise() -> None:
        result = await manager.start_update(
            action="update",
            target_rev="rev2026",
            target_version="1.2.4",
            reason="test.update",
            countdown_sec=30.0,
            drain_timeout_sec=10.0,
            signal_delay_sec=0.25,
        )
        assert result["accepted"] is True
        assert result["planned"] is True
        status = read_status()
        assert status["state"] == "planned"
        assert status["phase"] == "scheduled"
        assert status["planned_reason"] == "minimum_update_period"
        assert status["scheduled_for"] == 400.0
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "planned"
        assert attempt["scheduled_for"] == 400.0

    asyncio.run(_exercise())


def test_supervisor_defer_update_reschedules_active_countdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status(
        {
            "state": "countdown",
            "phase": "countdown",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "scheduled_for": 200.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "requested_at": 100.0,
            "updated_at": 100.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    async def _sleep_forever() -> None:
        await asyncio.Future()

    async def _exercise() -> None:
        monkeypatch.setattr(supervisor.time, "time", lambda: 150.0)
        manager._update_task = asyncio.create_task(_sleep_forever())
        try:
            result = await manager.defer_update(delay_sec=300.0, reason="test.defer")
        finally:
            if manager._update_task is not None and not manager._update_task.done():
                manager._update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await manager._update_task
        assert result["accepted"] is True
        assert result["planned"] is True
        status = read_status()
        assert status["state"] == "planned"
        assert status["planned_reason"] == "operator_defer"
        assert status["scheduled_for"] == 450.0
        attempt = supervisor._read_update_attempt()
        assert isinstance(attempt, dict)
        assert attempt["state"] == "planned"
        assert attempt["scheduled_for"] == 450.0

    import contextlib

    asyncio.run(_exercise())


def test_supervisor_monitor_resumes_due_planned_transition(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor.time, "time", lambda: 500.0)
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
        }
    )
    supervisor._write_update_attempt(
        {
            "state": "planned",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "countdown_sec": 30.0,
            "drain_timeout_sec": 10.0,
            "signal_delay_sec": 0.25,
            "scheduled_for": 499.0,
            "updated_at": 490.0,
        }
    )
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    calls: list[dict] = []

    def _capture(request: dict, *, countdown_sec: float | None = None) -> dict:
        calls.append({"request": dict(request), "countdown_sec": countdown_sec})
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(manager, "_begin_countdown_transition", _capture)

    asyncio.run(manager._maybe_resume_or_continue_transition())

    assert calls
    assert calls[0]["request"]["target_rev"] == "rev2026"


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


def test_runtime_state_payload_surfaces_warm_switch_admission(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                return type("Mem", (), {"rss": 256 * 1024 * 1024})()

        @staticmethod
        def virtual_memory():
            return type("VM", (), {"available": 1024 * 1024 * 1024})()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "planned_reason": "minimum_update_period",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["runtime_port"] == 8777
    assert payload["candidate_slot"] == "B"
    assert payload["candidate_runtime_port"] == 8778
    assert payload["transition_mode"] == "warm_switch"
    assert payload["warm_switch_supported"] is True
    assert payload["warm_switch_allowed"] is True
    assert payload["slot_ports"]["A"] == 8777
    assert payload["slot_ports"]["B"] == 8778


def test_runtime_state_payload_falls_back_to_stop_and_switch_when_memory_is_low(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _Proc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path)

        @staticmethod
        def poll():
            return None

    class _Psutil:
        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def memory_info(self):
                return type("Mem", (), {"rss": 256 * 1024 * 1024})()

        @staticmethod
        def virtual_memory():
            return type("VM", (), {"available": 300 * 1024 * 1024})()

    manager._proc = _Proc()
    write_status(
        {
            "state": "planned",
            "phase": "scheduled",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
        }
    )
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(supervisor, "_listener_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "_runtime_api_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor, "choose_inactive_slot", lambda: "B")
    monkeypatch.setattr(supervisor, "psutil", _Psutil)

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["transition_mode"] == "stop_and_switch"
    assert payload["warm_switch_allowed"] is False
    assert "insufficient memory" in str(payload["warm_switch_reason"] or "")


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


def test_supervisor_promote_root_preserves_subsequent_transition_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")
    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
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
    monkeypatch.setattr(
        supervisor,
        "promote_root_from_slot",
        lambda *, slot=None: {
            "ok": True,
            "slot": slot or "B",
            "required": True,
            "restart_required": True,
            "changed_paths": ["src/adaos/apps/supervisor.py"],
        },
    )
    supervisor._write_update_attempt(
        {
            "state": "active",
            "action": "update",
            "target_rev": "rev2026",
            "target_version": "1.2.3",
            "reason": "test.update",
            "subsequent_transition": True,
            "subsequent_transition_requested_at": 410.0,
            "subsequent_transition_request": {
                "action": "update",
                "target_rev": "rev2026",
                "target_version": "1.2.4",
                "reason": "test.subsequent",
            },
            "updated_at": 400.0,
        }
    )
    write_status({"state": "validated", "phase": "root_promotion_pending", "target_slot": "B"})

    payload = asyncio.run(manager.promote_root(reason="test.root_promotion"))

    assert payload["status"]["phase"] == "root_promoted"
    attempt = supervisor._read_update_attempt()
    assert isinstance(attempt, dict)
    assert attempt["state"] == "awaiting_root_restart"
    assert attempt["subsequent_transition"] is True
    assert attempt["subsequent_transition_request"]["target_version"] == "1.2.4"


def test_public_update_status_payload_is_browser_safe() -> None:
    payload = supervisor._public_update_status_payload(
        {
            "status": {
                "state": "restarting",
                "phase": "shutdown",
                "message": "countdown completed; pending update written",
                "target_rev": "rev2026",
                "target_version": "0.1.0+1.abc",
                "planned_reason": "minimum_update_period",
                "min_update_period_sec": 300.0,
                "scheduled_for": 456.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 400.0,
                "updated_at": 123.0,
                "error": "hidden",
            },
            "runtime": {
                "active_slot": "A",
                "runtime_state": "spawned",
                "runtime_url": "http://127.0.0.1:8777",
                "runtime_port": 8777,
                "runtime_instance_id": "rt-a-a1b2c3d4",
                "transition_role": "active",
                "listener_running": False,
                "runtime_api_ready": False,
                "candidate_slot": "B",
                "candidate_runtime_url": "http://127.0.0.1:8778",
                "candidate_runtime_port": 8778,
                "candidate_runtime_instance_id": "rt-b-c9d8e7f6",
                "candidate_transition_role": "candidate",
                "candidate_listener_running": True,
                "candidate_runtime_api_ready": True,
                "candidate_runtime_state": "ready",
                "transition_mode": "warm_switch",
                "warm_switch_supported": True,
                "warm_switch_allowed": True,
                "warm_switch_reason": "warm switch admitted",
                "slot_ports": {"A": 8777, "B": 8778},
                "root_promotion_required": True,
                "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
                "managed_cmdline": ["hidden"],
            },
            "attempt": {
                "state": "awaiting_root_restart",
                "awaiting_restart": True,
                "planned_reason": "minimum_update_period",
                "scheduled_for": 456.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 400.0,
                "updated_at": 222.0,
            },
            "_served_by": "supervisor_fallback",
        }
    )

    assert payload["ok"] is True
    assert payload["status"]["state"] == "restarting"
    assert payload["status"]["phase"] == "shutdown"
    assert payload["status"]["planned_reason"] == "minimum_update_period"
    assert payload["status"]["scheduled_for"] == 456.0
    assert payload["status"]["subsequent_transition"] is True
    assert payload["attempt"]["state"] == "awaiting_root_restart"
    assert payload["attempt"]["awaiting_restart"] is True
    assert payload["attempt"]["planned_reason"] == "minimum_update_period"
    assert payload["attempt"]["scheduled_for"] == 456.0
    assert payload["attempt"]["subsequent_transition"] is True
    assert payload["runtime"]["active_slot"] == "A"
    assert payload["runtime"]["runtime_instance_id"] == "rt-a-a1b2c3d4"
    assert payload["runtime"]["transition_role"] == "active"
    assert payload["runtime"]["runtime_url"] == "http://127.0.0.1:8777"
    assert payload["runtime"]["candidate_runtime_url"] == "http://127.0.0.1:8778"
    assert payload["runtime"]["candidate_runtime_instance_id"] == "rt-b-c9d8e7f6"
    assert payload["runtime"]["candidate_transition_role"] == "candidate"
    assert payload["runtime"]["candidate_runtime_state"] == "ready"
    assert payload["runtime"]["candidate_runtime_api_ready"] is True
    assert payload["runtime"]["transition_mode"] == "warm_switch"
    assert payload["runtime"]["slot_ports"]["B"] == 8778
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
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_TRANSITION_ROLE"] == "active"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8777"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-a-a-")


def test_spawn_runtime_locked_uses_slot_specific_port_for_slot_b(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 4343

        @staticmethod
        def poll():
            return None

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(supervisor, "active_slot", lambda: "B")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": "/slot/repo",
            "env": {"PYTHONPATH": "/slot/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"B": {"path": "/slots/B"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_runtime_locked())

    assert captured["args"][-1] == "8778"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8778"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-b-a-")


def test_spawn_candidate_runtime_locked_uses_candidate_role_and_skips_pending_update(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 5151

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
        "read_slot_manifest",
        lambda slot: {
            "slot": slot,
            "argv": ["/slot/python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
            "cwd": f"/slots/{slot}/repo",
            "env": {"PYTHONPATH": f"/slots/{slot}/repo/src"},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "core_slot_status",
        lambda: {"slots": {"B": {"path": "/slots/B"}}},
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", _fake_popen)

    asyncio.run(manager._spawn_candidate_runtime_locked(slot="B"))

    assert captured["args"][-1] == "8778"
    assert captured["kwargs"]["cwd"] == "/slots/B/repo"
    assert captured["kwargs"]["env"]["ADAOS_ACTIVE_CORE_SLOT"] == "B"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_TRANSITION_ROLE"] == "candidate"
    assert captured["kwargs"]["env"]["ADAOS_RUNTIME_PORT"] == "8778"
    assert captured["kwargs"]["env"]["ADAOS_SKIP_PENDING_CORE_UPDATE"] == "1"
    assert str(captured["kwargs"]["env"]["ADAOS_RUNTIME_INSTANCE_ID"]).startswith("rt-b-c-")


def test_runtime_state_payload_surfaces_candidate_runtime_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    manager = supervisor.SupervisorManager(runtime_host="127.0.0.1", runtime_port=8777, token="dev-local-token")

    class _ActiveProc:
        pid = 32123
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"]
        cwd = str(tmp_path / "active")

        @staticmethod
        def poll():
            return None

    class _CandidateProc:
        pid = 32124
        args = ["python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8778"]
        cwd = str(tmp_path / "candidate")

        @staticmethod
        def poll():
            return None

    manager._proc = _ActiveProc()
    manager._candidate_proc = _CandidateProc()
    manager._candidate_slot = "B"
    manager._candidate_runtime_instance_id = "rt-b-c-12345678"
    manager._candidate_transition_role = "candidate"
    monkeypatch.setattr(supervisor, "active_slot", lambda: "A")
    monkeypatch.setattr(
        supervisor,
        "active_slot_manifest",
        lambda: {
            "slot": "A",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path / "active"),
        },
    )
    monkeypatch.setattr(
        supervisor,
        "read_slot_manifest",
        lambda slot: {
            "slot": slot,
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "cwd": str(tmp_path / "candidate"),
        },
    )
    monkeypatch.setattr(supervisor, "validate_slot_structure", lambda slot: {"slot": slot, "ok": True, "issues": []})
    monkeypatch.setattr(
        supervisor,
        "_listener_running",
        lambda host, port, **kwargs: int(port) in {8777, 8778},
    )
    monkeypatch.setattr(
        supervisor,
        "_runtime_api_ready",
        lambda base_url, **kwargs: base_url.endswith(":8777") or base_url.endswith(":8778"),
    )

    payload = manager.status()

    assert payload["candidate_slot"] == "B"
    assert payload["candidate_runtime_port"] == 8778
    assert payload["candidate_runtime_instance_id"] == "rt-b-c-12345678"
    assert payload["candidate_transition_role"] == "candidate"
    assert payload["candidate_runtime_state"] == "ready"
    assert payload["candidate_runtime_api_ready"] is True
