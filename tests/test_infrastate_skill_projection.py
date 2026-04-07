from __future__ import annotations

import importlib.util
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


def _load_infrastate_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".adaos" / "workspace" / "skills" / "infrastate_skill" / "handlers" / "main.py"
    module_name = f"test_infrastate_skill_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_infrastate_yjs_tabs_do_not_self_reference_sync_runtime():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"

    reliability = {
        "runtime": {
            "sync_runtime": {
                "webspaces": {
                    "default": {
                        "log_mode": "snapshot_plus_diff",
                        "update_log_entries": 1,
                        "replay_window_entries": 1,
                    }
                }
            }
        }
    }

    selected = mod._selected_yjs_webspace_id({}, reliability)
    items = mod._yjs_webspace_tabs(_Conf(), {}, reliability, {"kind": "local"})

    assert selected == "default"
    assert items
    assert items[0]["id"] == "default"


def test_infrastate_node_label_skips_webspace_like_noise():
    mod = _load_infrastate_module()

    label = mod._node_label(["default", "desktop", {"WEBSPACE_ID": "DEFAULT"}, "TE1"], fallback="hub")

    assert label == "TE1"


def test_infrastate_node_tabs_keep_offline_member_selected():
    mod = _load_infrastate_module()

    class _Conf:
        role = "hub"
        node_id = "hub-1"
        node_names = ["Hub"]

    reliability = {
        "runtime": {
            "hub_member_connection_state": {
                "known_members": [
                    {
                        "node_id": "member-1",
                        "node_names": ["TE1"],
                        "connected": False,
                        "state": "offline",
                        "observed_via": "subnet_directory",
                    }
                ]
            }
        }
    }

    tabs, selected = mod._node_tabs(_Conf(), {"selected_node_id": "member-1"}, reliability)

    assert any(item["id"] == "member-1" for item in tabs)
    assert selected["node_id"] == "member-1"
    assert selected["kind"] == "member"
    assert selected["connected"] is False


def test_infrastate_get_snapshot_projects_fallback_when_snapshot_crashes(monkeypatch):
    mod = _load_infrastate_module()
    projected: dict[str, object] = {}

    def _boom(*, webspace_id=None):
        raise UnboundLocalError("cannot access local variable 'sync_runtime' where it is not associated with a value")

    monkeypatch.setattr(mod, "_snapshot", _boom)
    monkeypatch.setattr(mod, "_project", lambda snapshot, webspace_id=None: projected.update({"snapshot": snapshot, "webspace_id": webspace_id}))
    monkeypatch.setattr(mod, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready"})
    monkeypatch.setattr(mod, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod,
        "_reliability_snapshot",
        lambda conf, lifecycle: {
            "runtime": {
                "sync_runtime": {
                    "assessment": {"state": "nominal", "reason": "test"},
                    "selected_webspace_id": "default",
                }
            }
        },
    )
    monkeypatch.setattr(mod, "_event_state", lambda: [])

    snapshot = mod.get_snapshot(webspace_id="default")

    assert snapshot["fallback"] is True
    assert "sync_runtime" in snapshot["errors"][0]
    assert projected["webspace_id"] == "default"
    assert isinstance(projected["snapshot"], dict)
    assert projected["snapshot"]["fallback"] is True


def test_infrastate_scenario_items_use_registry_and_repo_versions(monkeypatch):
    mod = _load_infrastate_module()

    class _ScenarioRecord:
        def __init__(self, name: str, active_version: str, last_updated: float | None = None):
            self.name = name
            self.active_version = active_version
            self.last_updated = last_updated

    class _MetaId:
        def __init__(self, value: str):
            self.value = value

    repo_metas = [
        SimpleNamespace(id=_MetaId("alpha"), version="1.2.3"),
        SimpleNamespace(id=_MetaId("beta"), version="2.0.0"),
    ]

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            scenarios_repo=SimpleNamespace(list=lambda: repo_metas),
        ),
    )
    monkeypatch.setattr(
        mod,
        "SqliteScenarioRegistry",
        lambda sql: SimpleNamespace(
            list=lambda: [
                _ScenarioRecord("alpha", "1.0.0", 1.0),
                _ScenarioRecord("gamma", "3.0.0", 2.0),
            ]
        ),
    )

    items = mod._scenario_items()

    assert items == [
        {"name": "alpha", "version": "1.2.3", "updated_at": 1.0},
        {"name": "gamma", "version": "3.0.0", "updated_at": 2.0},
    ]


