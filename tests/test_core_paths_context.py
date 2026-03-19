from __future__ import annotations

from pathlib import Path

from adaos.apps import autostart_runner
from adaos.services import core_slots, core_update


class _FakePaths:
    def __init__(self, base_dir: Path, package_dir: Path) -> None:
        self._base_dir = base_dir
        self._package_dir = package_dir

    def base_dir(self) -> Path:
        return self._base_dir

    def package_path(self) -> Path:
        return self._package_dir


class _FakeCtx:
    def __init__(self, base_dir: Path, package_dir: Path) -> None:
        self.paths = _FakePaths(base_dir, package_dir)


def test_core_slots_prefers_context_base_dir(monkeypatch, tmp_path: Path) -> None:
    ctx = _FakeCtx(tmp_path / "custom-base", tmp_path / "repo" / "src" / "adaos")
    monkeypatch.setattr(core_slots, "get_ctx", lambda: ctx)

    assert core_slots.slot_dir("A") == (tmp_path / "custom-base" / "state" / "core_slots" / "slots" / "A").resolve()


def test_core_update_prefers_context_paths(monkeypatch, tmp_path: Path) -> None:
    ctx = _FakeCtx(tmp_path / "custom-base", tmp_path / "repo" / "src" / "adaos")
    monkeypatch.setattr(core_update, "get_ctx", lambda: ctx)

    assert core_update._base_dir() == (tmp_path / "custom-base").resolve()
    assert core_update._repo_root() == (tmp_path / "repo").resolve()


def test_autostart_runner_slot_launch_spec_uses_context_base_dir(monkeypatch, tmp_path: Path) -> None:
    ctx = _FakeCtx(tmp_path / "custom-base", tmp_path / "repo" / "src" / "adaos")
    monkeypatch.setattr(autostart_runner, "get_ctx", lambda: ctx)
    monkeypatch.setattr(autostart_runner, "slot_dir", lambda slot: (tmp_path / "slots" / slot).resolve())

    argv, command = autostart_runner._slot_launch_spec(
        {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner", "--base-dir", "{base_dir}", "--port", "{port}"],
        },
        host="127.0.0.1",
        port=8777,
        token=None,
    )

    assert command is None
    assert argv is not None
    assert str((tmp_path / "custom-base").resolve()) in argv
