from __future__ import annotations

import sys
import types

from typer.testing import CliRunner

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.apps.cli.commands.setup import autostart_app
from adaos.apps.cli.commands import setup as setup_cmd
from requests import ConnectionError as RequestsConnectionError


def test_autostart_update_status_uses_local_admin_api(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "idle", "message": "boot", "target_rev": "rev2026"},
            "slots": {
                "active_slot": "A",
                "previous_slot": "B",
                "slots": {
                    "A": {
                        "manifest": {
                            "target_version": "0.1.0",
                            "git_short_commit": "8e2f6e75",
                            "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                            "git_branch": "rev2026",
                            "git_subject": "feat: add git webhook",
                        }
                    },
                    "B": {"manifest": {"target_version": "0.1.0", "git_short_commit": "4a525775", "git_branch": "rev2026"}},
                },
            },
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "target rev: rev2026" in result.output
    assert "active slot: A | 0.1.0 | 8e2f6e75 | rev2026" in result.output
    assert "active commit: 8e2f6e7529b60f67094a7951e690558c67fdf333" in result.output


def test_autostart_update_status_falls_back_to_active_manifest_payload(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "idle", "message": "boot"},
            "slots": {
                "active_slot": "B",
                "previous_slot": "A",
                "slots": {
                    "A": {"manifest": {}},
                    "B": {"manifest": {}},
                },
            },
            "active_manifest": {
                "target_version": "0.1.0",
                "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                "git_branch": "rev2026",
                "git_subject": "feat: add git webhook",
            },
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "active slot: B | 0.1.0 | 8e2f6e75 | rev2026" in result.output
    assert "active commit: 8e2f6e7529b60f67094a7951e690558c67fdf333" in result.output


