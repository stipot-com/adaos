from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from adaos.services.agent_context import get_ctx

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.services.scenario import webspace_runtime as webspace_runtime_module
from adaos.services.workspaces import (
    ensure_workspace,
    get_workspace,
    set_workspace_installed_overlay,
    set_workspace_manifest,
    set_workspace_pinned_widgets_overlay,
    set_workspace_topbar_overlay,
    set_workspace_page_schema_overlay,
)


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

    def get_map(self, name: str) -> _FakeMap:
        return self._state.setdefault(name, _FakeMap())

    def begin_transaction(self) -> _FakeTxn:
        return _FakeTxn()


class _FakeAsyncDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    async def __aenter__(self) -> _FakeDoc:
        return _FakeDoc(self._state)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def test_describe_webspace_operational_state_exposes_manifest_and_current_scenario(monkeypatch) -> None:
    webspace_id = "phase2-describe"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_runtime"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    result = asyncio.run(webspace_runtime_module.describe_webspace_operational_state(webspace_id))

    assert result.webspace_id == webspace_id
    assert result.kind == "dev"
    assert result.source_mode == "dev"
    assert result.stored_home_scenario == "prompt_engineer_scenario"
    assert result.effective_home_scenario == "prompt_engineer_scenario"
    assert result.current_scenario == "prompt_engineer_runtime"
    assert result.to_dict()["current_matches_home"] is False


def test_describe_webspace_projection_state_reports_active_layer(monkeypatch) -> None:
    webspace_id = "phase4-projection-describe"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Projection Lab",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    class _Projections:
        def snapshot(self) -> dict[str, object]:
            return {
                "active_scenario_id": "prompt_engineer_scenario",
                "active_space": "workspace",
                "base_rule_count": 2,
                "scenario_rule_count": 1,
            }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace(projections=_Projections()))

    result = asyncio.run(webspace_runtime_module.describe_webspace_projection_state(webspace_id))

    assert result["webspace_id"] == webspace_id
    assert result["target_scenario"] == "prompt_engineer_scenario"
    assert result["target_space"] == "workspace"
    assert result["active_scenario"] == "prompt_engineer_scenario"
    assert result["active_space"] == "workspace"
    assert result["active_matches_target"] is True
    assert result["base_rule_count"] == 2
    assert result["scenario_rule_count"] == 1


def test_describe_webspace_projection_state_detects_space_mismatch(monkeypatch) -> None:
    webspace_id = "phase4-projection-dev-mismatch"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }

    class _Projections:
        def snapshot(self) -> dict[str, object]:
            return {
                "active_scenario_id": "prompt_engineer_scenario",
                "active_space": "workspace",
                "base_rule_count": 2,
                "scenario_rule_count": 1,
            }

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: SimpleNamespace(projections=_Projections()))

    result = asyncio.run(webspace_runtime_module.describe_webspace_projection_state(webspace_id))

    assert result["target_space"] == "dev"
    assert result["active_space"] == "workspace"
    assert result["active_matches_target"] is False


def _patch_switch_dependencies(monkeypatch, *, state: dict[str, _FakeMap] | None = None) -> dict[str, _FakeMap]:
    fake_state = state or {"ui": _FakeMap(), "registry": _FakeMap(), "data": _FakeMap()}
    fake_ctx = get_ctx()
    rebuilds: list[str] = []
    workflows: list[tuple[str, str]] = []
    sync_listing_calls: list[bool] = []

    async def _fake_rebuild(self, webspace_id: str):
        rebuilds.append(webspace_id)
        return SimpleNamespace(webspace_id=webspace_id)

    async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):
        workflows.append((scenario_id, webspace_id))
        return None

    async def _fake_sync_listing() -> None:
        sync_listing_calls.append(True)

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(
        webspace_runtime_module,
        "_load_scenario_switch_content",
        lambda scenario_id, *, space: {
            "id": scenario_id,
            "ui": {"application": {"desktop": {"pageSchema": {"id": f"page-{scenario_id}"}}}},
            "registry": {"modals": [f"modal:{space}:{scenario_id}"]},
            "catalog": {"apps": [{"id": f"app:{scenario_id}"}]},
            "data": {"status": {"scenario": scenario_id, "space": space}},
        },
    )
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    fake_state["_meta"] = _FakeMap({"rebuilds": rebuilds, "workflows": workflows, "listing_syncs": sync_listing_calls})
    return fake_state


