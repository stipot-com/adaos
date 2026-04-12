from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=object,
        YMap=type("YMap", (), {}),
        YArray=type("YArray", (), {}),
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

from adaos.domain import Event
from adaos.services.eventbus import LocalEventBus
from adaos.services.router.service import RouterService
import adaos.services.router.service as router_service_module


pytestmark = pytest.mark.anyio


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


async def test_router_projects_media_route_contract_to_yjs(monkeypatch) -> None:
    docs: dict[str, dict[str, _FakeMap]] = {}

    monkeypatch.setattr(
        router_service_module,
        "async_get_ydoc",
        lambda webspace_id: _FakeAsyncDoc(docs.setdefault(webspace_id, {"data": _FakeMap()})),
    )
    monkeypatch.setattr(router_service_module, "load_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(router_service_module, "watch_rules", lambda *args, **kwargs: (lambda: None))

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    bus.publish(
        Event(
            type="io.out.media.route",
            source="test",
            ts=time.time(),
            payload={
                "need": "live_stream",
                "producer_preference": "member",
                "direct_local_ready": False,
                "root_routed_ready": True,
                "hub_webrtc_ready": True,
                "member_browser_direct": {
                    "possible": True,
                    "admitted": False,
                    "reason": "member_browser_direct_policy_not_admitted_yet",
                    "candidate_member_total": 1,
                    "browser_session_total": 2,
                },
                "_meta": {"webspace_id": "alpha"},
            },
        )
    )

    assert await bus.wait_for_idle()

    route = docs["alpha"]["data"]["media"]["route"]
    assert route["route_intent"] == "live_stream"
    assert route["active_route"] == "hub_webrtc_loopback"
    assert route["degradation_reason"] == "member_browser_direct_policy_not_admitted_yet"
    assert route["route_administrator"] == "router"
    assert route["target_webspace_id"] == "alpha"
    assert route["member_browser_direct"]["possible"] is True
    assert route["member_browser_direct"]["admitted"] is False


async def test_router_media_projection_preserves_existing_media_subtree(monkeypatch) -> None:
    docs: dict[str, dict[str, _FakeMap]] = {
        "beta": {
            "data": _FakeMap(
                {
                    "media": {
                        "sessions": {"active": 1},
                    }
                }
            )
        }
    }

    monkeypatch.setattr(
        router_service_module,
        "async_get_ydoc",
        lambda webspace_id: _FakeAsyncDoc(docs.setdefault(webspace_id, {"data": _FakeMap()})),
    )
    monkeypatch.setattr(router_service_module, "load_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(router_service_module, "watch_rules", lambda *args, **kwargs: (lambda: None))

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    bus.publish(
        Event(
            type="io.out.media.route",
            source="test",
            ts=123.0,
            payload={
                "route": {
                    "route_intent": "scenario_response_media",
                    "preferred_route": "member_browser_direct",
                    "active_route": "member_browser_direct",
                    "delivery_topology": "member_browser_direct",
                    "producer_authority": "member",
                    "producer_target": {"kind": "member", "member_id": "member-a"},
                    "selection_reason": "member_browser_direct_ready",
                    "degradation_reason": None,
                    "fallback_chain": ["member_browser_direct", "local_http"],
                    "member_browser_direct": {
                        "possible": True,
                        "admitted": True,
                        "ready": True,
                        "reason": "member_browser_direct_ready",
                        "candidate_member_total": 1,
                        "browser_session_total": 1,
                    },
                    "monitoring": {
                        "watch_signals": ["browser_session_total"],
                        "observed_failure": None,
                    },
                },
                "_meta": {"webspace_id": "beta"},
            },
        )
    )

    assert await bus.wait_for_idle()

    media = docs["beta"]["data"]["media"]
    assert media["sessions"]["active"] == 1
    assert media["route"]["active_route"] == "member_browser_direct"
    assert media["route"]["route_administrator"] == "router"
    assert media["route"]["updated_at"] == 123.0


async def test_router_media_projection_auto_selects_preferred_member_from_capacity(monkeypatch) -> None:
    import adaos.services.media_capability as media_capability

    docs: dict[str, dict[str, _FakeMap]] = {}

    monkeypatch.setattr(
        router_service_module,
        "async_get_ydoc",
        lambda webspace_id: _FakeAsyncDoc(docs.setdefault(webspace_id, {"data": _FakeMap()})),
    )
    monkeypatch.setattr(router_service_module, "load_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(router_service_module, "watch_rules", lambda *args, **kwargs: (lambda: None))
    monkeypatch.setattr(
        media_capability,
        "_directory_nodes",
        lambda: [
            {
                "node_id": "member-auto",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "webrtc_media",
                            "capabilities": [
                                "webrtc:av",
                                "producer:member",
                                "topology:member_browser_direct",
                                "media:live_stream",
                                "state:available",
                            ],
                            "priority": 60,
                        }
                    ]
                },
            }
        ],
    )
    monkeypatch.setattr(media_capability, "_live_member_links", lambda: [])

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    bus.publish(
        Event(
            type="io.out.media.route",
            source="test",
            ts=456.0,
            payload={
                "need": "live_stream",
                "producer_preference": "member",
                "direct_local_ready": False,
                "root_routed_ready": True,
                "hub_webrtc_ready": True,
                "member_browser_direct": {
                    "possible": True,
                    "admitted": True,
                    "browser_session_total": 1,
                },
                "_meta": {"webspace_id": "gamma"},
            },
        )
    )

    assert await bus.wait_for_idle()

    route = docs["gamma"]["data"]["media"]["route"]
    assert route["active_route"] == "member_browser_direct"
    assert route["preferred_member_id"] == "member-auto"
    assert route["producer_target"]["member_id"] == "member-auto"
    assert route["member_browser_direct"]["candidate_members"] == ["member-auto"]


