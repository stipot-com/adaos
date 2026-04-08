from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


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


class _FakeCanonicalObject:
    def __init__(
        self,
        object_id: str,
        kind: str,
        title: str,
        *,
        status: str = "online",
        summary: str = "",
    ) -> None:
        self.id = object_id
        self.kind = kind
        self.title = title
        self.status = status
        self.summary = summary

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
        }


def _load_infrascope_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "infrascope_skill" / "handlers" / "main.py"
    module_name = f"test_infrascope_skill_{uuid4().hex}"
    service_key = "adaos.services.system_model.service"
    previous_service_module = sys.modules.get(service_key)
    service_module = types.ModuleType("adaos.services.system_model.service")
    service_module.current_control_plane_objects = lambda webspace_id=None: []
    service_module.current_object_inspector = lambda object_id, task_goal=None, webspace_id=None: None
    service_module.current_overview_projection = lambda webspace_id=None: None
    sys.modules[service_key] = service_module
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_service_module is None:
            sys.modules.pop(service_key, None)
        else:
            sys.modules[service_key] = previous_service_module


def test_infrascope_skill_projects_overview_summary_and_incident_rows(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("local", "hub", "Local hub")
    member = _FakeCanonicalObject("member-1", "member", "Kitchen member", status="degraded", summary="link flaps")
    projection = SimpleNamespace(
        subject=local,
        objects=[member],
        context={
            "summary_tile": {
                "label": "health",
                "value": "degraded",
                "subtitle": "1 active incident",
            },
            "health_strip": [
                {
                    "id": "health:member-1",
                    "object_id": "member-1",
                    "summary": "Link is degraded",
                }
            ],
            "active_incidents": [
                {
                    "id": "incident:member-1",
                    "object_id": "member-1",
                    "title": "Kitchen member",
                    "severity": "high",
                    "summary": "Link is degraded",
                }
            ],
        },
    )

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: projection)

    summary = mod.get_overview_summary()
    incidents = mod.list_overview_collection("active_incidents")
    health = mod.list_overview_collection("health_strip")

    assert summary["object_id"] == "local"
    assert summary["buttons"][-1]["id"] == "inspect_local"
    assert incidents == [
        {
            "id": "incident:member-1",
            "object_id": "member-1",
            "object_title": "Kitchen member",
            "title": "Kitchen member",
            "subtitle": "high | degraded",
            "summary": "Link is degraded",
            "severity": "high",
            "status": "degraded",
            "icon": "git-branch-outline",
            "details": {
                "incident": {
                    "id": "incident:member-1",
                    "object_id": "member-1",
                    "title": "Kitchen member",
                    "severity": "high",
                    "summary": "Link is degraded",
                },
                "object": member.to_dict(),
            },
        }
    ]
    assert health[0]["object_id"] == "member-1"
    assert health[0]["icon"] == "git-branch-outline"


def test_infrascope_skill_inventory_and_inspector_shape(monkeypatch):
    mod = _load_infrascope_module()

    skill = _FakeCanonicalObject("skill:watchdog", "skill", "Watchdog", status="warning", summary="restart suggested")
    scenario = _FakeCanonicalObject("scenario:ops", "scenario", "Ops Desk", status="online")
    subject = _FakeCanonicalObject("hub-1", "hub", "Hub 1", status="degraded", summary="packet loss")
    projection = SimpleNamespace(
        subject=subject,
        context={
            "inspector": {
                "label": "hub",
                "value": "degraded",
                "subtitle": "Hub 1",
            },
            "actions": [{"id": "restart", "title": "Restart"}],
            "recent_changes": [{"id": "change:1", "title": "Config updated"}],
            "topology": {"edges": [{"from": "hub-1", "to": "member-1"}]},
            "task_packet": {"task_goal": "assist operator"},
        },
        incidents=[{"id": "incident:hub-1", "summary": "packet loss"}],
        summary="Hub 1 is degraded",
    )

    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [skill, scenario])
    monkeypatch.setattr(mod, "current_object_inspector", lambda object_id, task_goal=None, webspace_id=None: projection)

    inventory = mod.list_inventory("skills")
    inspector = mod.get_object_inspector("hub-1", task_goal="assist operator")

    assert [item["object_id"] for item in inventory] == ["skill:watchdog"]
    assert inventory[0]["status"] == "warning"
    assert inventory[0]["icon"] == "extension-puzzle-outline"
    assert inspector["object_id"] == "hub-1"
    assert inspector["object"]["kind"] == "hub"
    assert inspector["actions"] == [{"id": "restart", "title": "Restart"}]
    assert inspector["task_packet"] == {"task_goal": "assist operator"}
    assert inspector["topology"] == {"edges": [{"from": "hub-1", "to": "member-1"}]}