def test_switch_webspace_scenario_can_persist_home_scenario(monkeypatch) -> None:
    webspace_id = "phase2-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Home",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
            set_home=True,
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["_meta"]["rebuilds"] == [webspace_id]
    assert fake_state["_meta"]["workflows"] == [("prompt_engineer_scenario", webspace_id)]
    assert fake_state["_meta"]["listing_syncs"] == [True]
    assert result["ok"] is True
    assert result["set_home"] is True
    assert result["home_scenario"] == "prompt_engineer_scenario"


def test_switch_webspace_scenario_auto_persists_home_for_dev_webspace(monkeypatch) -> None:
    webspace_id = "phase2-dev-auto-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="web_desktop",
    )

    fake_state = _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert fake_state["ui"]["current_scenario"] == "prompt_engineer_scenario"
    assert fake_state["_meta"]["listing_syncs"] == [True]
    assert result["set_home"] is True
    assert result["home_scenario"] == "prompt_engineer_scenario"


def test_switch_webspace_scenario_keeps_home_unchanged_for_regular_workspace(monkeypatch) -> None:
    webspace_id = "phase2-workspace-no-auto-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Phase 2 Workspace",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    _patch_switch_dependencies(monkeypatch)

    result = asyncio.run(
        webspace_runtime_module.switch_webspace_scenario(
            webspace_id,
            "prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "web_desktop"
    assert result["set_home"] is False
    assert result["home_scenario"] == "web_desktop"


def test_go_home_webspace_uses_manifest_home_scenario(monkeypatch) -> None:
    webspace_id = "phase2-go-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(webspace_id: str, scenario_id: str, *, set_home: bool = False) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    result = asyncio.run(webspace_runtime_module.go_home_webspace(webspace_id))

    assert captured == [(webspace_id, "prompt_engineer_scenario", False)]
    assert result["scenario_id"] == "prompt_engineer_scenario"
    assert result["action"] == "go_home"
    assert result["source_of_truth"] == "manifest_home_scenario"
    assert result["scenario_resolution"] == "manifest_home"


def test_webspace_service_set_home_scenario_updates_manifest(monkeypatch) -> None:
    webspace_id = "phase2-set-home-service"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Service Home",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    info = asyncio.run(webspace_runtime_module.WebspaceService().set_home_scenario(webspace_id, "prompt_engineer_scenario"))

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.home_scenario == "prompt_engineer_scenario"
    assert info is not None
    assert info.home_scenario == "prompt_engineer_scenario"


def test_webspace_service_update_metadata_updates_title_and_home(monkeypatch) -> None:
    webspace_id = "phase2-update-metadata"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Workspace Before",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    info = asyncio.run(
        webspace_runtime_module.WebspaceService().update_metadata(
            webspace_id,
            title="Workspace After",
            home_scenario="prompt_engineer_scenario",
        )
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.title == "Workspace After"
    assert row.home_scenario == "prompt_engineer_scenario"
    assert info is not None
    assert info.title == "Workspace After"
    assert info.home_scenario == "prompt_engineer_scenario"


def test_ensure_dev_webspace_for_scenario_reuses_existing_dev_space() -> None:
    webspace_id = "phase2-dev-existing"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt IDE",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    result = asyncio.run(webspace_runtime_module.ensure_dev_webspace_for_scenario("prompt_engineer_scenario"))

    assert result["ok"] is True
    assert result["created"] is False
    assert result["webspace_id"] == webspace_id
    assert result["home_scenario"] == "prompt_engineer_scenario"


def test_ensure_dev_webspace_for_scenario_creates_missing_dev_space(monkeypatch) -> None:
    async def _fake_seed(webspace_id: str, scenario_id: str, *, dev=None) -> None:  # noqa: ARG001
        return None

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    result = asyncio.run(webspace_runtime_module.ensure_dev_webspace_for_scenario("phase2_fresh_scenario"))

    row = get_workspace(str(result["webspace_id"]))
    assert row is not None
    assert row.is_dev is True
    assert row.home_scenario == "phase2_fresh_scenario"
    assert result["created"] is True
    assert result["kind"] == "dev"
    assert result["source_mode"] == "dev"


def test_desktop_scenario_set_forwards_set_home_flag(monkeypatch) -> None:
    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(webspace_id: str, scenario_id: str, *, set_home: bool = False) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    asyncio.run(
        webspace_runtime_module._on_desktop_scenario_set(
            {"webspace_id": "phase2-forward", "scenario_id": "prompt_engineer_scenario", "set_home": True}
        )
    )

    assert captured == [("phase2-forward", "prompt_engineer_scenario", True)]


def test_desktop_scenario_set_preserves_explicit_false(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None]] = []

    async def _fake_switch(webspace_id: str, scenario_id: str, *, set_home: bool | None = None) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "switch_webspace_scenario", _fake_switch)

    asyncio.run(
        webspace_runtime_module._on_desktop_scenario_set(
            {"webspace_id": "phase2-forward", "scenario_id": "prompt_engineer_scenario", "set_home": False}
        )
    )

    assert captured == [("phase2-forward", "prompt_engineer_scenario", False)]


