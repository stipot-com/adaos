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
from adaos.services.io_web import desktop as desktop_module
from adaos.services.workspaces import index as workspace_index_module
from adaos.services.workspaces import (
    ensure_workspace,
    get_workspace_desktop_overlay,
    get_workspace,
    get_workspace_installed_overlay,
    get_workspace_overlay,
    get_workspace_pinned_widgets_overlay,
    has_workspace_overlay,
    normalize_workspaces,
    set_workspace_installed_overlay,
    set_workspace_pinned_widgets_overlay,
    set_workspace_manifest,
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


class _FakeSyncDoc:
    def __init__(self, state: dict[str, _FakeMap]) -> None:
        self._state = state

    def __enter__(self) -> _FakeDoc:
        return _FakeDoc(self._state)

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _patch_reload_dependencies(
    monkeypatch,
    captured: list[tuple[str, str, bool | None]],
    emitted: list[tuple[str, dict[str, object], str]],
    *,
    current_scenario: str | None = None,
) -> None:
    class _Bus:
        def publish(self, _event) -> None:
            return None

    fake_ctx = SimpleNamespace(bus=_Bus())
    fake_state = {"ui": _FakeMap()}
    if current_scenario:
        fake_state["ui"]["current_scenario"] = current_scenario

    async def _fake_seed(webspace_id: str, scenario_id: str, *, dev: bool | None = None) -> None:
        captured.append((webspace_id, scenario_id, dev))

    async def _fake_sync_listing() -> None:
        return None

    async def _fake_rebuild(self, webspace_id: str):
        return SimpleNamespace(webspace_id=webspace_id)

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)
    monkeypatch.setattr(webspace_runtime_module.WebspaceScenarioRuntime, "rebuild_webspace_async", _fake_rebuild)
    monkeypatch.setattr(webspace_runtime_module, "async_get_ydoc", lambda _webspace_id: _FakeAsyncDoc(fake_state))
    monkeypatch.setattr(webspace_runtime_module, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(
        webspace_runtime_module,
        "emit",
        lambda bus, topic, payload, source: emitted.append((topic, dict(payload), source)),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway",
        types.SimpleNamespace(
            y_server=SimpleNamespace(rooms={}),
            reset_live_webspace_room=lambda _webspace_id, close_reason="webspace_reload": {},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.store",
        types.SimpleNamespace(reset_ystore_for_webspace=lambda _webspace_id: None),
    )


def test_webspace_service_create_persists_manifest(monkeypatch) -> None:
    async def _fake_seed(_webspace_id: str, _scenario_id: str, *, dev: bool | None = None) -> None:
        return None

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    info = asyncio.run(
        webspace_runtime_module.WebspaceService().create(
            "prompt-lab",
            "Prompt Lab",
            scenario_id="prompt_engineer_scenario",
            dev=True,
        )
    )

    row = get_workspace("prompt-lab")
    assert row is not None
    assert row.kind == "dev"
    assert row.home_scenario == "prompt_engineer_scenario"
    assert row.source_mode == "dev"
    assert row.title == "DEV: Prompt Lab"
    assert info.kind == "dev"
    assert info.home_scenario == "prompt_engineer_scenario"
    assert info.source_mode == "dev"
    assert info.is_dev is True


def test_webspace_service_list_filters_by_manifest_kind(monkeypatch) -> None:
    async def _fake_seed(_webspace_id: str, _scenario_id: str, *, dev: bool | None = None) -> None:
        return None

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    service = webspace_runtime_module.WebspaceService()
    asyncio.run(service.create("regular-space", "Regular Space", scenario_id="web_desktop", dev=False))
    asyncio.run(service.create("dev-space", "Prompt Lab", scenario_id="prompt_engineer_scenario", dev=True))

    set_workspace_manifest(
        "dev-space",
        display_name="Prompt Lab",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    workspace_ids = {item.id for item in service.list(mode="workspace")}
    dev_ids = {item.id for item in service.list(mode="dev")}

    assert "regular-space" in workspace_ids
    assert "dev-space" not in workspace_ids
    assert "dev-space" in dev_ids


def test_webspace_listing_exposes_manifest_metadata(monkeypatch) -> None:
    async def _fake_seed(_webspace_id: str, _scenario_id: str, *, dev: bool | None = None) -> None:
        return None

    async def _fake_sync_listing() -> None:
        return None

    monkeypatch.setattr(webspace_runtime_module, "_seed_webspace_from_scenario", _fake_seed)
    monkeypatch.setattr(webspace_runtime_module, "_sync_webspace_listing", _fake_sync_listing)

    service = webspace_runtime_module.WebspaceService()
    asyncio.run(service.create("metadata-space", "Metadata Space", scenario_id="prompt_engineer_scenario", dev=True))

    items = {item["id"]: item for item in webspace_runtime_module._webspace_listing()}
    row = items["metadata-space"]

    assert row["title"] == "DEV: Metadata Space"
    assert row["kind"] == "dev"
    assert row["home_scenario"] == "prompt_engineer_scenario"
    assert row["source_mode"] == "dev"


def test_webspace_reload_defaults_to_manifest_home_scenario(monkeypatch) -> None:
    webspace_id = "ws-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Home Space",
        kind="workspace",
        source_mode="workspace",
        home_scenario="prompt_engineer_scenario",
    )
    captured: list[tuple[str, str, bool | None]] = []
    emitted: list[tuple[str, dict[str, object], str]] = []
    _patch_reload_dependencies(monkeypatch, captured, emitted)

    result = asyncio.run(webspace_runtime_module.reload_webspace_from_scenario(webspace_id))

    assert captured == [(webspace_id, "prompt_engineer_scenario", None)]
    assert result["scenario_id"] == "prompt_engineer_scenario"
    assert result["scenario_resolution"] == "manifest_home"
    assert result["kind"] == "workspace"
    assert result["source_mode"] == "workspace"
    assert result["home_scenario"] == "prompt_engineer_scenario"
    assert emitted[-1][1]["scenario_id"] == "prompt_engineer_scenario"


def test_webspace_reload_falls_back_to_current_scenario_for_legacy_manifest(monkeypatch) -> None:
    webspace_id = "ws-legacy"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Legacy Space",
        kind="workspace",
        source_mode="workspace",
        home_scenario=None,
    )

    captured: list[tuple[str, str, bool | None]] = []
    emitted: list[tuple[str, dict[str, object], str]] = []
    _patch_reload_dependencies(monkeypatch, captured, emitted, current_scenario="legacy_prompt_scenario")

    result = asyncio.run(webspace_runtime_module.reload_webspace_from_scenario(webspace_id))

    assert captured == [(webspace_id, "legacy_prompt_scenario", None)]
    assert result["scenario_id"] == "legacy_prompt_scenario"
    assert result["scenario_resolution"] == "current_scenario"
    assert result["current_scenario_before"] == "legacy_prompt_scenario"
    assert emitted[-1][1]["scenario_id"] == "legacy_prompt_scenario"


def test_get_workspace_backfills_legacy_manifest_defaults() -> None:
    webspace_id = "legacy-dev-manifest"
    ctx = get_ctx()

    with ctx.sql.connect() as con:
        workspace_index_module._ensure_schema(con)
        con.execute("DELETE FROM y_workspaces WHERE workspace_id=?", (webspace_id,))
        con.execute(
            "INSERT INTO y_workspaces(workspace_id, path, created_at, display_name) VALUES(?,?,?,?)",
            (webspace_id, "state/ystores/legacy-dev-manifest.sqlite3", 123456, "DEV: Legacy Dev"),
        )
        con.commit()

    row = get_workspace(webspace_id)

    assert row is not None
    assert row.kind == "dev"
    assert row.source_mode == "dev"
    assert row.home_scenario is None

    with ctx.sql.connect() as con:
        stored = con.execute(
            "SELECT kind, source_mode FROM y_workspaces WHERE workspace_id=?",
            (webspace_id,),
        ).fetchone()

    assert stored == ("dev", "dev")


def test_ensure_workspace_persists_default_home_scenario_for_new_rows() -> None:
    webspace_id = "implicit-home-space"
    row = ensure_workspace(webspace_id)

    assert row.home_scenario == "web_desktop"
    assert row.effective_home_scenario == "web_desktop"

    ctx = get_ctx()
    with ctx.sql.connect() as con:
        stored = con.execute(
            "SELECT home_scenario FROM y_workspaces WHERE workspace_id=?",
            (webspace_id,),
        ).fetchone()

    assert stored == ("web_desktop",)


def test_normalize_workspaces_backfills_manifest_defaults() -> None:
    legacy_id = "legacy-normalize-space"
    ctx = get_ctx()

    with ctx.sql.connect() as con:
        workspace_index_module._ensure_schema(con)
        con.execute("DELETE FROM y_workspaces WHERE workspace_id=?", (legacy_id,))
        con.execute(
            "INSERT INTO y_workspaces(workspace_id, path, created_at, display_name) VALUES(?,?,?,?)",
            (legacy_id, "state/ystores/legacy-normalize-space.sqlite3", 654321, "DEV: Normalize Me"),
        )
        con.commit()

    updated = normalize_workspaces()

    assert updated >= 1
    row = get_workspace(legacy_id)
    assert row is not None
    assert row.kind == "dev"
    assert row.source_mode == "dev"


def test_workspace_desktop_overlay_roundtrip() -> None:
    webspace_id = "phase5-overlay-roundtrip"
    ensure_workspace(webspace_id)

    set_workspace_installed_overlay(
        webspace_id,
        {"apps": ["scenario:prompt_engineer_scenario", "scenario:prompt_engineer_scenario"], "widgets": ["weather"]},
    )
    set_workspace_pinned_widgets_overlay(
        webspace_id,
        [{"id": "infra-status", "type": "visual.metricTile"}, {"id": "infra-status", "type": "visual.metricTile"}],
    )

    row = get_workspace(webspace_id)
    assert row is not None
    assert row.has_ui_overlay is True
    assert has_workspace_overlay(webspace_id) is True
    assert get_workspace_desktop_overlay(webspace_id) == {
        "installed": {
            "apps": ["scenario:prompt_engineer_scenario"],
            "widgets": ["weather"],
        },
        "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
    }
    assert get_workspace_overlay(webspace_id) == {
        "desktop": {
            "installed": {
                "apps": ["scenario:prompt_engineer_scenario"],
                "widgets": ["weather"],
            },
            "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile"}],
        }
    }
    assert get_workspace_installed_overlay(webspace_id) == {
        "apps": ["scenario:prompt_engineer_scenario"],
        "widgets": ["weather"],
    }
    assert get_workspace_pinned_widgets_overlay(webspace_id) == [
        {"id": "infra-status", "type": "visual.metricTile"}
    ]


