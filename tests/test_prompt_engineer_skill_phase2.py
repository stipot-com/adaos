from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_prompt_engineer_module(monkeypatch):
    monkeypatch.setenv("ADAOS_VALIDATE", "1")
    module_path = Path(__file__).resolve().parents[1] / ".adaos" / "workspace" / "skills" / "prompt_engineer_skill" / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("prompt_engineer_skill_phase2_main", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_create_dev_project_accepts_project_alias_payload(monkeypatch) -> None:
    module = _load_prompt_engineer_module(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    class _Svc:
        def create_scenario(self, name: str, template: str | None = None):
            captured.append((name, template))
            return SimpleNamespace(name=name, path=Path(f"/tmp/{name}"))

    monkeypatch.setattr(module, "RootDeveloperService", lambda: _Svc())
    monkeypatch.setattr(module, "_require_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(module, "bus_emit", lambda *args, **kwargs: None)

    result = module.prompt_create_dev_project(
        {
            "project_type": "scenario",
            "project_id": "demo_scenario",
            "template": "default",
        }
    )

    assert captured == [("demo_scenario", "default")]
    assert result["ok"] is True
    assert result["object_type"] == "scenario"
    assert result["object_id"] == "demo_scenario"
    assert result["project_id"] == "demo_scenario"


def test_prompt_create_dev_project_creates_skill(monkeypatch) -> None:
    module = _load_prompt_engineer_module(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    class _Svc:
        def create_skill(self, name: str, template: str | None = None):
            captured.append((name, template))
            return SimpleNamespace(name=name, path=Path(f"/tmp/{name}"))

    monkeypatch.setattr(module, "RootDeveloperService", lambda: _Svc())
    monkeypatch.setattr(module, "_require_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(module, "bus_emit", lambda *args, **kwargs: None)

    result = module.prompt_create_dev_project(
        {
            "object_type": "skill",
            "name": "demo_skill",
        }
    )

    assert captured == [("demo_skill", None)]
    assert result["ok"] is True
    assert result["object_type"] == "skill"
    assert result["object_id"] == "demo_skill"


def test_prompt_create_dev_project_normalizes_selector_like_template_payload(monkeypatch) -> None:
    module = _load_prompt_engineer_module(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    class _Svc:
        def create_scenario(self, name: str, template: str | None = None):
            captured.append((name, template))
            return SimpleNamespace(name=name, path=Path(f"/tmp/{name}"))

    monkeypatch.setattr(module, "RootDeveloperService", lambda: _Svc())
    monkeypatch.setattr(module, "_require_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(module, "bus_emit", lambda *args, **kwargs: None)

    result = module.prompt_create_dev_project(
        {
            "object_type": {"id": "scenario"},
            "name": {"value": "selector_scenario"},
            "template": {"id": "scenario_default", "label": "Default"},
        }
    )

    assert captured == [("selector_scenario", "scenario_default")]
    assert result["ok"] is True
    assert result["object_type"] == "scenario"
    assert result["object_id"] == "selector_scenario"


def test_prompt_create_dev_project_returns_structured_error(monkeypatch) -> None:
    module = _load_prompt_engineer_module(monkeypatch)

    class _Svc:
        def create_scenario(self, name: str, template: str | None = None):  # noqa: ARG002
            raise module.RootServiceError(f"Target already exists: /tmp/{name}")

    monkeypatch.setattr(module, "RootDeveloperService", lambda: _Svc())

    result = module.prompt_create_dev_project(
        {
            "object_type": "scenario",
            "name": "existing_scenario",
        }
    )

    assert result["ok"] is False
    assert "Target already exists" in result["error"]
