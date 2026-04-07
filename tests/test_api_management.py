from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

from adaos.apps.api import scenarios, skills
from adaos.apps.api.auth import require_token


@dataclass
class _Record:
    name: str
    installed: bool = True
    active_version: Optional[str] = None


@dataclass
class _Meta:
    id: Any
    name: str
    version: str
    path: str


class _FakeSkillManager:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def list_installed(self) -> list[_Record]:
        self.calls.append("list_installed")
        return [_Record(name="demo", installed=True, active_version="1.0.0")]

    def list_present(self) -> list[_Meta]:
        self.calls.append("list_present")
        return [_Meta(id=type("Id", (), {"value": "demo"})(), name="demo", version="1.0.0", path="/skills/demo")]

    def sync(self) -> None:
        self.calls.append("sync")

    def install(self, name: str, **kwargs: Any):
        self.calls.append(f"install:{name}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="1.0.0", path=f"/skills/{name}")

    def get(self, name: str):
        self.calls.append(f"get:{name}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="1.0.0", path=f"/skills/{name}")

    def runtime_status(self, name: str):
        self.calls.append(f"runtime_status:{name}")
        return {"active_slot": "A", "version": "1.0.0"}

    def runtime_update(self, name: str, *, space: str = "workspace"):
        self.calls.append(f"runtime_update:{name}:{space}")
        return {"ok": True, "version": "1.0.0"}

    def prepare_runtime(self, name: str, run_tests: bool = False):
        self.calls.append(f"prepare_runtime:{name}")
        return SimpleNamespace(version="2.0.0", slot="B")

    def activate_for_space(self, name: str, *, version: str | None = None, slot: str | None = None, space: str = "default", webspace_id: str = "default"):
        self.calls.append(f"activate_for_space:{name}:{version}:{slot}:{webspace_id}")
        return slot or "B"

    def uninstall(self, name: str) -> None:
        self.calls.append(f"uninstall:{name}")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.calls.append(f"push:{name}:{message}:{int(signoff)}")
        return "deadbeef"


class _FakeScenarioManager:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def list_installed(self) -> list[_Record]:
        self.calls.append("list_installed")
        return [_Record(name="scene", installed=True, active_version="0.1.0")]

    def list_present(self) -> list[_Meta]:
        self.calls.append("list_present")
        return [_Meta(id=type("Id", (), {"value": "scene"})(), name="scene", version="0.1.0", path="/scenarios/scene")]

    def sync(self) -> None:
        self.calls.append("sync")

    def install(self, name: str, *, pin: str | None = None):
        self.calls.append(f"install:{name}:{pin}")
        return _Meta(id=type("Id", (), {"value": name})(), name=name, version="0.1.0", path=f"/scenarios/{name}")

    def uninstall(self, name: str) -> None:
        self.calls.append(f"uninstall:{name}")

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.calls.append(f"push:{name}:{message}:{int(signoff)}")
        return "cafebabe"


def _make_client(skill_mgr: _FakeSkillManager, scenario_mgr: _FakeScenarioManager) -> TestClient:
    app = FastAPI()
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.dependency_overrides[require_token] = lambda: None
    app.dependency_overrides[skills._get_manager] = lambda: skill_mgr
    app.dependency_overrides[scenarios._get_manager] = lambda: scenario_mgr
    return TestClient(app)


def test_skill_api_exposes_management_routes() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    skills.submit_install_operation = lambda **kwargs: {
        "operation_id": "op-skill-demo",
        "target_id": kwargs["target_id"],
        "target_kind": kwargs["target_kind"],
        "status": "accepted",
    }
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/skills/list")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"][0]["name"] == "demo"

    assert client.post("/api/skills/sync").status_code == 200

    resp = client.post("/api/skills/install", json={"name": "demo"})
    assert resp.status_code == 200
    assert resp.json()["skill"]["id"] == "demo"
    assert resp.json()["runtime"]["slot"] == "B"

    resp = client.post("/api/skills/install", json={"name": "demo", "async_operation": True, "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["operation"]["target_id"] == "demo"

    resp = client.get("/api/skills/demo")
    assert resp.status_code == 200
    assert resp.json()["skill"]["name"] == "demo"

    assert client.delete("/api/skills/demo").status_code == 200

    resp = client.post("/api/skills/push", json={"name": "demo", "message": "msg"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "deadbeef"

    assert any(call.startswith("install:") for call in skill_mgr.calls)
    assert "prepare_runtime:demo" in skill_mgr.calls
    assert any(call.startswith("activate_for_space:demo:") and call.endswith(":desktop") for call in skill_mgr.calls)
    assert any(call.startswith("push:") for call in skill_mgr.calls)


def test_scenario_api_matches_service_surface() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    scenarios.submit_install_operation = lambda **kwargs: {
        "operation_id": "op-scenario-scene",
        "target_id": kwargs["target_id"],
        "target_kind": kwargs["target_kind"],
        "status": "accepted",
    }
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/scenarios/list?fs=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"][0]["name"] == "scene"
    assert "fs" in data

    assert client.post("/api/scenarios/sync").status_code == 200

    resp = client.post("/api/scenarios/install", json={"name": "scene"})
    assert resp.status_code == 200
    assert resp.json()["scenario"]["id"] == "scene"

    resp = client.post("/api/scenarios/install", json={"name": "scene", "async_operation": True, "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["operation"]["target_id"] == "scene"

    assert client.delete("/api/scenarios/scene").status_code == 200

    resp = client.post("/api/scenarios/push", json={"name": "scene", "message": "msg", "signoff": True})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "cafebabe"

    assert any(call.startswith("push:") for call in scenario_mgr.calls)


def test_skill_installed_status_uses_registry_catalog_version(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    monkeypatch.setattr(skills, "find_workspace_registry_entry", lambda *args, **kwargs: {"version": "2.0.0"})
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/skills/installed-status")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["remote_version"] == "2.0.0"
    assert item["update_available"] is True


def test_skill_update_refreshes_runtime_when_source_version_changed(monkeypatch) -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
    client = _make_client(skill_mgr, scenario_mgr)

    class _Service:
        def __init__(self, ctx) -> None:
            self.ctx = ctx

        def request_update(self, skill_id: str, *, dry_run: bool = False):
            return SimpleNamespace(updated=True, version="2.0.0")

    async def _rebuild(*args, **kwargs):
        return None

    monkeypatch.setattr(skills, "SkillUpdateService", _Service)
    monkeypatch.setattr(skills, "_get_manager", lambda ctx: skill_mgr)
    monkeypatch.setattr(skills, "rebuild_webspace_from_sources", _rebuild)

    resp = client.post("/api/skills/update", json={"name": "demo", "webspace_id": "default"})
    assert resp.status_code == 200
    assert resp.json()["updated"] is True
    assert "runtime_update:demo:workspace" in skill_mgr.calls
    assert "prepare_runtime:demo" in skill_mgr.calls
    assert any(call.startswith("activate_for_space:demo:2.0.0:B:default") for call in skill_mgr.calls)
