from pathlib import Path
import sys
import types

from typer.testing import CliRunner

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.cli.commands import scenario as scenario_cmd
from adaos.apps.cli.commands import skill as skill_cmd


def _fake_path_status(path: str):
    class _Status:
        def __init__(self, target: str):
            self.path = target
            self.exists = True
            self.dirty = False
            self.base_ref = "HEAD"
            self.changed_vs_base = False
            self.local_last_commit = None
            self.base_last_commit = None
            self.error = None

    return _Status(path)


def test_skill_status_falls_back_to_workspace_when_registry_empty(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "weather_skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    runtime_root = tmp_base_dir / "workspace" / "skills" / ".runtime" / "weather_skill" / "1.0.0"
    runtime_root.mkdir(parents=True, exist_ok=True)
    ((tmp_base_dir / "workspace" / "skills" / ".runtime" / "weather_skill") / "current_version").write_text("1.0.0", encoding="utf-8")

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/weather_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            return {"version": "1.0.0", "active_slot": "A", "ready": False}

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "weather_skill: v1.0.0 slot=A" in result.stdout


def test_skill_status_marks_workspace_draft_without_runtime_error(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "infra_access_skill"
    skill_root.mkdir(parents=True, exist_ok=True)

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/infra_access_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise AssertionError("runtime_status should not be called for workspace draft skills")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infra_access_skill: vn/a slot=n/a [draft]" in result.stdout
    assert "runtime-error" not in result.stdout


def test_skill_status_includes_repo_workspace_fallback_skills(tmp_base_dir, monkeypatch):
    repo_skill = tmp_base_dir / "repo" / ".adaos" / "workspace" / "skills" / "infrastate_skill"
    repo_skill.mkdir(parents=True, exist_ok=True)

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

        def repo_root(self):
            return tmp_base_dir / "repo"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status(".adaos/workspace/skills/infrastate_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise AssertionError("runtime_status should not be called for repo workspace draft skills")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infrastate_skill: vn/a slot=n/a [draft]" in result.stdout


def test_skill_status_marks_runtime_missing_without_runtime_error(tmp_base_dir, monkeypatch):
    skill_root = tmp_base_dir / "workspace" / "skills" / "infra_access_skill"
    skill_root.mkdir(parents=True, exist_ok=True)

    class _Row:
        name = "infra_access_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql):
            pass

        def list(self):
            return [_Row()]

    class _Paths:
        def workspace_dir(self):
            return tmp_base_dir / "workspace"

        def skills_workspace_dir(self):
            return tmp_base_dir / "workspace" / "skills"

        def dev_skills_dir(self):
            return tmp_base_dir / "skills-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(skill_cmd, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(skill_cmd, "ensure_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    monkeypatch.setattr(skill_cmd, "compute_path_status", lambda **kwargs: _fake_path_status("skills/infra_access_skill"))
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: _Ctx())

    class _Mgr:
        @staticmethod
        def runtime_status(_name: str):
            raise RuntimeError("no versions installed")

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())

    result = CliRunner().invoke(skill_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "infra_access_skill: vn/a slot=n/a [" in result.stdout
    assert "runtime-error" not in result.stdout


def test_scenario_status_reports_empty_when_registry_and_workspace_are_empty(tmp_path, monkeypatch):
    class _Paths:
        def workspace_dir(self):
            return tmp_path / "workspace"

        def scenarios_workspace_dir(self):
            return tmp_path / "workspace" / "scenarios"

        def dev_scenarios_dir(self):
            return tmp_path / "scenarios-dev"

    class _Ctx:
        paths = _Paths()
        sql = object()

    monkeypatch.setattr(scenario_cmd, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(scenario_cmd, "resolve_base_ref", lambda *args, **kwargs: "HEAD")
    result = CliRunner().invoke(scenario_cmd.app, ["status"])

    assert result.exit_code == 0
    assert "No installed scenarios." in result.stdout
