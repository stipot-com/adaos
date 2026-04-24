from __future__ import annotations

from pathlib import Path


class _FakePaths:
    def __init__(self, root: Path) -> None:
        self._root = root

    def base_dir(self) -> Path:
        return self._root

    def repo_root(self) -> Path:
        return self._root


class _FakeSettings:
    profile = "default"


class _FakeCtx:
    def __init__(self, root: Path) -> None:
        self.paths = _FakePaths(root)
        self.settings = _FakeSettings()


def test_linux_restart_service_times_out_with_status_details(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setattr(autostart, "_linux_restart_wait_timeout", lambda scope, service_name: 45.0)
    monkeypatch.setattr(
        autostart,
        "status",
        lambda ctx: {
            "scope": "system",
            "service": "/etc/systemd/system/adaos.service",
            "host": "127.0.0.1",
            "port": 8778,
            "service_main_pid": 111,
        },
    )

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(autostart.subprocess, "run", lambda *args, **kwargs: _Proc())

    class _RunProc:
        def __init__(self, *, returncode=0, stdout="", stderr="") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd: list[str]):
        if cmd == ["systemctl", "is-active", "adaos.service"]:
            return _RunProc(returncode=3, stdout="activating")
        if cmd == ["systemctl", "status", "adaos.service", "--no-pager", "--lines=40"]:
            return _RunProc(returncode=3, stdout="Active: activating (auto-restart)")
        raise AssertionError(f"unexpected _run command: {cmd}")

    monkeypatch.setattr(autostart, "_run", _fake_run)
    monkeypatch.setattr(autostart, "_linux_service_main_pid", lambda scope: 111)
    monkeypatch.setattr(autostart, "_discover_live_control_bind", lambda host, port: None)

    ticks = iter([0.0, 0.2, 45.2])
    monkeypatch.setattr(autostart.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(autostart.time, "sleep", lambda _: None)

    try:
        autostart.restart_service(_FakeCtx(tmp_path))
    except RuntimeError as exc:
        message = str(exc)
        assert "timed out waiting for adaos.service to restart" in message
        assert "Active: activating (auto-restart)" in message
        assert "listening: False" in message
    else:
        raise AssertionError("expected restart_service() to raise on timeout")


def test_linux_restart_service_waits_for_slow_systemd_restart(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setattr(autostart, "_linux_restart_wait_timeout", lambda scope, service_name: 120.0)
    monkeypatch.setattr(
        autostart,
        "status",
        lambda ctx: {
            "scope": "system",
            "service": "/etc/systemd/system/adaos.service",
            "host": "127.0.0.1",
            "port": 8778,
            "service_main_pid": 111,
        },
    )

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(autostart.subprocess, "run", lambda *args, **kwargs: _Proc())

    class _RunProc:
        def __init__(self, *, returncode=0, stdout="", stderr="") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    active_calls = {"count": 0}

    def _fake_run(cmd: list[str]):
        if cmd == ["systemctl", "is-active", "adaos.service"]:
            active_calls["count"] += 1
            if active_calls["count"] < 3:
                return _RunProc(returncode=3, stdout="deactivating")
            return _RunProc(returncode=0, stdout="active")
        raise AssertionError(f"unexpected _run command: {cmd}")

    monkeypatch.setattr(autostart, "_run", _fake_run)
    pid_values = iter([111, 111, 222])
    monkeypatch.setattr(autostart, "_linux_service_main_pid", lambda scope: next(pid_values))
    monkeypatch.setattr(autostart, "_discover_live_control_bind", lambda host, port: ("127.0.0.1", 8778))

    ticks = iter([0.0, 30.0, 60.0, 60.1])
    monkeypatch.setattr(autostart.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(autostart.time, "sleep", lambda _: None)

    payload = autostart.restart_service(_FakeCtx(tmp_path))

    assert payload["ok"] is True
    assert payload["service_main_pid"] == 222
    assert payload["url"] == "http://127.0.0.1:8778"