async def test_router_media_projection_refreshes_route_on_browser_session_change(monkeypatch) -> None:
    import adaos.services.media_capability as media_capability

    docs: dict[str, dict[str, _FakeMap]] = {}
    browser_state = {
        "peers": [
            {
                "device_id": "browser-1",
                "webspace_id": "delta",
                "connection_state": "connected",
            }
        ]
    }

    monkeypatch.setattr(
        router_service_module,
        "async_get_ydoc",
        lambda webspace_id: _FakeAsyncDoc(docs.setdefault(webspace_id, {"data": _FakeMap()})),
    )
    monkeypatch.setattr(router_service_module, "load_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(router_service_module, "watch_rules", lambda *args, **kwargs: (lambda: None))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        types.SimpleNamespace(active_browser_session_snapshot=lambda: browser_state),
    )
    monkeypatch.setattr(
        media_capability,
        "_directory_nodes",
        lambda: [
            {
                "node_id": "member-delta",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "webrtc_media",
                            "capabilities": [
                                "webrtc:av",
                                "producer:member",
                                "topology:member_browser_direct",
                                "media:live_stream",
                                "state:available",
                            ],
                            "priority": 60,
                        }
                    ]
                },
            }
        ],
    )
    monkeypatch.setattr(media_capability, "_live_member_links", lambda: [])

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    bus.publish(
        Event(
            type="io.out.media.route",
            source="test",
            ts=500.0,
            payload={
                "need": "live_stream",
                "producer_preference": "member",
                "direct_local_ready": False,
                "root_routed_ready": True,
                "hub_webrtc_ready": True,
                "member_browser_direct": {
                    "admitted": True,
                },
                "_meta": {"webspace_id": "delta"},
            },
        )
    )
    assert await bus.wait_for_idle()

    initial = docs["delta"]["data"]["media"]["route"]
    assert initial["active_route"] == "member_browser_direct"

    browser_state["peers"] = []
    bus.publish(
        Event(
            type="browser.session.changed",
            source="test",
            ts=501.0,
            payload={
                "device_id": "browser-1",
                "webspace_id": "delta",
                "connection_state": "closed",
            },
        )
    )
    assert await bus.wait_for_idle()

    refreshed = docs["delta"]["data"]["media"]["route"]
    assert refreshed["active_route"] == "hub_webrtc_loopback"
    assert refreshed["member_browser_direct"]["browser_session_total"] == 0
    assert refreshed["degradation_reason"] == "member_browser_direct_missing_browser_session"
    assert refreshed["monitoring"]["refresh_cause"] == "browser.session.changed"
    assert refreshed["monitoring"]["observed_failure"] == "browser_session_closed"


