from __future__ import annotations

from pathlib import Path

from adaos.services import autostart


class _FakePaths:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def base_dir(self) -> Path:
        return self._base_dir


class _FakeCtx:
    def __init__(self, base_dir: Path) -> None:
        self.paths = _FakePaths(base_dir)


def test_windows_status_detects_disabled_stale_task(monkeypatch, tmp_path: Path) -> None:
    current_wrapper = tmp_path / "bin" / "adaos-autostart.ps1"
    current_wrapper.parent.mkdir(parents=True, exist_ok=True)
    current_wrapper.write_text("", encoding="utf-8")

    stale_wrapper = tmp_path / "old" / "adaos-autostart.ps1"
    stale_wrapper.parent.mkdir(parents=True, exist_ok=True)
    stale_wrapper.write_text("--host 127.0.0.1 --port 8778\n", encoding="utf-8")

    class _Proc:
        returncode = 0
        stdout = (
            "TaskName: \\AdaOS\n"
            "Status: Disabled\n"
            "Scheduled Task State: Disabled\n"
            f'Task To Run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{stale_wrapper}"\n'
        )
        stderr = ""

    monkeypatch.setattr(autostart, "_is_windows", lambda: True)
    monkeypatch.setattr(autostart, "_is_linux", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_run", lambda cmd: _Proc())
    monkeypatch.setattr(autostart, "_tcp_probe", lambda host, port, timeout=0.6: False)

    status = autostart.status(_FakeCtx(tmp_path))

    assert status["enabled"] is False
    assert status["active"] is False
    assert status["wrapper"] == str(stale_wrapper.resolve())
    assert status["registered_wrapper"] == str(stale_wrapper.resolve())
    assert status["expected_wrapper"] == str(current_wrapper.resolve())
    assert status["wrapper_matches_expected"] is False
    assert status["port"] == 8778


def test_windows_status_reports_wrapper_context(monkeypatch, tmp_path: Path) -> None:
    current_wrapper = tmp_path / "bin" / "adaos-autostart.ps1"
    current_wrapper.parent.mkdir(parents=True, exist_ok=True)
    current_wrapper.write_text("", encoding="utf-8")

    service_base = tmp_path / "service-base"
    shared_dotenv = tmp_path / ".env.shared"
    wrapper = tmp_path / "old" / "adaos-autostart.ps1"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "\n".join(
            [
                f"$env:ADAOS_BASE_DIR = '{service_base}'",
                f"$env:ADAOS_SHARED_DOTENV_PATH = '{shared_dotenv}'",
                "$args = @(",
                "  '--host'",
                "  '127.0.0.1'",
                "  '--port'",
                "  '8778'",
                ")",
            ]
        ),
        encoding="utf-8",
    )

    class _Proc:
        returncode = 0
        stdout = (
            "TaskName: \\AdaOS\n"
            "Status: Running\n"
            "Scheduled Task State: Running\n"
            f'Task To Run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{wrapper}"\n'
        )
        stderr = ""

    monkeypatch.setattr(autostart, "_is_windows", lambda: True)
    monkeypatch.setattr(autostart, "_is_linux", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_run", lambda cmd: _Proc())
    monkeypatch.setattr(autostart, "_discover_live_control_bind", lambda host, port: (host, port))

    status = autostart.status(_FakeCtx(tmp_path))

    assert status["base_dir"] == str(service_base.resolve())
    assert status["shared_dotenv_path"] == str(shared_dotenv.resolve())
    assert status["wrapper_env"]["ADAOS_BASE_DIR"] == str(service_base)


def test_linux_status_root_prefers_system_service_when_user_bus_exists(monkeypatch, tmp_path: Path) -> None:
    user_home = tmp_path / "home"
    user_service = user_home / ".config" / "systemd" / "user" / "adaos.service"
    system_service = tmp_path / "etc" / "systemd" / "system" / "adaos.service"
    user_wrapper = tmp_path / "user-wrapper.sh"
    system_wrapper = tmp_path / "system-wrapper.sh"

    user_service.parent.mkdir(parents=True, exist_ok=True)
    system_service.parent.mkdir(parents=True, exist_ok=True)
    user_wrapper.write_text("export ADAOS_BASE_DIR='/tmp/user'\nexec python --host 127.0.0.1 --port 8777\n", encoding="utf-8")
    system_wrapper.write_text("export ADAOS_BASE_DIR='/tmp/system'\nexec python --host 127.0.0.1 --port 8778\n", encoding="utf-8")
    user_service.write_text(f"[Service]\nExecStart={user_wrapper}\n", encoding="utf-8")
    system_service.write_text(f"[Service]\nExecStart={system_wrapper}\n", encoding="utf-8")

    class _Proc:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def _run(cmd: list[str]):
        return _Proc(0)

    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_home", lambda: user_home)
    monkeypatch.setattr(autostart, "_linux_service_path_system", lambda: system_service.resolve())
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: True)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: True)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: tmp_path / "bus")
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_run", _run)
    monkeypatch.setattr(autostart, "_discover_live_control_bind", lambda host, port: (host, port))

    status = autostart.status(_FakeCtx(tmp_path / "base"))

    assert status["scope"] == "system"
    assert status["service"] == str(system_service.resolve())
    assert status["wrapper"] == str(system_wrapper.resolve())
    assert status["port"] == 8778


def test_linux_status_reports_last_runner_status(monkeypatch, tmp_path: Path) -> None:
    user_home = tmp_path / "home"
    user_service = user_home / ".config" / "systemd" / "user" / "adaos.service"
    wrapper = tmp_path / "user-wrapper.sh"
    base_dir = tmp_path / "base"
    status_path = base_dir / "state" / "core_update" / "status.json"

    user_service.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        f"export ADAOS_BASE_DIR='{base_dir}'\nexec python --host 127.0.0.1 --port 8777\n",
        encoding="utf-8",
    )
    user_service.write_text(f"[Service]\nExecStart={wrapper}\n", encoding="utf-8")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        '{"state":"failed","phase":"uvicorn.run","message":"autostart runner failed during uvicorn.run"}',
        encoding="utf-8",
    )

    class _Proc:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_home", lambda: user_home)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: False)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: False)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: tmp_path / "bus")
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_run", lambda cmd: _Proc(0))
    monkeypatch.setattr(autostart, "_discover_live_control_bind", lambda host, port: (host, port))

    status = autostart.status(_FakeCtx(base_dir))

    assert status["core_update_status"]["state"] == "failed"
    assert status["core_update_status"]["phase"] == "uvicorn.run"