def test_infrascope_skill_returns_safe_fallback_for_unknown_object(monkeypatch):
    mod = _load_infrascope_module()

    def _raise(*args, **kwargs):
        raise KeyError("missing")

    monkeypatch.setattr(mod, "current_object_inspector", _raise)

    payload = mod.get_object_inspector("missing-object")

    assert payload["value"] == "unknown"
    assert payload["warning"] == "Object not found: missing-object"
    assert payload["topology"] == {"edges": []}
    assert payload["task_packet"] == {}


def test_infrascope_skill_projects_snapshot_only_when_payload_changes(monkeypatch):
    mod = _load_infrascope_module()

    local = _FakeCanonicalObject("hub:local", "hub", "Local hub", status="online")
    browser = _FakeCanonicalObject("browser:dev-1", "browser_session", "Browser dev-1", status="warning", summary="connecting")

    overview_projection = SimpleNamespace(
        subject=local,
        objects=[browser],
        context={
            "summary_tile": {
                "label": "health",
                "value": "warning",
                "subtitle": "1 browser session",
            },
            "health_strip": [
                {
                    "id": "health:browsers",
                    "object_id": "browser:dev-1",
                    "title": "Browsers",
                    "summary": "Browser dev-1 is connecting",
                }
            ],
            "active_incidents": [],
            "active_runtimes": [],
            "quota_summary": [],
            "recent_changes": [],
        },
    )

    def _inspector(object_id, task_goal=None, webspace_id=None):
        subject = local if object_id in {"local", "hub:local"} else browser
        return SimpleNamespace(
            subject=subject,
            context={
                "inspector": {
                    "label": subject.kind,
                    "value": subject.status,
                    "subtitle": subject.title,
                },
                "actions": [],
                "recent_changes": [],
                "topology": {"edges": []},
                "task_packet": {"task_goal": task_goal or "assist operator"},
            },
            incidents=[],
            summary=f"{subject.title} summary",
        )

    writes: list[tuple[str, str | None, str]] = []

    monkeypatch.setattr(mod, "current_overview_projection", lambda webspace_id=None: overview_projection)
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [local, browser])
    monkeypatch.setattr(mod, "current_object_inspector", _inspector)
    monkeypatch.setattr(mod, "_projection_webspace_ids", lambda webspace_id=None: [str(webspace_id or "ws-1")])
    monkeypatch.setattr(
        mod,
        "ctx_subnet",
        SimpleNamespace(set=lambda slot, value, webspace_id=None: writes.append((slot, webspace_id, value["summary"]["value"]))),
    )

    snapshot = mod.get_snapshot(webspace_id="ws-1")
    first = mod.refresh_snapshot(webspace_id="ws-1")
    second = mod.refresh_snapshot(webspace_id="ws-1")

    assert snapshot["inventory"]["browsers"][0]["object_id"] == "browser:dev-1"
    assert snapshot["inspectors"]["local"]["object_id"] == "hub:local"
    assert snapshot["inspectors"]["browser:dev-1"]["object"]["kind"] == "browser_session"
    assert first["projected"] == 1
    assert second["projected"] == 0
    assert writes == [("infrascope.snapshot", "ws-1", "warning")]


