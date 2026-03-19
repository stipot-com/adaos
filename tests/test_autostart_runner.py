from __future__ import annotations

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
