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


def test_infrastate_scenario_items_only_show_installed_registry_entries(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    alpha_dir = workspace / "scenarios" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "scenario.yaml").write_text("id: alpha\nversion: '1.2.3'\n", encoding="utf-8")
    beta_dir = workspace / "scenarios" / "beta"
    beta_dir.mkdir(parents=True, exist_ok=True)
    (beta_dir / "scenario.yaml").write_text("id: beta\nversion: '2.0.0'\n", encoding="utf-8")

    class _ScenarioRecord:
        def __init__(self, name: str, active_version: str, last_updated: float | None = None):
            self.name = name
            self.active_version = active_version
            self.last_updated = last_updated

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            git=object(),
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
        {"name": "alpha", "version": "1.2.3", "updated_at": 1.0, "uninstall_disabled": False},
        {"name": "gamma", "version": "3.0.0", "updated_at": 2.0, "uninstall_disabled": False},
    ]


def test_infrastate_skill_items_use_registry_and_workspace_versions(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrastate_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: infrastate_skill\nversion: '0.19.0'\n", encoding="utf-8")
    extra_dir = workspace / "skills" / "extra_skill"
    extra_dir.mkdir(parents=True, exist_ok=True)
    (extra_dir / "skill.yaml").write_text("id: extra_skill\nversion: '9.9.9'\n", encoding="utf-8")

    class _SkillRecord:
        def __init__(self, name: str, active_version: str, installed: bool = True):
            self.name = name
            self.active_version = active_version
            self.installed = installed

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(
            sql=object(),
            git=object(),
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            bus=None,
            caps=object(),
            settings=object(),
            skills_repo=object(),
        ),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: SimpleNamespace(list=lambda: [_SkillRecord("infrastate_skill", "0.18.0")]))
    monkeypatch.setattr(mod, "SkillManager", lambda **kwargs: SimpleNamespace(runtime_status=lambda name: {"active_slot": "A"}))
    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: [{"id": "infrastate_skill", "name": "infrastate_skill", "version": "0.20.0"}] if kind == "skills" else [],
    )

    items = mod._skills_items()

    assert items == [
        {
            "name": "infrastate_skill",
            "display_name": "infrastate_skill *",
            "version": "0.19.0",
            "version_display": "0.19.0 (0.20.0)",
            "slot": "A",
            "active": True,
            "can_activate": True,
            "can_test": True,
            "used_by_scenarios": [],
            "uninstall_disabled": False,
            "remote_version": "0.20.0",
            "update_available": True,
        }
    ]


def test_infrastate_adaos_update_uses_union_sparse_sync_and_installed_skill_names(monkeypatch):
    mod = _load_infrastate_module()
    runtime_updates: list[str] = []

    ctx = SimpleNamespace(
        sql=object(),
        git=object(),
        paths=SimpleNamespace(workspace_dir=lambda: Path(".")),
        bus=None,
        caps=object(),
        settings=object(),
        skills_repo=object(),
        scenarios_repo=object(),
    )

    monkeypatch.setattr(mod, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        mod,
        "sync_workspace_sparse_to_registry",
        lambda current_ctx: {"ok": True, "skills": ["installed_skill"], "scenarios": ["scene_one"], "fallback_used": {}},
    )
    monkeypatch.setattr(
        mod,
        "SkillManager",
        lambda **kwargs: SimpleNamespace(runtime_update=lambda name, space="workspace": runtime_updates.append(name) or {"ok": True}),
    )
    monkeypatch.setattr(mod, "SqliteSkillRegistry", lambda sql: object())

    result = mod._adaos_update_local()

    assert result["ok"] is True
    assert result["skills_synced"] is True
    assert result["scenarios_synced"] is True
    assert result["skills"] == ["installed_skill"]
    assert result["scenarios"] == ["scene_one"]
    assert runtime_updates == ["installed_skill"]


def test_infrastate_marketplace_filters_installed_and_marks_running_operations(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
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


def test_infrastate_marketplace_catalog_prefers_remote_registry_and_local_scan(monkeypatch, tmp_path: Path):
    mod = _load_infrastate_module()
    workspace = tmp_path / "workspace"
    scenario_dir = workspace / "scenarios" / "infrascope"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.yaml").write_text(
        "\n".join(
            [
                "id: infrascope",
                "name: Infrascope",
                "version: '0.2.0'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        mod,
        "get_ctx",
        lambda: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )
    monkeypatch.setattr(mod, "list_workspace_registry_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"scenarios":[{"kind":"scenario","id":"remote_scene","name":"remote_scene","version":"1.0.0"}]}',
            stderr="",
        ),
    )

    items = mod._marketplace_catalog_entries("scenarios")

    assert [item["name"] for item in items] == ["infrascope", "remote_scene"]


def test_infrastate_marketplace_hides_skills_installed_via_scenario_dependencies(monkeypatch):
    mod = _load_infrastate_module()

    monkeypatch.setattr(
        mod,
        "_marketplace_catalog_entries",
        lambda kind: (
            [{"kind": "skill", "id": "prompt_engineer_skill", "name": "prompt_engineer_skill", "version": "0.5.0"}]
            if kind == "skills"
            else [{"kind": "scenario", "id": "prompt_engineer_scenario", "name": "prompt_engineer_scenario", "version": "0.2.0"}]
        ),
    )
    monkeypatch.setattr(mod, "_skills_items", lambda: [])
    monkeypatch.setattr(mod, "_scenario_items", lambda: [])
    monkeypatch.setattr(mod, "_operations_snapshot", lambda webspace_id=None: {"active_items": []})
    monkeypatch.setattr(mod, "read_manifest", lambda name: {"depends": ["prompt_engineer_skill"]} if name == "prompt_engineer_scenario" else {})

    items = mod._marketplace_items(webspace_id="default")

    assert items["skills"] == []
    assert [item["id"] for item in items["scenarios"]] == ["prompt_engineer_scenario"]


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


def test_infrastate_skill_runtime_migration_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_migration_report(
        {},
        {
            "manifest": {
                "skill_runtime_migration": {
                    "total": 2,
                    "failed_total": 1,
                    "rollback_total": 1,
                    "skills": [
                        {"skill": "weather_skill", "ok": True},
                        {"skill": "voice_skill", "ok": False, "failed_stage": "tests"},
                    ],
                }
            }
        },
    )
    note = mod._skill_runtime_migration_note(report)

    assert report["failed_total"] == 1
    assert "skill_migration=1/2" in note
    assert "voice_skill:tests" in note
    assert "rollback=1" in note


def test_infrastate_skill_runtime_rollback_helpers_report_failures():
    mod = _load_infrastate_module()

    report = mod._skill_runtime_rollback_report(
        {
            "skill_runtime_rollback": {
                "total": 3,
                "failed_total": 1,
                "rollback_total": 2,
                "skipped_total": 1,
                "skills": [
                    {"skill": "weather_skill", "ok": True},
                    {"skill": "voice_skill", "ok": False, "error": "broken rollback"},
                    {"skill": "maps_skill", "ok": True, "skipped": True},
                ],
            }
        },
        {},
    )
    note = mod._skill_runtime_rollback_note(report)

    assert report["failed_total"] == 1
    assert "skill_rollback=2/3" in note
    assert "failed=voice_skill" in note
    assert "skipped=1" in note