def test_infrastate_skill_items_use_registry_catalog_for_update_status(monkeypatch):
    mod = _load_infrastate_module()

    class _MetaId:
        def __init__(self, value: str):
            self.value = value

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            skills_repo=SimpleNamespace(list=lambda: [SimpleNamespace(id=_MetaId("infrastate_skill"), version="0.19.0")]),
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: Path(".")),
            bus=None,
            caps=object(),
            settings=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())
    monkeypatch.setattr(mod, "SkillManager", lambda **kwargs: SimpleNamespace(runtime_status=lambda name: {"active_slot": "A"}))
    monkeypatch.setattr(mod, "find_workspace_registry_entry", lambda *args, **kwargs: {"version": "0.20.0"})

    items = mod._skills_items()

    assert items == [
        {
            "name": "infrastate_skill",
            "version": "0.19.0",
            "slot": "A",
            "remote_version": "0.20.0",
            "update_available": True,
        }
    ]


def test_infrastate_marketplace_filters_installed_and_marks_running_operations(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: Path("."))),
    )
    monkeypatch.setattr(
        mod,
        "list_workspace_registry_entries",
        lambda workspace_root, kind=None: (
            [
                {"kind": "skill", "id": "installed_skill", "name": "installed_skill", "version": "1.0.0"},
                {"kind": "skill", "id": "queued_skill", "name": "queued_skill", "version": "1.2.0"},
            ]
            if kind == "skills"
            else [{"kind": "scenario", "id": "new_scene", "name": "new_scene", "version": "0.5.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [{"name": "installed_skill", "version": "1.0.0", "slot": "A"}])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(
        mod,
        "get_operation_manager",
        lambda: SimpleNamespace(
            snapshot=lambda webspace_id=None: {
                "active_items": [
                    {
                        "target_kind": "skill",
                        "target_id": "queued_skill",
                        "status": "running",
                        "current_step": "skill.install",
                    }
                ]
            }
        ),
    )

    items = mod._marketplace_items(webspace_id="default")

    assert [item["id"] for item in items["skills"]] == ["queued_skill"]
    assert items["skills"][0]["install_disabled"] is True
    assert items["skills"][0]["operation_status"] == "running"
    assert [item["id"] for item in items["scenarios"]] == ["new_scene"]


def test_infrastate_effective_runtime_projection_prefers_validated_target_slot():
    mod = _load_infrastate_module()

    slots_payload = {
        "active_slot": "A",
        "previous_slot": "B",
        "slots": {
            "A": {"manifest": {"slot": "A", "git_short_commit": "ddeb33f", "git_commit": "ddeb33f-old"}},
            "B": {"manifest": {"slot": "B", "git_short_commit": "stale-b"}},
        },
    }
    build = {
        "runtime_version": "old",
        "runtime_git_commit": "ddeb33f-old",
        "runtime_git_short_commit": "ddeb33f",
        "runtime_git_branch": "HEAD",
        "runtime_git_subject": "old subject",
    }
    status = {
        "state": "succeeded",
        "phase": "validate",
        "target_slot": "B",
        "manifest": {
            "slot": "B",
            "target_version": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_commit": "8dd3543c72f912ef0d7932f4c5754ce4c6700849",
            "git_short_commit": "8dd3543",
            "git_branch": "HEAD",
            "git_subject": "feat: add skill-aware infra_access publication and Root MCP token lifecycle management",
        },
    }

    effective_slots, effective_build = mod._effective_runtime_projection(status, {}, slots_payload, build)

    assert effective_slots["active_slot"] == "B"
    assert effective_slots["previous_slot"] == "A"
    assert effective_slots["slots"]["B"]["manifest"]["git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_short_commit"] == "8dd3543"
    assert effective_build["runtime_git_commit"] == "8dd3543c72f912ef0d7932f4c5754ce4c6700849"