def test_infrascope_scenario_declares_inventory_drilldown_and_inspector_flow():
    root = Path(__file__).resolve().parents[1]
    scenario_path = root / ".adaos" / "workspace" / "scenarios" / "infrascope" / "scenario.json"
    skill_path = root / ".adaos" / "workspace" / "skills" / "infrascope_skill" / "skill.yaml"

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    skill_yaml = skill_path.read_text(encoding="utf-8")
    widgets = {item["id"]: item for item in scenario["ui"]["application"]["desktop"]["pageSchema"]["widgets"]}

    inventory = widgets["inventory-list"]
    incidents = widgets["overview-incidents"]
    operations = widgets["overview-operations"]
    summary = widgets["selected-object-summary"]
    mode = widgets["infrascope-mode"]
    inventory_tabs = widgets["infrascope-inventory-tabs"]
    inspector_tabs = widgets["inspector-tabs"]

    assert scenario["type"] == "desktop"
    assert inventory["visibleIf"] == "$state.infrascopeMode === 'inventory'"
    assert inventory["dataSource"]["kind"] == "y"
    assert inventory["dataSource"]["path"] == "data/infrascope/inventory/$state.inventoryKind"
    assert "refreshMs" not in inventory.get("inputs", {})
    assert incidents["actions"][0]["params"]["inspectorTab"] == "incidents"
    assert operations["dataSource"]["path"] == "data/infrascope/operations/items"
    assert summary["dataSource"]["kind"] == "y"
    assert summary["dataSource"]["path"] == "data/infrascope/inspectors/$state.selectedObjectId"
    assert widgets["overview-summary"]["dataSource"]["path"] == "data/infrascope/summary"
    assert mode["inputs"]["selectedStateKey"] == "infrascopeMode"
    assert inventory_tabs["inputs"]["selectedStateKey"] == "inventoryKind"
    assert inspector_tabs["inputs"]["selectedStateKey"] == "inspectorTab"
    assert "get_overview_summary" in skill_yaml
    assert "get_object_inspector" in skill_yaml
    assert "get_snapshot" in skill_yaml
    assert "refresh_snapshot" in skill_yaml
    assert "data_projections" in skill_yaml
    assert "infrascope.snapshot" in skill_yaml
    assert "device.registered" in skill_yaml
    assert "browser.session.changed" in skill_yaml
    assert "webrtc.peer.state.changed" in skill_yaml
    assert "workspace." in skill_yaml
    assert "user.profile.changed" in skill_yaml
    assert "capacity.changed" in skill_yaml


def test_infrascope_adds_skill_migration_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(
        mod,
        "_skill_runtime_migration_report",
        lambda: {
            "total": 2,
            "failed_total": 1,
            "rollback_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {"skill": "voice_skill", "ok": False, "failed_stage": "tests"},
            ],
        },
    )

    snapshot = mod.get_snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-runtime-migration"
    assert rows[0]["status"] == "offline"
    assert "voice_skill:tests" in rows[0]["subtitle"]


def test_infrascope_adds_skill_rollback_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda: {})
    monkeypatch.setattr(
        mod,
        "_skill_runtime_rollback_report",
        lambda: {
            "total": 3,
            "failed_total": 1,
            "rollback_total": 2,
            "skipped_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {"skill": "voice_skill", "ok": False, "error": "broken rollback"},
                {"skill": "maps_skill", "ok": True, "skipped": True},
            ],
        },
    )

    snapshot = mod.get_snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-runtime-rollback"
    assert rows[0]["status"] == "offline"
    assert "2/3 rolled back" in rows[0]["subtitle"]


def test_infrascope_adds_skill_post_commit_operation_row(monkeypatch):
    mod = _load_infrascope_module()

    monkeypatch.setattr(mod, "get_overview_summary", lambda webspace_id=None: {"label": "scope", "value": "warning"})
    monkeypatch.setattr(mod, "list_overview_collection", lambda section, webspace_id=None: [])
    monkeypatch.setattr(mod, "list_inventory", lambda kind, webspace_id=None: [])
    monkeypatch.setattr(mod, "current_control_plane_objects", lambda webspace_id=None: [])
    monkeypatch.setattr(mod, "get_object_inspector", lambda object_id, task_goal=None, webspace_id=None: {"object_id": object_id})
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active": [], "active_items": []})
    monkeypatch.setattr(mod, "_skill_runtime_migration_report", lambda: {})
    monkeypatch.setattr(mod, "_skill_runtime_rollback_report", lambda: {})
    monkeypatch.setattr(
        mod,
        "_skill_post_commit_checks_report",
        lambda: {
            "total": 2,
            "failed_total": 1,
            "deactivated_total": 1,
            "skills": [
                {"skill": "weather_skill", "ok": True},
                {"skill": "voice_skill", "ok": False, "failed_stage": "tests", "deactivated": True},
            ],
        },
    )

    snapshot = mod.get_snapshot()

    rows = snapshot["operations"]["items"]
    assert rows
    assert rows[0]["id"] == "core-update-skill-post-commit-checks"
    assert rows[0]["status"] == "offline"
    assert "deactivated=1" in rows[0]["subtitle"]
