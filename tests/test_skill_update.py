from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from adaos.adapters.git.cli_git import CliGitClient
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.services.skill.update import SkillUpdateService


class _MiniPaths:
    def __init__(self, base: Path) -> None:
        self._base = Path(base)
        self._workspace = self._base / "workspace"
        self._skills = self._workspace / "skills"

    def base_dir(self) -> Path:
        return self._base

    def workspace_dir(self) -> Path:
        return self._workspace

    def skills_dir(self) -> Path:
        return self._skills

    def skills_workspace_dir(self) -> Path:
        return self._skills


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_monorepo(root: Path, *, tracked_skill_env: bool) -> Path:
    remote = root / "remote"
    remote.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-b", "main"], cwd=remote)
    _run_git(["config", "user.email", "adaos-tests@example.com"], cwd=remote)
    _run_git(["config", "user.name", "AdaOS Tests"], cwd=remote)

    skill_dir = remote / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "id: infrastate_skill\nname: Infra State\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    if tracked_skill_env:
        (skill_dir / ".skill_env.json").write_text('{"mode":"remote-v1"}\n', encoding="utf-8")

    desktop_skill_dir = remote / "skills" / "web_desktop_skill"
    desktop_skill_dir.mkdir(parents=True, exist_ok=True)
    (desktop_skill_dir / "skill.yaml").write_text(
        "id: web_desktop_skill\nname: Web Desktop\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (desktop_skill_dir / ".skill_env.json").write_text('{"mode":"desktop-remote-v1"}\n', encoding="utf-8")
    (remote / "registry.json").write_text(
        '{\n  "version": 1,\n  "updated_at": "2026-01-01T00:00:00+00:00",\n  "skills": [],\n  "scenarios": []\n}\n',
        encoding="utf-8",
    )

    _run_git(["add", "-A"], cwd=remote)
    _run_git(["commit", "-m", "seed infrastate"], cwd=remote)
    return remote


def _update_remote_skill(remote: Path, *, version: str, skill_env: str | None) -> None:
    skill_dir = remote / "skills" / "infrastate_skill"
    (skill_dir / "skill.yaml").write_text(
        f"id: infrastate_skill\nname: Infra State\nversion: '{version}'\n",
        encoding="utf-8",
    )
    if skill_env is not None:
        (skill_dir / ".skill_env.json").write_text(skill_env, encoding="utf-8")
    _run_git(["add", "-A"], cwd=remote)
    _run_git(["commit", "-m", f"update infrastate {version}"], cwd=remote)


def _make_service(base: Path, remote: Path) -> tuple[SkillUpdateService, _MiniPaths, GitSkillRepository]:
    paths = _MiniPaths(base)
    paths.workspace_dir().mkdir(parents=True, exist_ok=True)
    git = CliGitClient(depth=0)
    repo = GitSkillRepository(paths=paths, git=git, monorepo_url=str(remote), monorepo_branch="main")
    ctx = SimpleNamespace(
        skills_repo=repo,
        paths=paths,
        settings=SimpleNamespace(skills_monorepo_url=str(remote), skills_monorepo_branch="main"),
        git=git,
        fs=SimpleNamespace(require_write=lambda _path: None),
    )
    return SkillUpdateService(ctx), paths, repo


def test_request_update_preserves_untracked_skill_env_overlay(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_TESTING", "0")
    remote = _init_monorepo(tmp_path / "case-untracked", tracked_skill_env=False)
    service, paths, repo = _make_service(tmp_path / "case-untracked-node", remote)

    repo.install("infrastate_skill")
    skill_dir = paths.skills_dir() / "infrastate_skill"
    local_env = skill_dir / ".skill_env.json"
    local_env.write_text('{"mode":"local"}\n', encoding="utf-8")

    _update_remote_skill(remote, version="1.0.1", skill_env='{"mode":"remote-default"}\n')

    result = service.request_update("infrastate_skill")

    assert result.updated is True
    assert result.version == "1.0.1"
    assert local_env.read_text(encoding="utf-8") == '{"mode":"local"}\n'
    assert "version: '1.0.1'" in (skill_dir / "skill.yaml").read_text(encoding="utf-8")


def test_request_update_preserves_modified_tracked_skill_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_TESTING", "0")
    remote = _init_monorepo(tmp_path / "case-tracked", tracked_skill_env=True)
    service, paths, repo = _make_service(tmp_path / "case-tracked-node", remote)

    repo.install("infrastate_skill")
    skill_dir = paths.skills_dir() / "infrastate_skill"
    local_env = skill_dir / ".skill_env.json"
    local_env.write_text('{"mode":"local-custom"}\n', encoding="utf-8")

    _update_remote_skill(remote, version="1.0.2", skill_env='{"mode":"remote-v2"}\n')

    result = service.request_update("infrastate_skill")

    assert result.updated is True
    assert result.version == "1.0.2"
    assert local_env.read_text(encoding="utf-8") == '{"mode":"local-custom"}\n'
    assert "version: '1.0.2'" in (skill_dir / "skill.yaml").read_text(encoding="utf-8")


def test_request_update_preserves_unrelated_skill_env_overlay(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_TESTING", "0")
    remote = _init_monorepo(tmp_path / "case-unrelated", tracked_skill_env=True)
    service, paths, repo = _make_service(tmp_path / "case-unrelated-node", remote)

    repo.install("infrastate_skill")
    repo.install("web_desktop_skill")

    unrelated_env = paths.skills_dir() / "web_desktop_skill" / ".skill_env.json"
    unrelated_env.write_text('{"mode":"desktop-local-custom"}\n', encoding="utf-8")

    _update_remote_skill(remote, version="1.0.3", skill_env='{"mode":"remote-v3"}\n')

    result = service.request_update("infrastate_skill")

    assert result.updated is True
    assert result.version == "1.0.3"
    assert unrelated_env.read_text(encoding="utf-8") == '{"mode":"desktop-local-custom"}\n'


def test_request_update_rebuilds_workspace_registry_when_local_registry_is_dirty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_TESTING", "0")
    remote = _init_monorepo(tmp_path / "case-registry", tracked_skill_env=False)
    service, paths, repo = _make_service(tmp_path / "case-registry-node", remote)

    repo.install("infrastate_skill")
    workspace = paths.workspace_dir()
    registry = workspace / "registry.json"
    registry.write_text('{"version":1,"updated_at":"local","skills":[{"name":"local_only"}],"scenarios":[]}\n', encoding="utf-8")

    _update_remote_skill(remote, version="1.0.4", skill_env='{"mode":"remote-default"}\n')

    result = service.request_update("infrastate_skill")

    assert result.updated is True
    assert result.version == "1.0.4"
    registry_payload = registry.read_text(encoding="utf-8")
    assert '"name": "infrastate_skill"' in registry_payload
    assert '"version": "1.0.4"' in registry_payload
    assert '"name": "local_only"' not in registry_payload