def test_autostart_update_status_prints_supervisor_attempt(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "attempt": {"state": "awaiting_root_restart"},
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "supervisor attempt: awaiting_root_restart" in result.output
    assert "next step: supervisor/bootstrap update is promoted; ensure adaos.service restart completes" in result.output


def test_autostart_update_status_prints_planned_schedule_and_subsequent_transition(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {
                "state": "planned",
                "phase": "scheduled",
                "scheduled_for": 1776000000.0,
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 1775999700.0,
            },
            "attempt": {"state": "planned"},
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "scheduled for:" in result.output
    assert "subsequent transition: queued" in result.output


def test_autostart_update_defer_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True, "planned": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["update-defer", "--delay-sec", "900", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/defer"
    assert captured["body"]["delay_sec"] == 900.0
    assert captured["body"]["reason"] == "cli.core_update.defer"


def test_autostart_update_status_prints_scheduled_and_subsequent_transition(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {
                "state": "planned",
                "phase": "scheduled",
                "scheduled_for": 1_775_966_400.0,
                "subsequent_transition": True,
            },
            "attempt": {
                "state": "planned",
                "subsequent_transition": True,
                "subsequent_transition_requested_at": 1_775_966_100.0,
                "candidate_prewarm_state": "starting",
                "candidate_prewarm_message": "passive candidate runtime is still warming on http://127.0.0.1:8778",
            },
            "runtime": {
                "transition_mode": "warm_switch",
                "candidate_slot": "B",
                "candidate_runtime_state": "starting",
                "candidate_runtime_url": "http://127.0.0.1:8778",
            },
            "slots": {"active_slot": "A", "previous_slot": "B", "slots": {}},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "scheduled for:" in result.output
    assert "subsequent transition: queued" in result.output
    assert "transition mode: warm_switch" in result.output
    assert "candidate prewarm: starting" in result.output


def test_autostart_update_defer_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True, "planned": True}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)

    result = runner.invoke(autostart_app, ["update-defer", "--delay-sec", "900", "--reason", "test.defer", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/defer"
    assert captured["body"] == {"delay_sec": 900.0, "reason": "test.defer"}


def test_autostart_smoke_update_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+1.abc"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_admin_post", _post)

    result = runner.invoke(autostart_app, ["smoke-update", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/admin/update/start"
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+1.abc"
    assert captured["body"]["reason"] == "cli.smoke_update"


def test_autostart_update_start_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+2.def"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_admin_post", _post)

    result = runner.invoke(autostart_app, ["update-start", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+2.def"


def test_autostart_update_promote_root_posts_to_supervisor(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_update_post", _post)

    result = runner.invoke(autostart_app, ["update-promote-root", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/promote-root"
    assert captured["body"]["reason"] == "cli.core_update.root_promotion"


def test_autostart_update_complete_promotes_root_and_restarts_service(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_update_get",
        lambda token=None: {
            "ok": True,
            "status": {"state": "validated", "phase": "root_promotion_pending"},
            "runtime": {"root_promotion_required": True},
        },
    )

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True, "status": {"phase": "root_promoted"}}

    monkeypatch.setattr(setup_cmd, "_autostart_supervisor_post", _post)
    monkeypatch.setattr(
        setup_cmd,
        "_restart_autostart_service",
        lambda: {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]},
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/supervisor/update/promote-root"
    assert captured["body"]["reason"] == "cli.core_update.complete"
    assert '"service": "adaos.service"' in result.output


def test_autostart_update_complete_retries_restart_when_root_already_promoted(monkeypatch) -> None:
    runner = CliRunner()
    captured = {"restart_calls": 0}

    monkeypatch.setattr(
        setup_cmd,
        "_autostart_update_get",
        lambda token=None: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "attempt": {"state": "awaiting_root_restart"},
            "runtime": {"root_promotion_required": False, "bootstrap_update": {"required": False}},
        },
    )
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("promote-root should not be called")),
    )

    def _restart():
        captured["restart_calls"] += 1
        return {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]}

    monkeypatch.setattr(setup_cmd, "_restart_autostart_service", _restart)

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["restart_calls"] == 1
    assert "already completed" in result.output


def test_autostart_update_complete_noops_when_root_promotion_not_required(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_update_get",
        lambda token=None: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "validate"},
            "runtime": {"root_promotion_required": False, "bootstrap_update": {"required": False}},
        },
    )

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert '"noop": true' in result.output.lower()
    assert "root promotion is not required" in result.output


def test_autostart_update_complete_retries_restart_for_root_promoted_without_attempt_payload(monkeypatch) -> None:
    runner = CliRunner()
    captured = {"restart_calls": 0}
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_update_get",
        lambda token=None: {
            "ok": True,
            "status": {"state": "succeeded", "phase": "root_promoted"},
            "runtime": {"root_promotion_required": False, "bootstrap_update": {"required": False}},
        },
    )
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_supervisor_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("promote-root should not be called")),
    )

    def _restart():
        captured["restart_calls"] += 1
        return {"ok": True, "scope": "system", "service": "adaos.service", "command": ["systemctl", "restart", "adaos.service"]}

    monkeypatch.setattr(setup_cmd, "_restart_autostart_service", _restart)

    result = runner.invoke(autostart_app, ["update-complete", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["restart_calls"] == 1
    assert "autostart service restart requested" in result.output


def test_autostart_update_status_reports_service_unavailable(monkeypatch) -> None:
    runner = CliRunner()

    def _boom(path, *, token=None):
        raise RuntimeError(
            "local AdaOS admin API is unavailable; the service may be restarting or failed to boot. "
            "Inspect 'journalctl --user -u adaos.service -n 120 --no-pager' and '.adaos/state/core_update/status.json'."
        )

    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _boom)

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code != 0
    assert "local AdaOS admin API is unavailable" in result.output


def test_autostart_update_status_falls_back_to_local_runner_state(monkeypatch) -> None:
    runner = CliRunner()

    def _boom(path, *, token=None):
        raise RuntimeError(
            "local AdaOS admin API is unavailable at http://127.0.0.1:8777; the service may be restarting or failed to boot. "
            "Inspect 'journalctl --user -u adaos.service -n 120 --no-pager' and '.adaos/state/core_update/status.json'."
        )

    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _boom)
    monkeypatch.setattr(
        setup_cmd,
        "_local_autostart_update_payload",
        lambda: {
            "ok": True,
            "status": {"state": "idle", "message": "autostart runner boot"},
            "slots": {
                "active_slot": "B",
                "previous_slot": "A",
                "slots": {
                    "A": {"manifest": {"target_version": "0.1.0", "git_short_commit": "54e4a96a", "git_branch": "rev2026"}},
                    "B": {"manifest": {"target_version": "0.1.1", "git_short_commit": "8e2f6e75", "git_branch": "rev2026"}},
                },
            },
            "active_manifest": {
                "target_version": "0.1.1",
                "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                "git_branch": "rev2026",
            },
            "_local_fallback": True,
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "message: autostart runner boot" in result.output
    assert "active slot: B | 0.1.1 | 8e2f6e75 | rev2026" in result.output


def test_autostart_inspect_renders_hot_children_and_services(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_collect_autostart_inspect",
        lambda sample_sec=0.2, token=None: {
            "autostart": {
                "enabled": True,
                "active": True,
                "listening": True,
                "url": "http://127.0.0.1:8777",
            },
            "bind": {"host": "127.0.0.1", "port": 8777},
            "process": {
                "pid": 3210,
                "root": {
                    "pid": 3210,
                    "kind": "autostart_runner",
                    "status": "running",
                    "cpu_percent": 12.5,
                    "rss_bytes": 64 * 1024 * 1024,
                    "threads": 17,
                    "age_sec": 93,
                    "cmdline_text": "python -m adaos.apps.autostart_runner --host 127.0.0.1 --port 8777",
                },
                "top_children": [
                    {
                        "pid": 4001,
                        "kind": "skill_runtime",
                        "cpu_percent": 97.2,
                        "rss_bytes": 128 * 1024 * 1024,
                        "threads": 9,
                        "age_sec": 40,
                        "cmdline_text": "python skills/runtime_runner.py weather",
                    }
                ],
            },
            "services": [
                {
                    "name": "weather",
                    "running": True,
                    "pid": 4001,
                    "base_url": "http://127.0.0.1:9123",
                    "health_ok": True,
                }
            ],
        },
    )

    result = runner.invoke(autostart_app, ["inspect"])

    assert result.exit_code == 0, result.output
    assert "autostart: enabled=True active=True listening=True" in result.output
    assert "runtime: pid=3210 kind=autostart_runner status=running cpu=12.5%" in result.output
    assert "pid=4001 kind=skill_runtime cpu=97.2%" in result.output
    assert "weather: running pid=4001 http://127.0.0.1:9123 health=ok" in result.output


def test_autostart_inspect_renders_service_and_supervisor_sections(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_collect_autostart_inspect",
        lambda sample_sec=0.2, token=None: {
            "autostart": {
                "enabled": True,
                "active": True,
                "listening": False,
                "url": "http://127.0.0.1:8777",
            },
            "bind": {"host": "127.0.0.1", "port": 8777},
            "service_process": {
                "pid": 11939,
                "root": {
                    "pid": 11939,
                    "kind": "supervisor",
                    "status": "sleeping",
                    "cpu_percent": 0.0,
                    "rss_bytes": 32 * 1024 * 1024,
                    "threads": 4,
                    "age_sec": 20,
                    "cmdline_text": "python -m adaos.apps.supervisor --host 127.0.0.1 --port 8777",
                },
            },
            "supervisor": {
                "url": "http://127.0.0.1:8776",
                "reachable": True,
                "process": {
                    "pid": 11939,
                    "kind": "supervisor",
                    "status": "sleeping",
                    "cpu_percent": 0.0,
                    "rss_bytes": 32 * 1024 * 1024,
                    "threads": 4,
                    "age_sec": 20,
                    "cmdline_text": "python -m adaos.apps.supervisor --host 127.0.0.1 --port 8777",
                },
            },
            "runtime_process": {
                "pid": 11941,
                "root": {
                    "pid": 11941,
                    "kind": "autostart_runner",
                    "status": "running",
                    "cpu_percent": 18.5,
                    "rss_bytes": 64 * 1024 * 1024,
                    "threads": 7,
                    "age_sec": 11,
                    "cmdline_text": "python -m adaos.apps.autostart_runner --host 127.0.0.1 --port 8777",
                },
                "top_children": [],
            },
            "services": [],
        },
    )

    result = runner.invoke(autostart_app, ["inspect"])

    assert result.exit_code == 0, result.output
    assert "service: pid=11939 kind=supervisor" in result.output
    assert "supervisor: url=http://127.0.0.1:8776 reachable=True" in result.output
    assert "runtime: pid=11941 kind=autostart_runner" in result.output


def test_probe_http_json_uses_default_autostart_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"ok": True}

    def _get(url: str, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _Response()

    monkeypatch.setattr(setup_cmd, "_autostart_admin_headers", lambda token=None: {"X-AdaOS-Token": "dev-local-token"})
    monkeypatch.setattr(setup_cmd.requests, "get", _get)

    payload = setup_cmd._probe_http_json("http://127.0.0.1:8776", "/api/supervisor/status")

    assert payload == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:8776/api/supervisor/status"
    assert captured["headers"]["X-AdaOS-Token"] == "dev-local-token"
    assert captured["headers"]["Accept"] == "application/json"


def test_autostart_inspect_json_outputs_payload(monkeypatch) -> None:
    runner = CliRunner()
    payload = {
        "autostart": {"enabled": True, "active": True, "listening": True},
        "bind": {"host": "127.0.0.1", "port": 8777},
        "process": None,
        "services": [],
    }
    monkeypatch.setattr(setup_cmd, "_collect_autostart_inspect", lambda sample_sec=0.2, token=None: payload)

    result = runner.invoke(autostart_app, ["inspect", "--json"])

    assert result.exit_code == 0, result.output
    assert '"host": "127.0.0.1"' in result.output
    assert '"port": 8777' in result.output


def test_select_autostart_target_pid_prefers_pidfile_candidate(monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "_pidfile_path", lambda host, port: object())
    monkeypatch.setattr(setup_cmd, "_read_pidfile", lambda path: {"pid": 2222})
    monkeypatch.setattr(setup_cmd, "_find_listening_server_pid", lambda host, port: 3333)
    monkeypatch.setattr(setup_cmd, "_find_matching_server_pids", lambda host, port, protected_pids=None: [4444])
    monkeypatch.setattr(setup_cmd, "_current_process_family_pids", lambda: {9999})

    class _FakeProc:
        def __init__(self, pid: int):
            self.pid = pid

        def status(self):
            return "running"

    monkeypatch.setattr(setup_cmd.psutil, "Process", _FakeProc)

    pid = setup_cmd._select_autostart_target_pid({}, "127.0.0.1", 8777)

    assert pid == 2222
