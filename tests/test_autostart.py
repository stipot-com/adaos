from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from adaos.apps.autostart_runner import _slot_launch_spec
from adaos.services.autostart import default_spec, enable, status


class _FakePaths:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def base_dir(self) -> Path:
        return self._base_dir


class _FakeSettings:
    profile = "default"


class _FakeCtx:
    def __init__(self, base_dir: Path) -> None:
        self.paths = _FakePaths(base_dir)
        self.settings = _FakeSettings()


def test_default_autostart_spec_uses_runner(tmp_path: Path) -> None:
    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8779, token="t1")
    assert spec.argv[:3] == (spec.argv[0], "-m", "adaos.apps.supervisor")
    assert "--host" in spec.argv
    assert "--port" in spec.argv
    assert spec.env["ADAOS_BASE_DIR"] == str(tmp_path)
    assert spec.env["ADAOS_PROFILE"] == "default"
    assert spec.env["ADAOS_AUTOSTART_MANAGED"] == "1"
    assert spec.env["ADAOS_TOKEN"] == "t1"


def test_default_autostart_spec_falls_back_to_loaded_runtime_token(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.delenv("ADAOS_TOKEN", raising=False)
    monkeypatch.delenv("ADAOS_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.setattr(autostart, "load_config", lambda: SimpleNamespace(token="runtime-token"))

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8779)

    assert spec.env["ADAOS_TOKEN"] == "runtime-token"


def test_default_autostart_spec_omits_token_when_no_runtime_or_env_token(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.delenv("ADAOS_TOKEN", raising=False)
    monkeypatch.delenv("ADAOS_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.setattr(autostart, "load_config", lambda: SimpleNamespace(token=""))

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8779)

    assert "ADAOS_TOKEN" not in spec.env


def test_default_autostart_spec_keeps_context_base_dir_when_shared_dotenv_is_dev(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    shared_dotenv = repo_root / ".env"
    shared_dotenv.write_text("ENV_TYPE=dev\nADAOS_PROFILE=from-dotenv\n", encoding="utf-8")

    monkeypatch.setattr(autostart, "_shared_dotenv_path", lambda ctx: shared_dotenv)

    ctx = _FakeCtx(tmp_path / "active-base")
    spec = default_spec(ctx, host="127.0.0.1", port=8779, token="t1")

    assert spec.env["ADAOS_BASE_DIR"] == str((tmp_path / "active-base").resolve())
    assert spec.env["ADAOS_PROFILE"] == "from-dotenv"
    assert spec.env["ADAOS_SHARED_DOTENV_PATH"] == str(shared_dotenv.resolve())


def test_default_autostart_spec_respects_explicit_base_dir_from_shared_dotenv(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    shared_dotenv = repo_root / ".env"
    shared_dotenv.write_text(
        f"ENV_TYPE=dev\nADAOS_PROFILE=from-dotenv\nADAOS_BASE_DIR={tmp_path / 'service-base'}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(autostart, "_shared_dotenv_path", lambda ctx: shared_dotenv)

    ctx = _FakeCtx(tmp_path / "active-base")
    spec = default_spec(ctx, host="127.0.0.1", port=8779, token="t1")

    assert spec.env["ADAOS_BASE_DIR"] == str((tmp_path / "service-base").resolve())
    assert spec.env["ADAOS_PROFILE"] == "from-dotenv"
    assert spec.env["ADAOS_SHARED_DOTENV_PATH"] == str(shared_dotenv.resolve())


def test_slot_launch_spec_formats_placeholders() -> None:
    argv, command = _slot_launch_spec(
        {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
        },
        host="127.0.0.1",
        port=8777,
        token="tok",
    )
    assert command is None
    assert argv is not None
    assert argv[-1] == "8777"


def test_linux_status_without_user_bus_uses_service_file(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)

    calls: list[list[str]] = []

    def _boom(cmd: list[str]):
        calls.append(cmd)
        raise AssertionError("status() must not call systemctl --user when the user bus is missing")

    monkeypatch.setattr(autostart, "_run", _boom)

    service_path = tmp_path / ".config" / "systemd" / "user" / "adaos.service"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("[Unit]\nDescription=test\n", encoding="utf-8")

    payload = status(_FakeCtx(tmp_path))
    assert payload["enabled"] is True
    assert payload["active"] is None
    assert calls == []


def test_restart_service_uses_unit_name_on_linux(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setattr(
        autostart,
        "status",
        lambda ctx: {
            "scope": "system",
            "service": "/etc/systemd/system/adaos.service",
        },
    )

    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        return _Proc()

    monkeypatch.setattr(autostart.subprocess, "run", _run)

    payload = autostart.restart_service(_FakeCtx(tmp_path))

    assert captured["cmd"] == ["systemctl", "restart", "adaos.service"]
    assert payload["service"] == "adaos.service"
    assert payload["service_ref"] == "/etc/systemd/system/adaos.service"


def test_linux_enable_without_user_bus_raises_helpful_error(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: False)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: False)

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    try:
        enable(_FakeCtx(tmp_path), spec, scope="user")
    except RuntimeError as exc:
        msg = str(exc)
        assert "systemctl --user is not available" in msg
        assert "Generated files" in msg
    else:
        raise AssertionError("expected enable() to raise when systemctl --user is unavailable")

    assert (tmp_path / "bin" / "adaos-autostart.sh").exists()
    assert (tmp_path / ".config" / "systemd" / "user" / "adaos.service").exists()


def test_linux_enable_root_falls_back_to_system_service(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: True)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: True)
    monkeypatch.setattr(autostart, "_linux_service_path_system", lambda: (tmp_path / "etc" / "systemd" / "system" / "adaos.service").resolve())

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def _run(cmd: list[str]):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(autostart, "_run", _run)

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    res = enable(_FakeCtx(tmp_path), spec)
    assert res["scope"] == "system"
    assert (tmp_path / "etc" / "systemd" / "system" / "adaos.service").exists()
    assert ["systemctl", "enable", "--now", "adaos.service"] in calls


def test_linux_enable_root_prefers_system_service_even_with_user_bus(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: tmp_path / "bus")
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: True)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: True)
    monkeypatch.setattr(autostart, "_linux_service_path_system", lambda: (tmp_path / "etc" / "systemd" / "system" / "adaos.service").resolve())

    stale_user_service = tmp_path / ".config" / "systemd" / "user" / "adaos.service"
    stale_user_service.parent.mkdir(parents=True, exist_ok=True)
    stale_user_service.write_text("[Unit]\nDescription=stale\n", encoding="utf-8")

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(autostart, "_run", lambda cmd: calls.append(cmd) or _Proc())

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    res = enable(_FakeCtx(tmp_path), spec)

    assert res["scope"] == "system"
    assert ["systemctl", "enable", "--now", "adaos.service"] in calls
    assert ["systemctl", "--user", "disable", "--now", "adaos.service"] in calls
    assert not stale_user_service.exists()


def test_linux_enable_system_run_as_user_rejects_root_paths(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: True)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: True)

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    spec = type(spec)(  # keep dataclass type but override fields
        name=spec.name,
        argv=("/root/adaos/.venv/bin/python3",) + tuple(spec.argv[1:]),
        env={**spec.env, "ADAOS_BASE_DIR": "/root/adaos/.adaos"},
    )

    try:
        enable(_FakeCtx(tmp_path), spec, scope="system", run_as="adaos", create_user=True)
    except RuntimeError as exc:
        assert "paths point to /root" in str(exc)
    else:
        raise AssertionError("expected enable() to reject running as user with /root paths")


def test_linux_enable_system_can_create_user(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)
    monkeypatch.setattr(autostart, "_linux_has_systemd_pid1", lambda: True)
    monkeypatch.setattr(autostart, "_linux_is_root", lambda: True)
    monkeypatch.setattr(autostart, "_linux_service_path_system", lambda: (tmp_path / "etc" / "systemd" / "system" / "adaos.service").resolve())

    created: list[str] = []
    monkeypatch.setattr(autostart, "_linux_user_exists", lambda u: False)
    monkeypatch.setattr(autostart, "_linux_create_system_user", lambda u: created.append(u))

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(autostart, "_run", lambda cmd: calls.append(cmd) or _Proc())

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    res = enable(_FakeCtx(tmp_path), spec, scope="system", run_as="adaos", create_user=True)
    assert res["scope"] == "system"
    assert res["run_as"] == "adaos"
    assert created == ["adaos"]
