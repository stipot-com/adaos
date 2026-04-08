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
    assert "process: pid=3210 kind=autostart_runner status=running cpu=12.5%" in result.output
    assert "pid=4001 kind=skill_runtime cpu=97.2%" in result.output
    assert "weather: running pid=4001 http://127.0.0.1:9123 health=ok" in result.output


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
