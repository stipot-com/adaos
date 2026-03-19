from __future__ import annotations

from pathlib import Path

from adaos.apps.core_update_apply import prepare_slot


def test_prepare_slot_from_local_repo_includes_shared_dotenv(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    slot_root = tmp_path / "base" / "state" / "core_slots" / "slots" / "A"
    source_repo = tmp_path / "source"
    source_repo.mkdir(parents=True)
    (source_repo / ".git").mkdir()
    shared_dotenv = tmp_path / "repo.env"
    shared_dotenv.write_text("ENV_TYPE=prod\n", encoding="utf-8")

    def _fake_run(cmd: list[str], *, cwd: Path | None = None) -> None:
        commands.append(list(cmd))
        if cmd[:2] == ["git", "clone"]:
            checkout = Path(cmd[-1])
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / ".git").mkdir(exist_ok=True)
            return
        if len(cmd) >= 4 and cmd[1:3] == ["-m", "venv"]:
            venv_dir = Path(cmd[3])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
            return
        if "pip" in cmd:
            return

    monkeypatch.setattr("adaos.apps.core_update_apply._run", _fake_run)

    manifest = prepare_slot(
        slot="A",
        slot_dir_path=slot_root,
        base_dir=tmp_path / "base",
        source_repo_root=source_repo,
        shared_dotenv_path=shared_dotenv,
        target_version="1.0.0",
    )

    assert manifest["cwd"] == str((slot_root / "repo").resolve())
    assert manifest["env"]["ADAOS_SHARED_DOTENV_PATH"] == str(shared_dotenv)
    assert any(Path(cmd[0]).name.lower().startswith("git") and len(cmd) >= 2 and cmd[1] == "clone" for cmd in commands)


def test_prepare_slot_from_plain_source_tree_without_git(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    slot_root = tmp_path / "base" / "state" / "core_slots" / "slots" / "A"
    source_repo = tmp_path / "source"
    source_repo.mkdir(parents=True)
    (source_repo / "pyproject.toml").write_text("[project]\nname='adaos'\nversion='0.0.0'\n", encoding="utf-8")
    (source_repo / "README.md").write_text("src tree\n", encoding="utf-8")

    def _fake_run(cmd: list[str], *, cwd: Path | None = None) -> None:
        commands.append(list(cmd))
        if Path(cmd[0]).name.lower().startswith("git") and len(cmd) >= 2 and cmd[1] == "clone":
            checkout = Path(cmd[-1])
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / ".git").mkdir(exist_ok=True)
            return
        if len(cmd) >= 4 and cmd[1:3] == ["-m", "venv"]:
            venv_dir = Path(cmd[3])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
            return
        if "pip" in cmd:
            return

    monkeypatch.setattr("adaos.apps.core_update_apply._run", _fake_run)
    monkeypatch.setattr("adaos.apps.core_update_apply.shutil.which", lambda name: "git" if name == "git" else None)

    manifest = prepare_slot(
        slot="A",
        slot_dir_path=slot_root,
        base_dir=tmp_path / "base",
        source_repo_root=source_repo,
        repo_url="https://github.com/stipot-com/adaos.git",
        target_rev="rev2026",
        target_version="1.0.0",
    )

    assert any(Path(cmd[0]).name.lower().startswith("git") and len(cmd) >= 2 and cmd[1] == "clone" for cmd in commands)
    assert Path(manifest["repo_dir"]).joinpath(".git").exists()


def test_prepare_slot_falls_back_to_copy_when_git_unavailable(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    slot_root = tmp_path / "base" / "state" / "core_slots" / "slots" / "A"
    source_repo = tmp_path / "source"
    source_repo.mkdir(parents=True)
    (source_repo / "pyproject.toml").write_text("[project]\nname='adaos'\nversion='0.0.0'\n", encoding="utf-8")
    (source_repo / "README.md").write_text("src tree\n", encoding="utf-8")
    (source_repo / ".adaos").mkdir()
    (source_repo / ".adaos" / "secret.txt").write_text("must not copy\n", encoding="utf-8")

    def _fake_run(cmd: list[str], *, cwd: Path | None = None) -> None:
        commands.append(list(cmd))
        if len(cmd) >= 4 and cmd[1:3] == ["-m", "venv"]:
            venv_dir = Path(cmd[3])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
            return
        if "pip" in cmd:
            return

    monkeypatch.setattr("adaos.apps.core_update_apply._run", _fake_run)
    monkeypatch.setattr("adaos.apps.core_update_apply.shutil.which", lambda name: None)

    manifest = prepare_slot(
        slot="A",
        slot_dir_path=slot_root,
        base_dir=tmp_path / "base",
        source_repo_root=source_repo,
        repo_url="https://github.com/stipot-com/adaos.git",
        target_version="1.0.0",
    )

    assert Path(manifest["repo_dir"]).joinpath("README.md").exists()
    assert not Path(manifest["repo_dir"]).joinpath(".adaos").exists()
    assert not any(Path(cmd[0]).name.lower().startswith("git") and len(cmd) >= 2 and cmd[1] == "clone" for cmd in commands)
