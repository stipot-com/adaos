from __future__ import annotations

import asyncio
from types import SimpleNamespace

import y_py as Y

from adaos.services.yjs import bootstrap as bootstrap_module
from adaos.services.yjs.webspace import default_webspace_id


class _FakeStore:
    def __init__(self, apply_state=None, *, incremental_write_ok: bool = True) -> None:
        self._apply_state = apply_state
        self._incremental_write_ok = incremental_write_ok
        self.start_calls = 0
        self.apply_updates_calls = 0
        self.encode_calls = 0
        self.write_calls = 0
        self.write_kinds: list[str] = []
        self.write_notify: list[bool] = []
        self.encoded_state: dict[str, object] | None = None

    async def start(self) -> None:
        self.start_calls += 1

    async def apply_updates(self, ydoc: Y.YDoc) -> None:
        self.apply_updates_calls += 1
        if callable(self._apply_state):
            self._apply_state(ydoc)

    def _capture_state(self, ydoc: Y.YDoc) -> dict[str, object]:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")
        return {
            "current_scenario": ui_map.get("current_scenario"),
            "ui_application": ui_map.get("application"),
            "ui_scenarios": ui_map.get("scenarios"),
            "data_catalog": data_map.get("catalog"),
            "data_installed": data_map.get("installed"),
            "data_scenarios": data_map.get("scenarios"),
            "registry_merged": registry_map.get("merged"),
            "registry_scenarios": registry_map.get("scenarios"),
        }

    async def write_update(self, update: bytes, *, update_kind: str = "raw", notify: bool = True) -> bool:
        self.write_calls += 1
        self.write_kinds.append(update_kind)
        self.write_notify.append(bool(notify))
        if not self._incremental_write_ok:
            raise RuntimeError("incremental write unavailable")
        ydoc = Y.YDoc()
        if callable(self._apply_state):
            self._apply_state(ydoc)
        if update:
            Y.apply_update(ydoc, update)
        self.encoded_state = self._capture_state(ydoc)
        return True

    async def encode_state_as_update(self, ydoc: Y.YDoc) -> None:
        self.encode_calls += 1
        self.encoded_state = self._capture_state(ydoc)


