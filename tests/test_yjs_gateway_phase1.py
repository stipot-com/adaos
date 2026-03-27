from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=object,
        apply_update=lambda *args, **kwargs: None,
        encode_state_as_update=lambda *args, **kwargs: b"",
        encode_state_vector=lambda *args, **kwargs: b"",
    )

existing_ypy_websocket = sys.modules.get("ypy_websocket")
if existing_ypy_websocket is None or not hasattr(existing_ypy_websocket, "__path__"):
    ystore_mod = types.ModuleType("ypy_websocket.ystore")
    ystore_mod.BaseYStore = object
    ystore_mod.YDocNotFound = RuntimeError

    class _StubStarted:
        async def wait(self) -> None:
            return None

    class _StubWebsocketServer:
        def __init__(self, *args, **kwargs) -> None:
            self.rooms = {}
            self.rooms_ready = SimpleNamespace()
            self.log = SimpleNamespace()
            self.started = _StubStarted()

        async def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        async def start_room(self, room) -> None:  # noqa: ARG002
            return None

        async def serve(self, adapter) -> None:  # noqa: ARG002
            return None

    class _StubMap(dict):
        pass

    class _StubYDoc:
        def get_map(self, name: str) -> _StubMap:  # noqa: ARG002
            return _StubMap()

    class _StubYRoom:
        def __init__(self, *, ready=None, ystore=None, log=None) -> None:
            self.ready = ready
            self.ystore = ystore
            self.log = log
            self.ydoc = _StubYDoc()

        async def stop(self) -> None:
            return None

    ypy_websocket_mod = types.ModuleType("ypy_websocket")
    ypy_websocket_mod.__path__ = []  # type: ignore[attr-defined]
    ypy_websocket_mod.ystore = ystore_mod

    websocket_mod = types.ModuleType("ypy_websocket.websocket")
    websocket_mod.Websocket = object

    websocket_server_mod = types.ModuleType("ypy_websocket.websocket_server")
    websocket_server_mod.WebsocketServer = _StubWebsocketServer

    yroom_mod = types.ModuleType("ypy_websocket.yroom")
    yroom_mod.YRoom = _StubYRoom

    sys.modules["ypy_websocket"] = ypy_websocket_mod
    sys.modules["ypy_websocket.ystore"] = ystore_mod
    sys.modules["ypy_websocket.websocket"] = websocket_mod
    sys.modules["ypy_websocket.websocket_server"] = websocket_server_mod
    sys.modules["ypy_websocket.yroom"] = yroom_mod

from adaos.services.workspaces import ensure_workspace, set_workspace_manifest
from adaos.services.yjs import gateway_ws as gateway_module


class _FakeYStore:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.apply_updates_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    async def apply_updates(self, ydoc) -> None:  # noqa: ARG002
        self.apply_updates_calls += 1


def test_ensure_webspace_ready_uses_manifest_defaults(monkeypatch) -> None:
    webspace_id = "gateway-home"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Gateway Home",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str) -> None:
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
            }
        )

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)

    asyncio.run(gateway_module.ensure_webspace_ready(webspace_id))

    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "prompt_engineer_scenario",
            "space": "dev",
        }
    ]
    assert fake_store.stop_calls == 1


def test_ensure_webspace_ready_explicit_scenario_overrides_manifest_home(monkeypatch) -> None:
    webspace_id = "gateway-explicit"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="Explicit Space",
        kind="workspace",
        source_mode="workspace",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str) -> None:
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
            }
        )

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)

    asyncio.run(gateway_module.ensure_webspace_ready(webspace_id, scenario_id="custom_scenario"))

    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "custom_scenario",
            "space": "workspace",
        }
    ]