async def test_router_media_projection_refreshes_preferred_member_on_member_inventory_change(monkeypatch) -> None:
    import adaos.services.media_capability as media_capability

    docs: dict[str, dict[str, _FakeMap]] = {}
    browser_state = {
        "peers": [
            {
                "device_id": "browser-1",
                "webspace_id": "epsilon",
                "connection_state": "connected",
            }
        ]
    }
    directory_nodes = {
        "items": [
            {
                "node_id": "member-old",
                "roles": ["member"],
                "online": True,
                "node_state": "ready",
                "capacity": {
                    "io": [
                        {
                            "io_type": "webrtc_media",
                            "capabilities": [
                                "webrtc:av",
                                "producer:member",
                                "topology:member_browser_direct",
                                "media:live_stream",
                                "state:available",
                            ],
                            "priority": 50,
                        }
                    ]
                },
            }
        ]
    }

    monkeypatch.setattr(
        router_service_module,
        "async_get_ydoc",
        lambda webspace_id: _FakeAsyncDoc(docs.setdefault(webspace_id, {"data": _FakeMap()})),
    )
    monkeypatch.setattr(router_service_module, "load_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr(router_service_module, "watch_rules", lambda *args, **kwargs: (lambda: None))
    monkeypatch.setitem(
        sys.modules,
        "adaos.services.yjs.gateway_ws",
        types.SimpleNamespace(active_browser_session_snapshot=lambda: browser_state),
    )
    monkeypatch.setattr(media_capability, "_directory_nodes", lambda: list(directory_nodes["items"]))
    monkeypatch.setattr(media_capability, "_live_member_links", lambda: [])

    bus = LocalEventBus()
    router = RouterService(eventbus=bus, base_dir=Path("."))
    await router.start()

    bus.publish(
        Event(
            type="io.out.media.route",
            source="test",
            ts=600.0,
            payload={
                "need": "live_stream",
                "producer_preference": "member",
                "direct_local_ready": False,
                "root_routed_ready": True,
                "hub_webrtc_ready": True,
                "member_browser_direct": {
                    "admitted": True,
                },
                "_meta": {"webspace_id": "epsilon"},
            },
        )
    )
    assert await bus.wait_for_idle()
    assert docs["epsilon"]["data"]["media"]["route"]["preferred_member_id"] == "member-old"

    directory_nodes["items"] = [
        {
            "node_id": "member-new",
            "roles": ["member"],
            "online": True,
            "node_state": "ready",
            "capacity": {
                "io": [
                    {
                        "io_type": "webrtc_media",
                        "capabilities": [
                            "webrtc:av",
                            "producer:member",
                            "topology:member_browser_direct",
                            "media:live_stream",
                            "state:available",
                        ],
                        "priority": 70,
                    }
                ]
            },
        }
    ]
    bus.publish(
        Event(
            type="subnet.member.snapshot.changed",
            source="test",
            ts=601.0,
            payload={
                "node_id": "member-new",
                "snapshot": {"ready": True},
            },
        )
    )
    assert await bus.wait_for_idle()

    refreshed = docs["epsilon"]["data"]["media"]["route"]
    assert refreshed["active_route"] == "member_browser_direct"
    assert refreshed["preferred_member_id"] == "member-new"
    assert refreshed["producer_target"]["member_id"] == "member-new"
    assert refreshed["member_browser_direct"]["candidate_members"] == ["member-new"]
    assert refreshed["monitoring"]["refresh_cause"] == "subnet.member.snapshot.changed"
