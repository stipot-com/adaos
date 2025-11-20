from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adaos.adapters.git.cli_git import CliGitClient
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.skills.git_repo import GitSkillRepository


class _MiniPaths:
    def __init__(self, base: Path) -> None:
        self._base = Path(base)
        self._workspace = self._base / "workspace"
        self._skills = self._workspace / "skills"
        self._scenarios = self._workspace / "scenarios"

    def workspace_dir(self) -> Path:
        return self._workspace

    def skills_dir(self) -> Path:
        return self._skills

    def scenarios_dir(self) -> Path:
        return self._scenarios


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run([
        "git",
        *args,
    ], cwd=str(cwd), check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_monorepo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir()
    _run_git(["init"], cwd=remote)
    _run_git(["config", "user.email", "adaos-tests@example.com"], cwd=remote)
    _run_git(["config", "user.name", "AdaOS Tests"], cwd=remote)

    skills = remote / "skills"
    weather = skills / "weather_skill"
    news = skills / "news_skill"
    scenarios = remote / "scenarios"
    greet = scenarios / "greet_on_boot"
    for path in (weather, news, greet):
        path.mkdir(parents=True, exist_ok=True)

    (weather / "skill.yaml").write_text(
        "id: weather_skill\nname: Weather\nversion: '1.0.0'\n",
        encoding="utf-8",
    )
    (news / "skill.yaml").write_text(
        "id: news_skill\nname: News\nversion: '2.0.0'\n",
        encoding="utf-8",
    )
    (greet / "scenario.yaml").write_text(
        "id: greet_on_boot\nname: Greet on boot\nversion: '1.0.0'\n",
        encoding="utf-8",
    )

    _run_git(["add", "-A"], cwd=remote)
    _run_git(["commit", "-m", "seed workspace"], cwd=remote)
    return remote


def _make_paths(tmp_path: Path) -> _MiniPaths:
    base = tmp_path / "workspace"
    paths = _MiniPaths(base)
    paths.workspace_dir().mkdir(parents=True, exist_ok=True)
    return paths


def _make_skill_repo(paths: _MiniPaths, remote: Path) -> GitSkillRepository:
    git = CliGitClient(depth=0)
    return GitSkillRepository(paths=paths, git=git, monorepo_url=str(remote))


def _make_scenario_repo(paths: _MiniPaths, remote: Path) -> GitScenarioRepository:
    git = CliGitClient(depth=0)
    return GitScenarioRepository(paths=paths, git=git, url=str(remote))


@pytest.fixture
def monorepo(tmp_path) -> Path:
    return _init_monorepo(tmp_path)


@pytest.fixture
def paths(tmp_path) -> TestPaths:
    return _make_paths(tmp_path)


def _git_status_clean(workspace: Path) -> bool:
    try:
        out = _run_git(["status", "--porcelain"], cwd=workspace)
    except subprocess.CalledProcessError:
        return False
    return out.strip() == ""


def test_skill_reinstall_happy_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    meta1 = repo.install("weather_skill")
    assert meta1.id.value == "weather_skill"
    repo.uninstall("weather_skill")
    assert not (paths.skills_dir() / "weather_skill").exists()

    meta2 = repo.install("weather_skill")
    assert meta2.id.value == "weather_skill"
    assert (paths.skills_dir() / "weather_skill").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_scenario_reinstall_happy_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_scenario_repo(paths, monorepo)

    meta1 = repo.install("greet_on_boot")
    assert meta1.id.value == "greet_on_boot"
    repo.uninstall("greet_on_boot")
    assert not (paths.scenarios_dir() / "greet_on_boot").exists()

    meta2 = repo.install("greet_on_boot")
    assert meta2.id.value == "greet_on_boot"
    assert (paths.scenarios_dir() / "greet_on_boot").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_uninstall_idempotent(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    repo.install("weather_skill")
    repo.uninstall("weather_skill")
    # second uninstall should be a no-op
    repo.uninstall("weather_skill")
    assert _git_status_clean(paths.workspace_dir())


def test_install_missing_remote_path(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    repo = _make_skill_repo(paths, monorepo)

    with pytest.raises(FileNotFoundError):
        repo.install("missing_skill")

    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    if sparse_file.exists():
        assert "skills/missing_skill" not in sparse_file.read_text(encoding="utf-8")
    assert not (paths.skills_dir() / "missing_skill").exists()
    assert _git_status_clean(paths.workspace_dir())


def test_sparse_checkout_scope(monkeypatch, monorepo, paths):
    monkeypatch.setenv("ADAOS_TESTING", "0")
    skill_repo = _make_skill_repo(paths, monorepo)
    scenario_repo = _make_scenario_repo(paths, monorepo)

    skill_repo.install("weather_skill")
    scenario_repo.install("greet_on_boot")

    skills_present = {
        child.name for child in paths.skills_dir().iterdir() if child.is_dir()
    }
    assert skills_present == {"weather_skill"}
    sparse_file = paths.workspace_dir() / ".git" / "info" / "sparse-checkout"
    assert "skills/weather_skill" in sparse_file.read_text(encoding="utf-8")
    assert "skills/news_skill" not in sparse_file.read_text(encoding="utf-8")

    skill_repo.uninstall("weather_skill")
    assert not (paths.skills_dir() / "weather_skill").exists()
    assert "skills/weather_skill" not in sparse_file.read_text(encoding="utf-8")
    # scenario entry should remain in sparse checkout
    assert "scenarios/greet_on_boot" in sparse_file.read_text(encoding="utf-8")
    assert _git_status_clean(paths.workspace_dir())
