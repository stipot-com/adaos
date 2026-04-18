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
                "profile_control_mode": "phase1_intent_only",
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
    assert "memory: mode=normal control=phase1_intent_only suspicion=idle sessions=2" in result.output
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
                        "session_state": "planned",
                        "profile_mode": "sampled_profile",
                        "publish_state": "local_only",
                    }
                ],
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-sessions"])

    assert result.exit_code == 0
    assert "sessions total: 1" in result.output
    assert "session: id=mem-001 state=planned mode=sampled_profile publish=local_only" in result.output


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
                    "session_state": "planned",
                    "profile_mode": "sampled_profile",
                    "publish_state": "publish_requested",
                    "trigger_reason": "operator.request",
                },
                "operations": [
                    {"event": "tool_invoked", "sequence": 1},
                    {"event": "tool_invoked", "sequence": 2},
                ],
            }

    monkeypatch.setattr(runtime_cli.requests, "get", lambda *args, **kwargs: _Response())

    result = CliRunner().invoke(runtime_cli.app, ["memory-session", "mem-001"])

    assert result.exit_code == 0
    assert "session: id=mem-001 state=planned mode=sampled_profile publish=publish_requested" in result.output
    assert "trigger: operator.request" in result.output
    assert "operations: 2" in result.output
    assert "last operation: event=tool_invoked seq=2" in result.output
