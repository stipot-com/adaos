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
from adaos.services.workspaces import ensure_workspace, get_workspace, set_workspace_manifest


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
