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


def test_autostart_runner_reconciles_root_promotion_restart_before_idle(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(autostart_runner, "_parse_args", lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})())
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "read_status",
        lambda: {"state": "succeeded", "phase": "root_promoted", "message": "restart adaos.service to activate"},
    )
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert captured
    assert captured[0]["state"] == "succeeded"
    assert captured[0]["phase"] == "validate"
    assert "root promotion restart completed" in captured[0]["message"]


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


def test_autostart_runner_clears_plan_and_exits_after_successful_apply(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: {"target_rev": "rev2026", "target_slot": "B"})
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(
        autostart_runner,
        "execute_pending_update",
        lambda plan: {
            "state": "succeeded",
            "returncode": 0,
            "target_slot": "B",
            "manifest": {"slot": "B"},
            "started_at": 10.0,
        },
    )
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: calls.append("clear_plan"))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", dict(payload))))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (_ for _ in ()).throw(AssertionError("should exit before bind resolution")))

    try:
        autostart_runner.main()
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert "clear_plan" in calls
    status_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "write_status"]
    assert status_calls
    payload = status_calls[-1][1]
    assert payload["state"] == "restarting"
    assert payload["phase"] == "launch"
    assert payload["target_slot"] == "B"


def test_launch_active_slot_marks_child_to_skip_pending_update(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {"slot": "B", "argv": ["python", "-m", "adaos.apps.autostart_runner"], "env": {}, "cwd": ""},
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    captured_env: dict[str, str] = {}

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    def _popen(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _Proc()

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", _popen)
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: payload)

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert captured_env[autostart_runner._SKIP_PENDING_UPDATE_ENV] == "1"


def test_launch_active_slot_marks_root_promotion_pending_when_manifest_requires_it(monkeypatch) -> None:
    monkeypatch.setattr(autostart_runner, "active_slot", lambda: "B")
    monkeypatch.setattr(
        autostart_runner,
        "active_slot_manifest",
        lambda: {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "env": {},
            "cwd": "",
            "bootstrap_update": {"required": True, "changed_paths": ["src/adaos/apps/supervisor.py"]},
        },
    )
    monkeypatch.setattr(autostart_runner, "_slot_launch_spec", lambda manifest, host, port, token=None: (["python"], None))
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: f"/slots/{slot}")

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def terminate(self):
            raise AssertionError("terminate should not be called on validation success")

        def kill(self):
            raise AssertionError("kill should not be called on validation success")

    monkeypatch.setattr(autostart_runner.subprocess, "Popen", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr(autostart_runner, "_probe_update_runtime", lambda **kwargs: (True, {"ok": True}))
    monkeypatch.setattr(autostart_runner, "_run_post_commit_skill_checks", lambda: {"ok": True, "failed_total": 0, "deactivated_total": 0})
    monkeypatch.setattr(autostart_runner, "clear_plan", lambda: None)
    captured: list[dict] = []
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: captured.append(dict(payload)))

    args = types.SimpleNamespace(token="dev-local-token")
    try:
        autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8777, validate=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")

    assert captured[-1]["state"] == "validated"
    assert captured[-1]["phase"] == "root_promotion_pending"
    assert captured[-1]["root_promotion_required"] is True


def test_launch_active_slot_respects_process_slot_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")
    monkeypatch.setattr(autostart_runner, "active_slot_manifest", lambda: (_ for _ in ()).throw(AssertionError("should not read manifest")))
    monkeypatch.setattr(autostart_runner.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not launch subprocess")))

    args = types.SimpleNamespace(token="dev-local-token")

    assert autostart_runner._launch_active_slot_if_needed(args, host="127.0.0.1", port=8778, validate=False) is None


def test_autostart_runner_skips_pending_update_when_requested(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []

    monkeypatch.setattr(
        autostart_runner,
        "_parse_args",
        lambda: type("Args", (), {"host": "127.0.0.1", "port": 8777, "token": None})(),
    )
    monkeypatch.setattr(autostart_runner, "init_ctx", lambda: None)
    monkeypatch.setattr(autostart_runner, "read_plan", lambda: calls.append("read_plan"))
    monkeypatch.setattr(autostart_runner, "load_config", lambda: None)
    monkeypatch.setattr(autostart_runner, "execute_pending_update", lambda plan: calls.append(("execute", plan)))
    monkeypatch.setattr(autostart_runner, "write_status", lambda payload: calls.append(("write_status", payload.get("state"))))
    monkeypatch.setattr(autostart_runner, "_resolve_bind", lambda conf, host, port: (host, port))
    monkeypatch.setattr(autostart_runner, "_advertise_base", lambda host, port: f"http://{host}:{port}")
    monkeypatch.setattr(autostart_runner, "_stop_previous_server", lambda host, port: None)
    monkeypatch.setattr(autostart_runner, "_pidfile_path", lambda host, port: tmp_path / "serve.json")
    monkeypatch.setattr(autostart_runner, "_write_pidfile", lambda path, **kwargs: path.write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(
        autostart_runner,
        "_launch_active_slot_if_needed",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setenv(autostart_runner._SKIP_PENDING_UPDATE_ENV, "1")

    try:
        autostart_runner.main()
    except SystemExit:
        pass

    assert "read_plan" not in calls
    assert not any(isinstance(item, tuple) and item[0] == "execute" for item in calls)


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
    assert details["timeout_sec"] == 1.0
    assert "y server task crashed" in str(details["summary"])


def test_update_validation_timeout_sec_defaults_to_45_seconds(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_TIMEOUT_SEC", raising=False)

    assert autostart_runner._update_validation_timeout_sec() == 45.0


def test_probe_update_runtime_succeeds_after_initial_ping_failures(monkeypatch) -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return dict(self._payload)

    attempts = {"ping": 0}

    def _fake_get(url: str, headers=None, timeout=None):
        if url.endswith("/api/ping"):
            attempts["ping"] += 1
            if attempts["ping"] < 3:
                raise autostart_runner.requests.ConnectionError("connection refused")
            return _Response(200, {"ok": True})
        if url.endswith("/api/status"):
            return _Response(200, {"ok": True})
        if url.endswith("/api/admin/update/status"):
            return _Response(
                200,
                {
                    "ok": True,
                    "slots": {"active_slot": "B"},
                    "active_manifest": {"slot": "B"},
                },
            )
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
                        "assessment": {"state": "ready"},
                        "transport": {"server_ready": True},
                    },
                },
            )
        raise AssertionError(f"unexpected url {url}")

    class _FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def time(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += float(seconds)

    clock = _FakeClock()
    monkeypatch.setattr(autostart_runner.requests, "get", _fake_get)
    monkeypatch.setattr(autostart_runner.time, "time", clock.time)
    monkeypatch.setattr(autostart_runner.time, "sleep", clock.sleep)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT", raising=False)
    monkeypatch.delenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME", raising=False)

    ok, details = autostart_runner._probe_update_runtime(
        host="127.0.0.1",
        port=8777,
        token="dev-local-token",
        timeout_sec=2.0,
        expected_slot="B",
    )

    assert ok is True
    assert details["attempts"] == 3
    assert details["timeout_sec"] == 2.0
    assert details["last_attempt"]["checks"][0]["ok"] is True