def test_bootstrap_seed_fallback_projects_compat_seed_without_effective_writes(monkeypatch) -> None:
    class _FailingManager:
        async def sync_to_yjs_async(self, *args, **kwargs) -> None:  # noqa: ARG002
            raise FileNotFoundError("missing scenario payload")

    emitted: list[tuple[str, dict[str, object], str]] = []
    store = _FakeStore()

    monkeypatch.setattr(bootstrap_module, "_scenario_manager", lambda: _FailingManager())
    monkeypatch.setattr(bootstrap_module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(
        bootstrap_module,
        "emit",
        lambda bus, type_, payload, source: emitted.append((type_, dict(payload), source)),  # noqa: ARG005
    )

    asyncio.run(
        bootstrap_module.ensure_webspace_seeded_from_scenario(
            store,
            webspace_id=default_webspace_id(),
            default_scenario_id="web_desktop",
        )
    )

    assert store.start_calls == 1
    assert store.apply_updates_calls == 1
    assert store.write_calls == 1
    assert store.write_kinds == ["diff"]
    assert store.write_notify == [False]
    assert store.encode_calls == 0
    assert store.encoded_state is not None
    assert store.encoded_state["current_scenario"] == "web_desktop"
    assert store.encoded_state["ui_application"] is None
    assert store.encoded_state["data_catalog"] is None
    assert store.encoded_state["data_installed"] is None
    assert store.encoded_state["registry_merged"] is None
    ui_scenarios = dict(store.encoded_state["ui_scenarios"] or {})
    data_scenarios = dict(store.encoded_state["data_scenarios"] or {})
    registry_scenarios = dict(store.encoded_state["registry_scenarios"] or {})
    assert ui_scenarios["web_desktop"]["application"]["desktop"]["pageSchema"]["id"] == "desktop"
    assert data_scenarios["web_desktop"]["catalog"]["apps"] == []
    assert registry_scenarios["web_desktop"] == {"widgets": [], "modals": []}
    assert emitted == [
        (
            "scenarios.synced",
            {"scenario_id": "web_desktop", "webspace_id": default_webspace_id()},
            "yjs.bootstrap",
        )
    ]


def test_bootstrap_reuses_projected_seed_and_only_nudges_rebuild(monkeypatch) -> None:
    def _apply_state(ydoc: Y.YDoc) -> None:
        with ydoc.begin_transaction() as txn:
            ui_map = ydoc.get_map("ui")
            data_map = ydoc.get_map("data")
            registry_map = ydoc.get_map("registry")
            ui_map.set(txn, "current_scenario", "prompt_engineer_scenario")
            ui_map.set(
                txn,
                "scenarios",
                {
                    "prompt_engineer_scenario": {
                        "application": {"desktop": {"pageSchema": {"id": "prompt-page"}}}
                    }
                },
            )
            data_map.set(
                txn,
                "scenarios",
                {"prompt_engineer_scenario": {"catalog": {"apps": [{"id": "prompt-app"}], "widgets": []}}},
            )
            registry_map.set(
                txn,
                "scenarios",
                {"prompt_engineer_scenario": {"modals": ["prompt-modal"], "widgets": []}},
            )

    class _UnexpectedManager:
        async def sync_to_yjs_async(self, *args, **kwargs) -> None:  # noqa: ARG002
            raise AssertionError("should not project scenario again when projected seed already exists")

    emitted: list[tuple[str, dict[str, object], str]] = []
    store = _FakeStore(apply_state=_apply_state)

    monkeypatch.setattr(bootstrap_module, "_scenario_manager", lambda: _UnexpectedManager())
    monkeypatch.setattr(bootstrap_module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(
        bootstrap_module,
        "emit",
        lambda bus, type_, payload, source: emitted.append((type_, dict(payload), source)),  # noqa: ARG005
    )

    asyncio.run(
        bootstrap_module.ensure_webspace_seeded_from_scenario(
            store,
            webspace_id=default_webspace_id(),
            default_scenario_id="web_desktop",
        )
    )

    assert store.start_calls == 1
    assert store.apply_updates_calls == 1
    assert store.write_calls == 0
    assert store.encode_calls == 0
    assert emitted == [
        (
            "scenarios.synced",
            {"scenario_id": "prompt_engineer_scenario", "webspace_id": default_webspace_id()},
            "yjs.bootstrap",
        )
    ]


def test_bootstrap_prefers_current_pointer_when_projecting_missing_effective_ui(monkeypatch) -> None:
    def _apply_state(ydoc: Y.YDoc) -> None:
        with ydoc.begin_transaction() as txn:
            ydoc.get_map("ui").set(txn, "current_scenario", "prompt_engineer_scenario")

    captured: list[tuple[str, str, str, bool]] = []

    class _Manager:
        async def sync_to_yjs_async(
            self,
            scenario_id: str,
            webspace_id: str | None = None,
            *,
            space: str = "workspace",
            emit_event: bool = True,
        ) -> None:
            captured.append((scenario_id, str(webspace_id or ""), space, emit_event))

    store = _FakeStore(apply_state=_apply_state)
    monkeypatch.setattr(bootstrap_module, "_scenario_manager", lambda: _Manager())

    asyncio.run(
        bootstrap_module.ensure_webspace_seeded_from_scenario(
            store,
            webspace_id=default_webspace_id(),
            default_scenario_id="web_desktop",
            space="dev",
        )
    )

    assert store.start_calls == 1
    assert store.apply_updates_calls == 1
    assert store.write_calls == 0
    assert captured == [("prompt_engineer_scenario", default_webspace_id(), "dev", True)]


def test_bootstrap_projects_into_provided_ydoc_in_single_pass(monkeypatch) -> None:
    projected: list[tuple[str, str]] = []

    class _ProjectingManager:
        def project_scenario_to_doc(self, ydoc: Y.YDoc, scenario_id: str, *, space: str = "workspace") -> None:
            projected.append((scenario_id, space))
            with ydoc.begin_transaction() as txn:
                ui_map = ydoc.get_map("ui")
                data_map = ydoc.get_map("data")
                registry_map = ydoc.get_map("registry")
                ui_map.set(txn, "current_scenario", scenario_id)
                ui_map.set(txn, "scenarios", {scenario_id: {"application": {"desktop": {"pageSchema": {"id": "prompt"}}}}})
                data_map.set(txn, "scenarios", {scenario_id: {"catalog": {"apps": [{"id": "prompt"}], "widgets": []}}})
                registry_map.set(txn, "scenarios", {scenario_id: {"widgets": [], "modals": ["prompt-modal"]}})

    emitted: list[tuple[str, dict[str, object], str]] = []
    store = _FakeStore()
    provided_doc = Y.YDoc()

    monkeypatch.setattr(bootstrap_module, "_scenario_manager", lambda: _ProjectingManager())
    monkeypatch.setattr(bootstrap_module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(
        bootstrap_module,
        "emit",
        lambda bus, type_, payload, source: emitted.append((type_, dict(payload), source)),  # noqa: ARG005
    )

    result = asyncio.run(
        bootstrap_module.ensure_webspace_seeded_from_scenario(
            store,
            webspace_id=default_webspace_id(),
            default_scenario_id="prompt_engineer_scenario",
            space="dev",
            ydoc=provided_doc,
        )
    )

    assert projected == [("prompt_engineer_scenario", "dev")]
    assert store.start_calls == 1
    assert store.apply_updates_calls == 1
    assert store.write_calls == 1
    assert store.encode_calls == 0
    assert result["used_provided_ydoc"] is True
    assert result["mode"] == "scenario_projection"
    assert result["persisted_via"] == "diff"
    assert provided_doc.get_map("ui").get("current_scenario") == "prompt_engineer_scenario"
    assert emitted == [
        (
            "scenarios.synced",
            {"scenario_id": "prompt_engineer_scenario", "webspace_id": default_webspace_id()},
            "yjs.bootstrap",
        )
    ]


def test_bootstrap_seed_fallback_uses_snapshot_when_incremental_write_fails(monkeypatch) -> None:
    class _FailingManager:
        async def sync_to_yjs_async(self, *args, **kwargs) -> None:  # noqa: ARG002
            raise FileNotFoundError("missing scenario payload")

    emitted: list[tuple[str, dict[str, object], str]] = []
    store = _FakeStore(incremental_write_ok=False)

    monkeypatch.setattr(bootstrap_module, "_scenario_manager", lambda: _FailingManager())
    monkeypatch.setattr(bootstrap_module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(
        bootstrap_module,
        "emit",
        lambda bus, type_, payload, source: emitted.append((type_, dict(payload), source)),  # noqa: ARG005
    )

    asyncio.run(
        bootstrap_module.ensure_webspace_seeded_from_scenario(
            store,
            webspace_id=default_webspace_id(),
            default_scenario_id="web_desktop",
        )
    )

    assert store.write_calls == 1
    assert store.encode_calls == 1
    assert store.encoded_state is not None
    assert store.encoded_state["current_scenario"] == "web_desktop"
    assert emitted == [
        (
            "scenarios.synced",
            {"scenario_id": "web_desktop", "webspace_id": default_webspace_id()},
            "yjs.bootstrap",
        )
    ]


def test_bootstrap_seed_if_empty_uses_configured_default_webspace(monkeypatch) -> None:
    captured: list[tuple[object, str]] = []
    fake_store = object()

    async def _fake_ensure(store, *, webspace_id: str, **kwargs) -> None:  # noqa: ANN001
        captured.append((store, webspace_id))

    monkeypatch.setattr(bootstrap_module, "default_webspace_id", lambda: "desktop-main")
    monkeypatch.setattr(bootstrap_module, "get_ystore_for_webspace", lambda webspace_id: (fake_store, webspace_id)[0] if webspace_id == "desktop-main" else None)
    monkeypatch.setattr(bootstrap_module, "ensure_webspace_seeded_from_scenario", _fake_ensure)

    asyncio.run(bootstrap_module.bootstrap_seed_if_empty(fake_store))  # type: ignore[arg-type]

    assert captured == [(fake_store, "desktop-main")]
