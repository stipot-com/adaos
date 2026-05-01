from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
    )
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.services.scenario.manager import ScenarioManager
from adaos.services.skill.manager import SkillManager


class _FakeCaps:
    def require(self, *args, **kwargs) -> None:
        return None


class _FakeGit:
    def __init__(self) -> None:
        self.commit_calls: list[dict[str, object]] = []
        self.push_calls: list[str] = []

    def changed_files(self, root: str, *, subpath: str):
        if subpath == "registry.json":
            return ["registry.json"]
        return [subpath]

    def commit_subpath(self, root: str, *, subpath, message: str, author_name: str, author_email: str, signoff: bool = False):
        self.commit_calls.append(
            {
                "root": root,
                "subpath": subpath,
                "message": message,
                "author_name": author_name,
                "author_email": author_email,
                "signoff": signoff,
            }
        )
        return "rev-1"

    def push(self, root: str) -> None:
        self.push_calls.append(root)


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeMap(dict):
    def set(self, txn, key: str, value: object) -> None:  # noqa: ARG002
        self[key] = value


class _FakeDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    def begin_transaction(self) -> _FakeTxn:
        return _FakeTxn()

    def get_map(self, name: str) -> _FakeMap:
        return self._state.setdefault(name, _FakeMap())


def _workspace_ctx(workspace: Path, git: _FakeGit) -> SimpleNamespace:
    return SimpleNamespace(
        git=git,
        paths=SimpleNamespace(workspace_dir=lambda: workspace),
        settings=SimpleNamespace(
            base_dir=str(workspace),
            git_author_name="Ada Tester",
            git_author_email="tester@adaos.local",
        ),
    )


def test_skill_push_updates_registry_and_commits_it(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    skill_dir = workspace / "skills" / "demo_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "id: demo_skill",
                "name: Demo Skill",
                "version: '1.0.0'",
                "description: Initial skill",
                "",
            ]
        ),
        encoding="utf-8",
    )

    git = _FakeGit()
    monkeypatch.setattr("adaos.services.skill.manager.get_git_availability", lambda base_dir=None: SimpleNamespace(enabled=True), raising=False)

    manager = object.__new__(SkillManager)
    manager.caps = _FakeCaps()
    manager.settings = SimpleNamespace(git_author_name="Ada Tester", git_author_email="tester@adaos.local")
    manager.ctx = _workspace_ctx(workspace, git)

    revision = manager.push("demo_skill", "publish demo skill")

    registry = json.loads((workspace / "registry.json").read_text(encoding="utf-8"))
    assert revision == "rev-1"
    assert [item["id"] for item in registry["skills"]] == ["demo_skill"]
    assert git.commit_calls[0]["subpath"] == ["skills/demo_skill", "registry.json"]
    assert git.push_calls == [str(workspace)]


def test_scenario_push_updates_registry_and_commits_it(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    scenario_dir = workspace / "scenarios" / "welcome_scene"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "scenario.json").write_text(
        json.dumps(
            {
                "id": "welcome_scene",
                "name": "Welcome Scene",
                "version": "0.1.0",
                "description": "Initial scenario",
            }
        ),
        encoding="utf-8",
    )

    git = _FakeGit()
    ctx = _workspace_ctx(workspace, git)
    monkeypatch.setattr("adaos.services.scenario.manager.get_git_availability", lambda base_dir=None: SimpleNamespace(enabled=True), raising=False)
    monkeypatch.setattr("adaos.services.scenario.manager.get_ctx", lambda: ctx)

    manager = object.__new__(ScenarioManager)
    manager.caps = _FakeCaps()
    manager.git = git
    manager.ctx = ctx

    revision = manager.push("welcome_scene", "publish welcome scenario")

    registry = json.loads((workspace / "registry.json").read_text(encoding="utf-8"))
    assert revision == "rev-1"
    assert [item["id"] for item in registry["scenarios"]] == ["welcome_scene"]
    assert git.commit_calls[0]["subpath"] == ["scenarios/welcome_scene", "registry.json"]
    assert git.push_calls == [str(workspace)]


def test_scenario_project_to_doc_keeps_runtime_owned_effective_data_under_rebuild_ownership(monkeypatch) -> None:
    manager = object.__new__(ScenarioManager)
    manager.caps = _FakeCaps()
    monkeypatch.setattr("adaos.services.scenario.manager._local_node_id", lambda: "node-1")

    state = {
        "ui": _FakeMap(
            {
                "application": {
                    "desktop": {
                        "pageSchema": {"id": "live-page"},
                    }
                }
            }
        ),
        "registry": _FakeMap({"merged": {"modals": ["live-modal"]}}),
        "data": _FakeMap(
            {
                "catalog": {"apps": [{"id": "live-app"}]},
                "installed": {"apps": ["scenario:web_desktop"], "widgets": []},
                "desktop": {"pageSchema": {"id": "live-desktop"}},
                "routing": {"routes": {"home": "/"}},
            }
        ),
    }

    manager._project_to_doc(
        _FakeDoc(state),
        "prompt_engineer_scenario",
        ui_section={"desktop": {"pageSchema": {"id": "legacy-page"}}},
        registry_section={"modals": ["legacy-modal"]},
        catalog_section={"apps": [{"id": "legacy-app"}]},
        data_section={
            "catalog": {"apps": [{"id": "should-not-overwrite"}]},
            "installed": {"apps": ["should-not-overwrite"]},
            "desktop": {"pageSchema": {"id": "should-not-overwrite"}},
            "routing": {"routes": {"home": "/should-not-overwrite"}},
            "weather": {"city": "Moscow"},
        },
    )

    assert state["ui"]["application"]["desktop"]["pageSchema"]["id"] == "live-page"
    assert state["registry"]["merged"]["modals"] == ["live-modal"]
    assert state["data"]["catalog"]["apps"] == [{"id": "live-app"}]
    assert state["data"]["installed"]["apps"] == ["scenario:web_desktop"]
    assert state["data"]["desktop"]["pageSchema"]["id"] == "live-desktop"
    assert state["data"]["routing"]["routes"]["home"] == "/"
    assert state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert state["ui"]["scenarios"]["node-1"]["prompt_engineer_scenario"]["application"]["desktop"]["pageSchema"]["id"] == "legacy-page"
    assert state["registry"]["scenarios"]["node-1"]["prompt_engineer_scenario"]["modals"] == ["legacy-modal"]
    assert state["data"]["scenarios"]["node-1"]["prompt_engineer_scenario"]["catalog"]["apps"] == [{"id": "legacy-app"}]
    assert state["data"]["weather"] == {"city": "Moscow"}
