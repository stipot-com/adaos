from __future__ import annotations

from pathlib import Path

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
    assert spec.argv[:3] == (spec.argv[0], "-m", "adaos.apps.autostart_runner")
    assert "--host" in spec.argv
    assert "--port" in spec.argv
    assert spec.env["ADAOS_BASE_DIR"] == str(tmp_path)
    assert spec.env["ADAOS_PROFILE"] == "default"
    assert spec.env["ADAOS_TOKEN"] == "t1"


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


def test_linux_enable_without_user_bus_raises_helpful_error(monkeypatch, tmp_path: Path) -> None:
    import adaos.services.autostart as autostart

    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(autostart, "_linux_user_bus_path", lambda: None)

    spec = default_spec(_FakeCtx(tmp_path), host="127.0.0.1", port=8777, token="t1")
    try:
        enable(_FakeCtx(tmp_path), spec)
    except RuntimeError as exc:
        msg = str(exc)
        assert "systemctl --user is not available" in msg
        assert "Generated files" in msg
    else:
        raise AssertionError("expected enable() to raise when systemctl --user is unavailable")

    assert (tmp_path / "bin" / "adaos-autostart.sh").exists()
    assert (tmp_path / ".config" / "systemd" / "user" / "adaos.service").exists()
