from __future__ import annotations

import importlib

from typer.testing import CliRunner


def test_runtime_memory_status_cli_prints_compact_summary(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "current_profile_mode": "normal",
                "profile_control_mode": "phase2_supervisor_restart",
                "suspicion_state": "idle",
                "sessions_total": 2,
                "requested_profile_mode": "sampled_profile",
                "requested_session_id": "mem-002",
                "last_session_id": "mem-001",
                "selected_profiler_adapter": "tracemalloc",
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-status"])

    assert result.exit_code == 0
    assert "memory: mode=normal control=phase2_supervisor_restart suspicion=idle sessions=2" in result.output
    assert "requested: mode=sampled_profile session=mem-002" in result.output
    assert "last session: mem-001" in result.output


def test_runtime_memory_sessions_cli_prints_session_rows(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "total": 1,
                "sessions": [
                    {
                        "session_id": "mem-001",
                        "session_state": "requested",
                        "profile_mode": "sampled_profile",
                        "publish_state": "local_only",
                    }
                ],
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-sessions"])

    assert result.exit_code == 0
    assert "sessions total: 1" in result.output
    assert "session: id=mem-001 state=requested mode=sampled_profile publish=local_only" in result.output


def test_runtime_memory_telemetry_cli_prints_tail(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "total": 2,
                "items": [
                    {"profile_mode": "normal", "suspicion_state": "idle", "family_rss_bytes": 128, "rss_growth_bytes": 0},
                    {"profile_mode": "sampled_profile", "suspicion_state": "suspected", "family_rss_bytes": 256, "rss_growth_bytes": 64},
                ],
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-telemetry", "--limit", "2"])

    assert result.exit_code == 0
    assert "telemetry samples: 2" in result.output
    assert "last sample: mode=sampled_profile suspicion=suspected family_rss=256 growth=64" in result.output


def test_runtime_memory_session_cli_prints_details(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "session": {
                    "session_id": "mem-001",
                    "session_state": "requested",
                    "profile_mode": "sampled_profile",
                    "publish_state": "publish_requested",
                    "trigger_reason": "operator.request",
                },
                "operations": [
                    {"event": "tool_invoked", "sequence": 1},
                    {"event": "tool_invoked", "sequence": 2},
                ],
                "telemetry": [{"sampled_at": 1.0}, {"sampled_at": 2.0}],
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-session", "mem-001"])

    assert result.exit_code == 0
    assert "session: id=mem-001 state=requested mode=sampled_profile publish=publish_requested" in result.output
    assert "trigger: operator.request" in result.output
    assert "operations: 2" in result.output
    assert "last operation: event=tool_invoked seq=2" in result.output
    assert "telemetry: 2" in result.output


def test_runtime_memory_profile_start_cli_posts_intent(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "control_mode": "phase2_supervisor_restart",
                "session": {
                    "session_id": "mem-001",
                    "session_state": "requested",
                    "profile_mode": "sampled_profile",
                },
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Response()

    monkeypatch.setattr(runtime_cli.requests, "post", _fake_post)

    result = CliRunner().invoke(runtime_cli.app, ["memory-profile-start"])

    assert result.exit_code == 0
    assert captured["url"] == "http://127.0.0.1:8777/api/supervisor/memory/profile/start"
    assert captured["json"]["profile_mode"] == "sampled_profile"
    assert "memory profile start: id=mem-001 state=requested mode=sampled_profile" in result.output
    assert "control mode: phase2_supervisor_restart" in result.output


def test_runtime_memory_profile_stop_cli_posts_intent(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "control_mode": "phase2_supervisor_restart",
                "session": {
                    "session_id": "mem-001",
                    "session_state": "cancelled",
                },
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Response()

    monkeypatch.setattr(runtime_cli.requests, "post", _fake_post)

    result = CliRunner().invoke(runtime_cli.app, ["memory-profile-stop", "mem-001"])

    assert result.exit_code == 0
    assert captured["url"] == "http://127.0.0.1:8777/api/supervisor/memory/profile/mem-001/stop"
    assert "memory profile stop: id=mem-001 state=cancelled" in result.output
    assert "control mode: phase2_supervisor_restart" in result.output


def test_runtime_memory_publish_cli_posts_intent(monkeypatch) -> None:
    runtime_cli = importlib.import_module("adaos.apps.cli.commands.runtime")

    monkeypatch.setattr(runtime_cli, "resolve_control_base_url", lambda explicit=None, prefer_local=True: "http://127.0.0.1:8777")
    monkeypatch.setattr(runtime_cli, "resolve_control_token", lambda explicit=None, base_url=None: "dev-token")

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "control_mode": "phase2_supervisor_restart",
                "session": {
                    "session_id": "mem-001",
                    "publish_state": "publish_requested",
                },
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Response()

    monkeypatch.setattr(runtime_cli.requests, "post", _fake_post)

    result = CliRunner().invoke(runtime_cli.app, ["memory-publish", "mem-001"])

    assert result.exit_code == 0
    assert captured["url"] == "http://127.0.0.1:8777/api/supervisor/memory/publish"
    assert captured["json"]["session_id"] == "mem-001"
    assert "memory publish: id=mem-001 publish=publish_requested" in result.output
    assert "control mode: phase2_supervisor_restart" in result.output