def test_web_desktop_service_ignores_legacy_yjs_installed_without_overlay(monkeypatch) -> None:
    webspace_id = "phase5-legacy-installed"
    ensure_workspace(webspace_id)
    fake_state = {
        "data": _FakeMap(
            {
                "installed": {
                    "apps": ["legacy-app"],
                    "widgets": ["legacy-widget"],
                }
            }
        )
    }
    monkeypatch.setattr(desktop_module, "get_ydoc", lambda _webspace_id: _FakeSyncDoc(fake_state))

    service = desktop_module.WebDesktopService()
    installed = service.get_installed(webspace_id)

    assert installed.to_dict() == {
        "apps": [],
        "widgets": [],
    }
    assert get_workspace_installed_overlay(webspace_id) == {
        "apps": [],
        "widgets": [],
    }


def test_web_desktop_service_set_pinned_widgets_updates_overlay_and_live_doc(monkeypatch) -> None:
    webspace_id = "phase5-pinned-widgets"
    ensure_workspace(webspace_id)
    fake_state = {
        "ui": _FakeMap({"application": {"desktop": {"topbar": []}}}),
        "data": _FakeMap({"desktop": {}}),
    }
    monkeypatch.setattr(desktop_module, "get_ydoc", lambda _webspace_id: _FakeSyncDoc(fake_state))

    service = desktop_module.WebDesktopService()
    service.set_pinned_widgets(
        [{"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}],
        webspace_id,
    )

    assert get_workspace_pinned_widgets_overlay(webspace_id) == [
        {"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}
    ]
    assert fake_state["ui"]["application"]["desktop"]["pinnedWidgets"] == [
        {"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}
    ]
    assert fake_state["data"]["desktop"]["pinnedWidgets"] == [
        {"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}
    ]


def test_web_desktop_service_get_snapshot_returns_overlay_state() -> None:
    webspace_id = "phase5-desktop-snapshot"
    ensure_workspace(webspace_id)
    set_workspace_installed_overlay(
        webspace_id,
        {"apps": ["scenario:prompt_engineer_scenario"], "widgets": ["weather"]},
    )
    set_workspace_pinned_widgets_overlay(
        webspace_id,
        [{"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}],
    )

    snapshot = desktop_module.WebDesktopService().get_snapshot(webspace_id)

    assert snapshot.to_dict() == {
        "installed": {
            "apps": ["scenario:prompt_engineer_scenario"],
            "widgets": ["weather"],
        },
        "pinnedWidgets": [{"id": "infra-status", "type": "visual.metricTile", "title": "Infra"}],
    }
