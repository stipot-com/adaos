from __future__ import annotations

import asyncio
import sys
import types

y_py_module = sys.modules.get("y_py")
if y_py_module is None:
    y_py_module = types.SimpleNamespace()
    sys.modules["y_py"] = y_py_module
if not hasattr(y_py_module, "YDoc"):
    y_py_module.YDoc = type("YDoc", (), {})
if not hasattr(y_py_module, "YMap"):
    y_py_module.YMap = type("YMap", (), {})
if not hasattr(y_py_module, "YArray"):
    y_py_module.YArray = type("YArray", (), {})
if not hasattr(y_py_module, "encode_state_vector"):
    y_py_module.encode_state_vector = lambda *args, **kwargs: b""
if not hasattr(y_py_module, "encode_state_as_update"):
    y_py_module.encode_state_as_update = lambda *args, **kwargs: b""
if not hasattr(y_py_module, "apply_update"):
    y_py_module.apply_update = lambda *args, **kwargs: None
if "ypy_websocket.ystore" not in sys.modules:
    ystore_module = types.ModuleType("ypy_websocket.ystore")
    ystore_module.BaseYStore = type("BaseYStore", (), {})
    ystore_module.YDocNotFound = type("YDocNotFound", (Exception,), {})
    sys.modules["ypy_websocket.ystore"] = ystore_module
if "ypy_websocket" not in sys.modules:
    pkg = types.ModuleType("ypy_websocket")
    pkg.ystore = sys.modules["ypy_websocket.ystore"]
    sys.modules["ypy_websocket"] = pkg

from adaos.services.subnet import link_manager as mod


class _FakeBus:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


class _FakeCtx:
    def __init__(self, bus) -> None:
        self.bus = bus


class _FakeDirectory:
    def __init__(self) -> None:
        self.calls = []
        self.heartbeats = []

    def on_member_runtime_snapshot(self, node_id: str, snapshot: dict) -> None:
        self.calls.append((node_id, dict(snapshot)))

    def on_member_runtime_snapshot_heartbeat(
        self,
        node_id: str,
        *,
        captured_at: float | None = None,
        node_state: str | None = None,
    ) -> None:
        self.heartbeats.append((node_id, captured_at, node_state))


class _FakeWebSocket:
    async def send_json(self, msg: dict) -> None:
        return None


def test_update_member_snapshot_publishes_only_material_changes(monkeypatch) -> None:
    fake_bus = _FakeBus()
    fake_directory = _FakeDirectory()
    monkeypatch.setattr(mod, "get_ctx", lambda: _FakeCtx(fake_bus))
    monkeypatch.setattr("adaos.services.registry.subnet_directory.get_directory", lambda: fake_directory)

    manager = mod.HubLinkManager()
    manager._links["member-1"] = mod.HubMemberLink(node_id="member-1", websocket=_FakeWebSocket())

    snapshot = {
        "captured_at": 100.0,
        "node_id": "member-1",
        "subnet_id": "sn-1",
        "role": "member",
        "ready": True,
        "node_state": "ready",
        "route_mode": "ws",
        "connected_to_hub": True,
        "capacity": {
            "io": [{"io_type": "webrtc_media"}],
            "skills": [{"name": "voice_chat_skill"}],
            "scenarios": [{"name": "web_desktop"}],
        },
        "build": {"runtime_version": "rev1", "runtime_git_short_commit": "abc1234"},
        "update_status": {"state": "succeeded", "phase": "validate", "action": "update"},
    }

    first = asyncio.run(manager.update_member_snapshot("member-1", snapshot=snapshot))
    second = asyncio.run(
        manager.update_member_snapshot(
            "member-1",
            snapshot={**snapshot, "captured_at": 101.0},
        )
    )
    third = asyncio.run(
        manager.update_member_snapshot(
            "member-1",
            snapshot={
                **snapshot,
                "captured_at": 102.0,
                "update_status": {"state": "applying", "phase": "apply", "action": "update"},
            },
        )
    )

    changed_events = [event for event in fake_bus.events if event.type == "subnet.member.snapshot.changed"]
    assert len(changed_events) == 2
    assert first["changed"] is True
    assert second["changed"] is False
    assert third["changed"] is True
    assert len(fake_directory.calls) == 2
    assert fake_directory.heartbeats == [("member-1", 101.0, "ready")]

    payload = changed_events[0].payload
    assert "snapshot" not in payload
    assert payload["snapshot_capacity"] == {"io_total": 1, "skill_total": 1, "scenario_total": 1}
    assert payload["snapshot_build"]["runtime_git_short_commit"] == "abc1234"
    assert payload["snapshot_update"]["state"] == "succeeded"


def test_update_member_snapshot_ignores_nested_capacity_timestamps(monkeypatch) -> None:
    fake_bus = _FakeBus()
    fake_directory = _FakeDirectory()
    monkeypatch.setattr(mod, "get_ctx", lambda: _FakeCtx(fake_bus))
    monkeypatch.setattr("adaos.services.registry.subnet_directory.get_directory", lambda: fake_directory)

    manager = mod.HubLinkManager()
    manager._links["member-1"] = mod.HubMemberLink(node_id="member-1", websocket=_FakeWebSocket())

    snapshot = {
        "captured_at": 100.0,
        "node_id": "member-1",
        "subnet_id": "sn-1",
        "role": "member",
        "ready": True,
        "node_state": "ready",
        "route_mode": "ws",
        "connected_to_hub": True,
        "capacity": {
            "io": [{"io_type": "webrtc_media", "updated_at": 10.0}],
            "skills": [{"name": "voice_chat_skill", "updated_at": 10.0}],
            "scenarios": [{"name": "web_desktop", "updated_at": 10.0}],
        },
    }

    first = asyncio.run(manager.update_member_snapshot("member-1", snapshot=snapshot))
    second = asyncio.run(
        manager.update_member_snapshot(
            "member-1",
            snapshot={
                **snapshot,
                "captured_at": 101.0,
                "capacity": {
                    "io": [{"io_type": "webrtc_media", "updated_at": 20.0}],
                    "skills": [{"name": "voice_chat_skill", "updated_at": 20.0}],
                    "scenarios": [{"name": "web_desktop", "updated_at": 20.0}],
                },
            },
        )
    )

    changed_events = [event for event in fake_bus.events if event.type == "subnet.member.snapshot.changed"]
    assert first["changed"] is True
    assert second["changed"] is False
    assert len(changed_events) == 1