def test_get_room_uses_manifest_defaults_for_room_seed(monkeypatch) -> None:
    webspace_id = "gateway-room"
    ensure_workspace(webspace_id)
    set_workspace_manifest(
        webspace_id,
        display_name="DEV: Room Space",
        kind="dev",
        source_mode="dev",
        home_scenario="prompt_engineer_scenario",
    )

    captured: list[dict[str, object]] = []
    fake_store = _FakeYStore()

    async def _fake_seed(ystore, *, webspace_id: str, default_scenario_id: str, space: str) -> None:
        captured.append(
            {
                "ystore": ystore,
                "webspace_id": webspace_id,
                "default_scenario_id": default_scenario_id,
                "space": space,
            }
        )

    class _Scheduler:
        async def ensure_every(self, **kwargs) -> None:  # noqa: ARG002
            return None

    monkeypatch.setattr(gateway_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(gateway_module, "ensure_webspace_seeded_from_scenario", _fake_seed)
    monkeypatch.setattr(gateway_module, "get_scheduler", lambda: _Scheduler())
    monkeypatch.setattr(gateway_module, "attach_room_observers", lambda _webspace_id, _ydoc: None)

    server = gateway_module.WorkspaceWebsocketServer(auto_clean_rooms=False)
    room = asyncio.run(server.get_room(webspace_id))

    assert room is server.rooms[webspace_id]
    assert fake_store.apply_updates_calls == 1
    assert captured == [
        {
            "ystore": fake_store,
            "webspace_id": webspace_id,
            "default_scenario_id": "prompt_engineer_scenario",
            "space": "dev",
        }
    ]


def test_process_events_command_publishes_go_home(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.go_home",
            cmd_id="cmd-1",
            payload={"webspace_id": "default"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == [("desktop.webspace.go_home", {"webspace_id": "default"})]
    assert responses[-1]["ok"] is True


def test_process_events_command_requires_scenario_id_for_set_home(monkeypatch) -> None:
    published: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = []

    monkeypatch.setattr(gateway_module, "_make_publish_bus", lambda *args, **kwargs: (lambda topic, extra=None: published.append((topic, extra))))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.set_home",
            cmd_id="cmd-2",
            payload={"webspace_id": "default"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert published == []
    assert responses[-1]["ok"] is False
    assert responses[-1]["error"] == "scenario_id required"


def test_process_events_command_ensure_dev_returns_webspace_id(monkeypatch) -> None:
    from adaos.services.scenario import webspace_runtime as webspace_runtime_module

    responses: list[dict[str, object]] = []
    ensured: list[tuple[str, str]] = []

    async def _fake_ensure_dev(
        scenario_id: str,
        *,
        requested_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, object]:
        assert requested_id is None
        assert title == "Prompt IDE"
        return {
            "ok": True,
            "accepted": True,
            "created": True,
            "webspace_id": "dev-prompt-engineer-scenario",
            "scenario_id": scenario_id,
            "home_scenario": scenario_id,
            "kind": "dev",
            "source_mode": "dev",
        }

    async def _fake_ready(webspace_id: str, scenario_id: str | None = None) -> None:
        ensured.append((webspace_id, str(scenario_id or "")))

    async def _send_response(msg: dict[str, object]) -> None:
        responses.append(msg)

    monkeypatch.setattr(webspace_runtime_module, "ensure_dev_webspace_for_scenario", _fake_ensure_dev)
    monkeypatch.setattr(gateway_module, "ensure_webspace_ready", _fake_ready)

    asyncio.run(
        gateway_module.process_events_command(
            kind="desktop.webspace.ensure_dev",
            cmd_id="cmd-3",
            payload={"scenario_id": "prompt_engineer_scenario", "title": "Prompt IDE"},
            device_id="dev-1",
            webspace_id="default",
            send_response=_send_response,
        )
    )

    assert ensured == [("dev-prompt-engineer-scenario", "prompt_engineer_scenario")]
    assert responses[-1]["ok"] is True
    assert responses[-1]["data"] == {
        "ok": True,
        "accepted": True,
        "created": True,
        "webspace_id": "dev-prompt-engineer-scenario",
        "scenario_id": "prompt_engineer_scenario",
        "home_scenario": "prompt_engineer_scenario",
        "kind": "dev",
        "source_mode": "dev",
    }
