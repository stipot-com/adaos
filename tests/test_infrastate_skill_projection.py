from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


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

    def _boom():
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