def test_reload_preview_webspaces_for_scenario_project(monkeypatch) -> None:
    scenario_id = "prompt_engineer_scenario"
    preview_a = "dev-prompt-a"
    preview_b = "dev-prompt-b"
    ensure_workspace(preview_a)
    ensure_workspace(preview_b)
    set_workspace_manifest(
        preview_a,
        display_name="DEV: Prompt A",
        kind="dev",
        source_mode="dev",
        home_scenario=scenario_id,
    )
    set_workspace_manifest(
        preview_b,
        display_name="DEV: Prompt B",
        kind="dev",
        source_mode="dev",
        home_scenario="other_scenario",
    )

    captured: list[tuple[str, str, str]] = []

    async def _fake_reload(webspace_id: str, *, scenario_id: str | None = None, action: str = "reload") -> dict[str, object]:
        captured.append((webspace_id, str(scenario_id), action))
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "action": action}

    monkeypatch.setattr(webspace_runtime_module, "reload_webspace_from_scenario", _fake_reload)

    result = asyncio.run(
        webspace_runtime_module.reload_preview_webspaces_for_project(
            "scenario",
            scenario_id,
            reason="project_meta_updated",
        )
    )

    assert captured == [(preview_a, scenario_id, "reload")]
    assert result["accepted"] is True
    assert result["reloaded_webspaces"] == [preview_a]


def test_reload_preview_webspaces_for_skill_dependency(monkeypatch) -> None:
    preview = "dev-scenario-preview"
    ensure_workspace(preview)
    set_workspace_manifest(
        preview,
        display_name="DEV: Scenario Preview",
        kind="dev",
        source_mode="dev",
        home_scenario="demo_scenario",
    )

    async def _fake_reload(webspace_id: str, *, scenario_id: str | None = None, action: str = "reload") -> dict[str, object]:
        return {"ok": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "action": action}

    monkeypatch.setattr(webspace_runtime_module, "reload_webspace_from_scenario", _fake_reload)
    monkeypatch.setattr(
        webspace_runtime_module.scenarios_loader,
        "read_manifest",
        lambda scenario_id, *, space="workspace": {"depends": ["weather_skill", "skill_alpha"]},
    )

    result = asyncio.run(
        webspace_runtime_module.reload_preview_webspaces_for_project(
            "skill",
            "skill_alpha",
            reason="git_updated",
        )
    )

    assert result["accepted"] is True
    assert result["reloaded_webspaces"] == [preview]


