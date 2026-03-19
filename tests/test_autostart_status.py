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
