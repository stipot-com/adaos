from __future__ import annotations

import types
from pathlib import Path

from adaos.apps import autostart_runner


def test_autostart_runner_initializes_context_before_pidfile(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(autostart_runner, "_parse_args", lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})())
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: calls.append("init_ctx"))
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append("write_status"))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: calls.append("stop_previous"))

    def _pidfile(host, port):
        calls.append("pidfile")
        raise SystemExit(0)

    monkeypatch.setattr(autostart_runner, "_pidfile_path", _pidfile)

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert calls[:3] == ["init_ctx", "write_status", "stop_previous"]
    assert "pidfile" in calls


def test_launch_active_slot_validates_required_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def __init__(self) -> None:
            self.wait_called = False

        def wait(self, timeout=None):
            self.wait_called = True
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    proc = _Proc()
    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, "ok"))
    monkeypatch.setattr(
        autostart_runner,
        "_run_post_commit_skill_checks",
        lambda: {"ok": False, "failed_total": 1, "deactivated_total": 1, "skills": [{"skill": "voice_skill", "ok": False, "failed_stage": "tests", "deactivated": True}]},
    )
    captured: list[dict] = []
    clear_calls: list[str] = []
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: clear_calls.append("clear"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert proc.wait_called is True
    assert clear_calls == ["clear"]
    assert captured[-1]["state"] == "succeeded"
    assert captured[-1]["phase"] == "validate"
    assert captured[-1]["skill_post_commit_checks"]["deactivated_total"] == 1
    assert "skills degraded after commit" in captured[-1]["message"]


def test_launch_active_slot_rolls_back_on_failed_validation(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    proc = _Proc()
    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (False, "http://127.0.0.1:8777/api/admin/update/status returned 500"))
    monkeypatch.setattr(autostart_runner, "rollback_to_previous_slot", lambda: "A")
    monkeypatch.setattr(
        autostart_runner,
        "rollback_installed_skill_runtimes",
        lambda: {"ok": True, "total": 1, "failed_total": 0, "rollback_total": 1, "skills": [{"skill": "weather_skill", "ok": True}]},
    )
    captured: list[dict] = []
    clear_calls: list[str] = []
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: clear_calls.append("clear"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit")

    assert proc.terminated is True
    assert clear_calls == ["clear"]
    assert captured[-1]["phase"] == "validate"
    assert captured[-1]["restored_slot"] == "A"
    assert captured[-1]["rollback"]["ok"] is True
    assert captured[-1]["skill_runtime_rollback"]["rollback_total"] == 1


def test_autostart_runner_keeps_plan_until_validation(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: {"target_rev": "rev2026", "target_slot": "B"})
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "execute_pending_update", lambda plan: {"state": "succeeded", "returncode": 0})
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", payload.get("state"))))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))

    def _launch(*args, **kwargs):
        calls.append(("launch", kwargs.get("validate")))
        raise SystemExit(0)

    monkeypatch.setattr(autostart_runner, "_launch_active_slot_if_needed", _launch)

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert ("launch", True) in calls
    assert "clear_plan" not in calls


def test_autostart_runner_writes_failed_status_on_boot_exception(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    try:
        autostart_runner.main()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert captured[-1]["state"] == "failed"
    assert captured[-1]["phase"] == "launch_active_slot"
    assert captured[-1]["error_type"] == "RuntimeError"
    assert "boom" in str(captured[-1]["error"])


def test_validate_sidecar_runtime_payload_requires_listener_when_enabled() -> None:
    ok, error, details = autostart_runner._validate_sidecar_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "enabled": True,
                "status": "unknown",
                "local_listener_state": "down",
            },
            "process": {
                "listener_running": False,
            },
        }
    )

    assert ok is False
    assert "listener" in str(error).lower()
    assert details["enabled"] is True
    assert details["listener_running"] is False


def test_validate_yjs_runtime_payload_requires_server_ready() -> None:
    ok, error, details = autostart_runner._validate_yjs_runtime_payload(
        {
            "ok": True,
            "runtime": {
                "available": True,
                "selected_webspace_id": "default",
                "assessment": {
                    "state": "degraded",
                    "reason": "yjs_websocket_server_not_ready",
                },
                "transport": {
                    "server_requested": True,
                    "server_task_running": False,
                    "server_ready": False,
                    "server_error": "RuntimeError: bind failed",
                },
            },
        },
        expected_webspace_id="default",
    )

    assert ok is False
    assert "bind failed" in str(error)
    assert details["server_ready"] is False
    assert details["selected_webspace_id"] == "default"


def test_probe_update_runtime_fails_when_runtime_guard_fails(monkeypatch) -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return dict(self._payload)

    def _fake_get(url: str, headers=None, timeout=None):
        if url.endswith("/api/ping"):
            return _Response(200, {"ok": True})
        if url.endswith("/api/node/sidecar/status"):
            return _Response(200, {"ok": True, "runtime": {"enabled": False}, "process": {}})
        if "/api/node/yjs/webspaces/default/runtime" in url:
            return _Response(
                200,
                {
                    "ok": True,
                    "runtime": {
                        "available": True,
                        "selected_webspace_id": "default",
                        "assessment": {
                            "state": "degraded",
                            "reason": "yjs_websocket_server_not_ready",
                        },
                        "transport": {
                            "server_requested": True,
                            "server_task_running": False,
                            "server_ready": False,
                            "server_error": "RuntimeError: y server task crashed",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected url {url}")

    time_values = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(autostart_runner.requests, "get", _fake_get)
    monkeypatch.setattr(autostart_runner.time, "time", lambda: next(time_values))
    monkeypatch.setattr(autostart_runner.time, "sleep", lambda _: None)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT", raising=False)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME", raising=False)

    ok, details = autostart_runner._probe_update_runtime(
        host="127.0.0.1",
        port=8777,
        token="dev-local-token",
        timeout_sec=0.5,
        expected_slot=None,
    )

    assert ok is False
    assert details["runtime_guards"] is True
    assert "y server task crashed" in str(details["summary"])