def test_scenarios_synced_routes_through_semantic_rebuild_helper(monkeypatch) -> None:
    captured: list[tuple[str, str | None, str, str]] = []

    async def _fake_rebuild(
        webspace_id: str,
        *,
        action: str = "rebuild",
        scenario_id: str | None = None,
        scenario_resolution: str | None = None,
        source_of_truth: str = "current_runtime",
        reseed_from_scenario: bool = False,
        event_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert reseed_from_scenario is False
        assert event_payload is None
        captured.append((webspace_id, scenario_id, action, source_of_truth))
        return {"ok": True}

    monkeypatch.setattr(webspace_runtime_module, "rebuild_webspace_from_sources", _fake_rebuild)

    asyncio.run(
        webspace_runtime_module._on_scenarios_synced(
            {"webspace_id": "phase3-bootstrap", "scenario_id": "web_desktop"}
        )
    )

    assert captured == [("phase3-bootstrap", "web_desktop", "scenario_projection_sync", "scenario_projection")]


def test_phase4_collect_resolver_inputs_does_not_refresh_projection_registry(monkeypatch) -> None:
    projection_calls: list[str] = []

    class _Projections:
        def load_from_scenario(self, scenario_id: str) -> int:
            projection_calls.append(scenario_id)
            return 1

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(SimpleNamespace(projections=_Projections()))
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap({"current_scenario": "web_desktop", "scenarios": {"web_desktop": {"application": {}}}}),
            "data": _FakeMap({"scenarios": {"web_desktop": {"catalog": {}}}}),
            "registry": _FakeMap({"scenarios": {"web_desktop": {}}}),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, "phase4-collect")

    assert inputs.scenario_id == "web_desktop"
    assert projection_calls == []


def test_phase5_collect_resolver_inputs_prefers_persistent_overlay(monkeypatch) -> None:
    webspace_id = "phase5-overlay-collect"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Overlay Collect",
        kind="workspace",
        source_mode="workspace",
        home_scenario="web_desktop",
    )
    set_workspace_installed_overlay(
        webspace_id,
        {"apps": ["overlay-app"], "widgets": ["overlay-widget"]},
    )
    set_workspace_pinned_widgets_overlay(
        webspace_id,
        [{"id": "infra-status", "type": "visual.metricTile"}],
    )
    set_workspace_topbar_overlay(
        webspace_id,
        [{"id": "home", "label": "Home"}],
    )
    set_workspace_page_schema_overlay(
        webspace_id,
        {"id": "desktop", "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]}, "widgets": []},
    )

    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    monkeypatch.setattr(runtime, "_collect_skill_decls", lambda mode="mixed": [])
    monkeypatch.setattr(runtime, "_list_desktop_scenarios", lambda space="mixed": [])

    fake_doc = _FakeDoc(
        {
            "ui": _FakeMap({"current_scenario": "web_desktop", "scenarios": {"web_desktop": {"application": {}}}}),
            "data": _FakeMap(
                {
                    "installed": {"apps": ["ydoc-app"], "widgets": ["ydoc-widget"]},
                    "scenarios": {"web_desktop": {"catalog": {}}},
                }
            ),
            "registry": _FakeMap({"scenarios": {"web_desktop": {}}}),
        }
    )

    inputs = runtime._collect_resolver_inputs_in_doc(fake_doc, webspace_id)

    assert inputs.overlay_snapshot["installed"] == {
        "apps": ["overlay-app"],
        "widgets": ["overlay-widget"],
    }
    assert inputs.overlay_snapshot["pinnedWidgets"] == [
        {"id": "infra-status", "type": "visual.metricTile"}
    ]
    assert inputs.overlay_snapshot["topbar"] == [{"id": "home", "label": "Home"}]
    assert inputs.overlay_snapshot["pageSchema"]["id"] == "desktop"
    assert inputs.overlay_snapshot["source"] == "workspace_manifest_overlay"


def test_phase5_resolver_prefers_pinned_widgets_from_overlay_over_scenario_defaults() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase5-pinned-overlay",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={
                "desktop": {
                    "topbar": [],
                    "pinnedWidgets": [{"id": "scenario-pin", "type": "visual.metricTile"}],
                }
            },
            scenario_catalog={"apps": [], "widgets": [{"id": "overlay-pin", "type": "visual.metricTile"}]},
            scenario_registry={},
            overlay_snapshot={
                "installed": {"apps": [], "widgets": []},
                "pinnedWidgets": [{"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}],
            },
            live_state={"desktop": {}, "routing": {}},
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    assert resolved.application["desktop"]["pinnedWidgets"] == [
        {"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}
    ]
    assert resolved.desktop["pinnedWidgets"] == [
        {"id": "overlay-pin", "type": "visual.metricTile", "title": "Overlay Pin"}
    ]


def test_phase5_resolver_prefers_page_schema_and_topbar_from_overlay() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase5-layout-overlay",
            scenario_id="web_desktop",
            source_mode="workspace",
            scenario_application={
                "desktop": {
                    "topbar": [{"id": "scenario-home", "label": "Home"}],
                    "pageSchema": {
                        "id": "desktop",
                        "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                        "widgets": [{"id": "scenario-widget", "type": "desktop.widgets", "area": "main"}],
                    },
                }
            },
            scenario_catalog={"apps": [], "widgets": []},
            scenario_registry={},
            overlay_snapshot={
                "installed": {"apps": [], "widgets": []},
                "topbar": [{"id": "overlay-home", "label": "Overlay Home"}],
                "pageSchema": {
                    "id": "desktop-custom",
                    "layout": {"type": "single", "areas": [{"id": "main", "role": "main"}]},
                    "widgets": [{"id": "overlay-widget", "type": "desktop.widgets", "area": "main"}],
                },
            },
            live_state={"desktop": {}, "routing": {}},
            skill_decls=[],
            desktop_scenarios=[],
        )
    )

    assert resolved.application["desktop"]["topbar"] == [{"id": "overlay-home", "label": "Overlay Home"}]
    assert resolved.application["desktop"]["pageSchema"]["id"] == "desktop-custom"
    assert resolved.desktop["topbar"] == [{"id": "overlay-home", "label": "Overlay Home"}]
    assert resolved.desktop["pageSchema"]["widgets"][0]["id"] == "overlay-widget"


def test_phase4_semantic_rebuild_refreshes_projection_rules_before_runtime_rebuild(monkeypatch) -> None:
    order: list[str] = []

    async def _fake_refresh(ctx, webspace_id: str, *, scenario_id: str | None = None) -> dict[str, object]:  # noqa: ARG001
        order.append("refresh")
        return {"attempted": True, "scenario_id": scenario_id, "space": "workspace", "rules_loaded": 1}

    async def _fake_rebuild(self, webspace_id: str):
        order.append("rebuild")
        return SimpleNamespace(scenario_id="web_desktop", apps=[], widgets=[])

    monkeypatch.setattr(webspace_runtime_module, "_refresh_projection_rules_for_rebuild", _fake_refresh)
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: get_ctx())
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)

    result = asyncio.run(
        webspace_runtime_module.rebuild_webspace_from_sources(
            "phase4-ordered-rebuild",
            action="rebuild",
            scenario_id="web_desktop",
            source_of_truth="scenario_projection",
        )
    )

    assert order == ["refresh", "rebuild"]
    assert result["accepted"] is True
    assert result["projection_refresh"]["rules_loaded"] == 1


