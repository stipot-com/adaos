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


def test_linux_autostart_service_restarts_after_clean_exit(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _FakeCtx(tmp_path / "base")
    spec = autostart.AutostartSpec(
        name="adaos",
        argv=("python", "-m", "adaos.apps.autostart_runner", "--host", "127.0.0.1", "--port", "8777"),
        env={"ADAOS_BASE_DIR": str((tmp_path / "base").resolve())},
    )

    monkeypatch.setattr(autostart, "_is_windows", lambda: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: True)
    monkeypatch.setattr(autostart, "_is_macos", lambda: False)
    monkeypatch.setattr(autostart, "_home", lambda: home)
    monkeypatch.setattr(autostart, "shutil_which", lambda cmd: None)
    monkeypatch.setattr(autostart, "_bootstrap_core_slot", lambda ctx, token=None: None)

    autostart.enable(ctx, spec)

    service_path = home / ".config" / "systemd" / "user" / "adaos.service"
    text = service_path.read_text(encoding="utf-8")
    assert "Restart=always" in text


def test_default_spec_exports_shared_dotenv(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_dir = repo_root / "src" / "adaos"
    package_dir.mkdir(parents=True)
    dotenv_path = repo_root / ".env"
    dotenv_path.write_text("NATS_URL=test\n", encoding="utf-8")

    class _Paths(_FakePaths):
        def package_path(self) -> Path:
            return package_dir

        def repo_root(self) -> Path:
            return repo_root

    class _CtxWithPackage(_FakeCtx):
        def __init__(self, base_dir: Path) -> None:
            self.paths = _Paths(base_dir)
            self.settings = type("Settings", (), {"profile": "default"})()

    spec = autostart.default_spec(_CtxWithPackage(tmp_path / "base"))
    assert spec.env["ADAOS_SHARED_DOTENV_PATH"] == str(dotenv_path.resolve())
