from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    client = _make_client(skill_mgr, scenario_mgr)

    resp = client.get("/api/skills/list")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"][0]["name"] == "demo"

    assert client.post("/api/skills/sync").status_code == 200

    resp = client.post("/api/skills/install", json={"name": "demo"})
    assert resp.status_code == 200
    assert resp.json()["skill"]["id"] == "demo"

    resp = client.get("/api/skills/demo")
    assert resp.status_code == 200
    assert resp.json()["skill"]["name"] == "demo"

    assert client.delete("/api/skills/demo").status_code == 200

    resp = client.post("/api/skills/push", json={"name": "demo", "message": "msg"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "deadbeef"

    assert any(call.startswith("install:") for call in skill_mgr.calls)
    assert any(call.startswith("push:") for call in skill_mgr.calls)


def test_scenario_api_matches_service_surface() -> None:
    skill_mgr = _FakeSkillManager()
    scenario_mgr = _FakeScenarioManager()
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

    assert client.delete("/api/scenarios/scene").status_code == 200

    resp = client.post("/api/scenarios/push", json={"name": "scene", "message": "msg", "signoff": True})
    assert resp.status_code == 200
    assert resp.json()["revision"] == "cafebabe"

    assert any(call.startswith("push:") for call in scenario_mgr.calls)