def test_phase4_projection_refresh_uses_dev_space_for_dev_webspace(monkeypatch) -> None:
    webspace_id = "phase4-dev-refresh"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    fake_state = {
        "ui": _FakeMap({"current_scenario": "prompt_engineer_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    captured: list[tuple[str, str]] = []

    class _Projections:
        def load_from_scenario(self, scenario_id: str, *, space: str = "workspace") -> int:
            captured.append((scenario_id, space))
            return 2

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))

    result = asyncio.run(
        webspace_runtime_module._refresh_projection_rules_for_rebuild(
            SimpleNamespace(projections=_Projections()),
            webspace_id,
        )
    )

    assert captured == [("prompt_engineer_scenario", "dev")]
    assert result["space"] == "dev"
    assert result["rules_loaded"] == 2


def test_phase3_resolver_outputs_are_explicit_and_reusable() -> None:
    runtime = webspace_runtime_module.WebspaceScenarioRuntime(get_ctx())
    resolved = runtime.resolve_webspace(
        webspace_runtime_module.WebspaceResolverInputs(
            webspace_id="phase3-explicit-resolver",
            scenario_id="prompt_engineer_scenario",
            source_mode="dev",
            scenario_application={"id": "prompt-root", "modals": {"scenario_modal": {"title": "Scenario"}}},
            scenario_catalog={
                "apps": [{"id": "scenario-app", "title": "Scenario App"}],
                "widgets": [{"id": "scenario-widget", "title": "Scenario Widget"}],
            },
            scenario_registry={"modals": ["scenario_modal"], "widgets": ["scenario_widget"]},
            overlay_snapshot={"installed": {"apps": ["scenario-app"], "widgets": []}},
            live_state={"desktop": {"installed": {}}, "routing": {}},
            skill_decls=[
                {
                    "skill": "prompt_skill",
                    "space": "dev",
                    "apps": [{"id": "skill-app", "title": "Skill App"}],
                    "widgets": [{"id": "skill-widget", "title": "Skill Widget"}],
                    "registry": {
                        "modals": {"skill_modal": {"title": "Skill Modal"}},
                        "widgets": ["skill_widget"],
                    },
                    "contributions": [
                        {
                            "extensionPoint": "desktop.apps",
                            "type": "app",
                            "id": "skill-app",
                            "autoInstall": True,
                        }
                    ],
                    "ydoc_defaults": {"data/prompt": {"status": "idle"}},
                }
            ],
            desktop_scenarios=[("other_scenario", "Other Scenario")],
        )
    )

    assert resolved.scenario_id == "prompt_engineer_scenario"
    assert [item["id"] for item in resolved.catalog["apps"]] == [
        "scenario-app",
        "scenario:other_scenario",
        "skill-app",
    ]
    assert [item["id"] for item in resolved.catalog["widgets"]] == ["scenario-widget", "skill-widget"]
    assert resolved.registry["modals"] == [
        "scenario_modal",
        "skill_modal",
        "apps_catalog",
        "widgets_catalog",
        "scenario_switcher",
    ]
    assert resolved.registry["widgets"] == ["scenario_widget", "skill_widget"]
    assert resolved.installed["apps"] == ["scenario-app", "scenario:other_scenario", "skill-app"]
    assert resolved.application["modals"]["scenario_modal"]["title"] == "Scenario"
    assert resolved.application["modals"]["skill_modal"]["title"] == "Skill Modal"
    assert resolved.desktop["installed"]["apps"] == ["scenario-app", "scenario:other_scenario", "skill-app"]
    assert resolved.routing["routes"] == {}


def test_restore_webspace_from_snapshot_reconciles_runtime(monkeypatch) -> None:
    fake_state = {
        "ui": _FakeMap({"current_scenario": "restored_prompt_scenario"}),
        "registry": _FakeMap(),
        "data": _FakeMap(),
    }
    rebuilds: list[str] = []
    workflows: list[tuple[str, str]] = []
    emitted: list[tuple[str, dict[str, object], str]] = []

    class _Bus:
        def publish(self, _event) -> None:
            return None

    fake_ctx = SimpleNamespace(bus=_Bus())

    async def _fake_rebuild(self, webspace_id: str):
        rebuilds.append(webspace_id)
        return SimpleNamespace(scenario_id="restored_prompt_scenario", apps=[{"id": "app-1"}], widgets=[])

    async def _fake_workflow_sync(self, scenario_id: str, webspace_id: str):
        workflows.append((scenario_id, webspace_id))
        return None

    async def _fake_restore_ystore(_webspace_id: str) -> dict[str, object]:
        return {"ok": True, "accepted": True, "snapshot_path": "state/ystores/default.snapshot"}

    async def _fake_reset_live_room(_webspace_id: str, close_reason: str = "webspace_restore") -> dict[str, object]:
        return {"accepted": True, "close_reason": close_reason}

    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module.ScenarioWorkflowRuntime, "sync_workflow_for_webspace", _fake_workflow_sync)
    monkeypatch.setattr(
        webspace_runtime_module,
        "emit",
        lambda bus, topic, payload, source: emitted.append((topic, dict(payload), source)),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway",
        types.SimpleNamespace(reset_live_webspace_room=_fake_reset_live_room),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(restore_ystore_for_webspace=_fake_restore_ystore),
    )

    result = asyncio.run(webspace_runtime_module.restore_webspace_from_snapshot("phase3-restore"))

    assert rebuilds == ["phase3-restore"]
    assert workflows == [("restored_prompt_scenario", "phase3-restore")]
    assert emitted == [
        (
            "desktop.webspace.restored",
            {
                "webspace_id": "phase3-restore",
                "action": "restore",
                "scenario_id": "restored_prompt_scenario",
                "snapshot_path": "state/ystores/default.snapshot",
            },
            "scenario.webspace_runtime",
        )
    ]
    assert result["accepted"] is True
    assert result["scenario_id"] == "restored_prompt_scenario"
    assert result["source_of_truth"] == "snapshot"
