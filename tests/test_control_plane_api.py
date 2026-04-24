from __future__ import annotations

import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adaos.apps.api.auth import require_token
from adaos.services.system_model import CanonicalObject, CanonicalProjection


def test_node_control_plane_object_self_returns_canonical_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_node_object",
        lambda: CanonicalObject(id="member:member-42", kind="member", title="Kitchen member", status="online"),
    )

    client = TestClient(app)
    resp = client.get("/api/node/control-plane/objects/self")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["object"]["id"] == "member:member-42"
    assert payload["object"]["status"] == "online"


def test_node_logs_returns_local_category_tail(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(node_api, "normalize_log_category", lambda category: "yjs")
    monkeypatch.setattr(
        node_api,
        "list_local_logs",
        lambda **kwargs: {
            "category": kwargs["category"],
            "source_mode": kwargs["source_mode"],
            "available": True,
            "query": {"limit": kwargs["limit"], "lines": kwargs["lines"]},
            "items": [{"name": "yjs_load_mark.jsonl", "tail": ['{"ts":1}']}],
        },
    )

    client = TestClient(app)
    resp = client.get("/api/node/logs/yjs", params={"limit": 3, "lines": 25})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["logs"]["category"] == "yjs"
    assert payload["logs"]["source_mode"] == "node_local_logs_dir"
    assert payload["logs"]["query"] == {"limit": 3, "lines": 25}
    assert payload["logs"]["items"][0]["name"] == "yjs_load_mark.jsonl"


def test_node_sidecar_status_proxies_to_supervisor_when_enabled(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_URL", "http://127.0.0.1:8776")
    monkeypatch.setenv("ADAOS_TOKEN", "dev-local-token")

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "runtime": {"transport_owner": "sidecar"},
                "process": {"listener_running": True},
            }

    class _FakeSession:
        trust_env = False

        def request(self, method: str, url: str, headers=None, json=None, timeout=None):
            assert method == "GET"
            assert url == "http://127.0.0.1:8776/api/supervisor/sidecar/status"
            assert headers["X-AdaOS-Token"] == "dev-local-token"
            assert json is None
            return _FakeResponse()

        def close(self):
            return None

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    monkeypatch.setattr(node_api.requests, "Session", _FakeSession)

    client = TestClient(app)
    resp = client.get("/api/node/sidecar/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["runtime"]["transport_owner"] == "sidecar"
    assert payload["process"]["listener_running"] is True


def test_node_sidecar_restart_proxies_to_supervisor_when_enabled(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    monkeypatch.setenv("ADAOS_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("ADAOS_SUPERVISOR_URL", "http://127.0.0.1:8776")

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "restart": {"accepted": True},
                "reconnect": {"ok": True},
                "runtime": {},
                "process": {"listener_running": True},
            }

    class _FakeSession:
        trust_env = False

        def request(self, method: str, url: str, headers=None, json=None, timeout=None):
            assert method == "POST"
            assert url == "http://127.0.0.1:8776/api/supervisor/sidecar/restart"
            assert json == {"reconnect_hub_root": True}
            return _FakeResponse()

        def close(self):
            return None

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None
    monkeypatch.setattr(node_api.requests, "Session", _FakeSession)

    client = TestClient(app)
    resp = client.post("/api/node/sidecar/restart", json={"reconnect_hub_root": True})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restart"]["accepted"] is True
    assert payload["reconnect"]["ok"] is True


def test_node_control_plane_reliability_projection_returns_canonical_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_reliability_projection",
        lambda webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/reliability",
            kind="reliability",
            title="Hub Alpha reliability",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online"),
            objects=[CanonicalObject(id="runtime:hub:alpha/yjs-sync", kind="runtime", title="Yjs sync", status="online")],
        ),
    )

    client = TestClient(app)
    resp = client.get("/api/node/control-plane/projections/reliability", params={"webspace_id": "desk"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["projection"]["id"] == "projection:hub:alpha/reliability"
    assert payload["projection"]["objects"][0]["id"] == "runtime:hub:alpha/yjs-sync"


def test_node_control_plane_overview_projection_returns_canonical_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_overview_projection",
        lambda webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/overview",
            kind="overview",
            title="Overview",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="warning"),
            context={"summary_tile": {"value": "warning"}},
        ),
    )

    client = TestClient(app)
    resp = client.get("/api/node/control-plane/projections/overview", params={"webspace_id": "desk"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["projection"]["id"] == "projection:hub:alpha/overview"
    assert payload["projection"]["context"]["summary_tile"]["value"] == "warning"


def test_node_control_plane_inventory_projection_returns_canonical_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_inventory_projection",
        lambda: CanonicalProjection(
            id="projection:hub:alpha/inventory",
            kind="inventory",
            title="Hub Alpha inventory",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online"),
            objects=[
                CanonicalObject(id="workspace:desk", kind="workspace", title="Desk", status="online"),
                CanonicalObject(id="capacity:alpha", kind="capacity", title="Capacity", status="online"),
            ],
        ),
    )

    client = TestClient(app)
    resp = client.get("/api/node/control-plane/projections/inventory")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["projection"]["id"] == "projection:hub:alpha/inventory"
    assert payload["projection"]["objects"][0]["id"] == "workspace:desk"


def test_current_control_plane_objects_reuses_short_ttl_cache(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    sys.modules.setdefault("y_py", types.SimpleNamespace(YDoc=type("YDoc", (), {}), apply_update=lambda *args, **kwargs: None))
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.services.system_model import service as mod

    calls = {"inventory": 0, "reliability": 0, "neighborhood": 0}
    node = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online")

    monkeypatch.setattr(mod, "_CONTROL_PLANE_CACHE", {})
    monkeypatch.setattr(mod, "_CONTROL_PLANE_CACHE_TTL_S", 60.0)
    monkeypatch.setattr(mod, "current_node_object", lambda: node)

    def _inventory():
        calls["inventory"] += 1
        return CanonicalProjection(
            id="projection:inventory",
            kind="inventory",
            title="Inventory",
            subject=node,
            objects=[CanonicalObject(id="workspace:desk", kind="workspace", title="Desk", status="online")],
        )

    def _reliability(*, webspace_id=None):
        calls["reliability"] += 1
        return CanonicalProjection(
            id="projection:reliability",
            kind="reliability",
            title="Reliability",
            subject=node,
            objects=[CanonicalObject(id="runtime:yjs", kind="runtime", title="YJS", status="online")],
        )

    def _neighborhood(*, webspace_id=None):
        calls["neighborhood"] += 1
        return CanonicalProjection(
            id="projection:neighborhood",
            kind="neighborhood",
            title="Neighborhood",
            subject=node,
            objects=[CanonicalObject(id="member:beta", kind="member", title="Beta", status="online")],
        )

    monkeypatch.setattr(mod, "current_inventory_projection", _inventory)
    monkeypatch.setattr(mod, "current_reliability_projection", _reliability)
    monkeypatch.setattr(mod, "_current_node_neighborhood_projection", _neighborhood)

    first = mod.current_control_plane_objects()
    second = mod.current_control_plane_objects()

    assert [item.id for item in first] == [item.id for item in second]
    assert calls == {"inventory": 1, "reliability": 1, "neighborhood": 1}


def test_current_node_neighborhood_projection_includes_remote_io_capacity_objects(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    sys.modules.setdefault("y_py", types.SimpleNamespace(YDoc=type("YDoc", (), {}), apply_update=lambda *args, **kwargs: None))
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.services.system_model import service as mod

    node = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online")
    monkeypatch.setattr(mod, "_control_plane_scope_refs", lambda: (None, None))
    monkeypatch.setattr(mod, "current_node_object", lambda: node)
    monkeypatch.setattr(mod, "local_capacity_object", lambda node_id=None: CanonicalObject(id="capacity:alpha", kind="capacity", title="Local capacity", status="online"))
    monkeypatch.setattr(
        mod,
        "current_reliability_projection",
        lambda webspace_id=None: CanonicalProjection(
            id="projection:reliability",
            kind="reliability",
            title="Reliability",
            subject=node,
            objects=[],
        ),
    )
    monkeypatch.setattr(
        mod,
        "get_directory",
        lambda: types.SimpleNamespace(
            list_known_nodes=lambda: [
                {
                    "node_id": "member-2",
                    "subnet_id": "main",
                    "roles": ["member"],
                    "hostname": "Kitchen member",
                    "base_url": "http://member-2.local",
                    "node_state": "ready",
                    "online": True,
                    "runtime_projection": {
                        "captured_at": __import__("time").time() - 5.0,
                        "node_names": ["Kitchen East"],
                        "primary_node_name": "Kitchen East",
                        "ready": True,
                        "route_mode": "ws",
                        "build": {"runtime_version": "0.2.0"},
                        "update_status": {"state": "succeeded", "phase": "validate"},
                    },
                    "capacity": {
                        "io": [
                            {
                                "io_type": "webrtc_media",
                                "priority": 60,
                                "capabilities": [
                                    "webrtc:av",
                                    "producer:member",
                                    "topology:member_browser_direct",
                                    "state:available",
                                ],
                            }
                        ],
                        "skills": [],
                        "scenarios": [],
                    },
                }
            ]
        ),
    )

    projection = mod._current_node_neighborhood_projection().to_dict()
    objects = {item["id"]: item for item in projection["objects"]}

    assert "member:member-2" in objects
    assert "capacity:member-2" in objects
    assert "io:member-2:webrtc_media" in objects
    assert objects["io:member-2:webrtc_media"]["runtime"]["topology"] == "member_browser_direct"
    assert projection["context"]["subnet_runtime_summary"]["freshness_totals"]["fresh"] == 1


def test_current_subnet_planning_context_extracts_summary_nodes_and_constraints(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    sys.modules.setdefault("y_py", types.SimpleNamespace(YDoc=type("YDoc", (), {}), apply_update=lambda *args, **kwargs: None))
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.services.system_model import service as mod

    subject = CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online")
    monkeypatch.setattr(mod, "current_node_object", lambda: subject)
    monkeypatch.setattr(
        mod,
        "current_neighborhood_projection",
        lambda object_id=None, webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/neighborhood",
            kind="neighborhood",
            title="Neighborhood",
            subject=subject,
            context={"subnet_runtime_summary": {"node_total": 2, "freshness_totals": {"fresh": 2}}},
        ),
    )
    monkeypatch.setattr(
        mod,
        "current_task_packet",
        lambda object_id, task_goal=None, webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/task-packet",
            kind="task_packet",
            title="Task packet",
            subject=subject,
            context={
                "task_goal": task_goal,
                "subnet_planning": {
                    "summary": {"node_total": 2, "freshness_totals": {"fresh": 2}},
                    "nodes": [{"id": "hub:alpha"}, {"id": "member:beta"}],
                },
                "constraints": {"roles_allowed": ["role:infra-operator"]},
                "allowed_actions": [{"id": "restart"}],
                "relevant_incidents": [{"id": "incident:1"}],
                "gap": {"ready": {"desired": True, "actual": True}},
            },
        ),
    )

    context = mod.current_subnet_planning_context(task_goal="plan rollout")

    assert context["object_id"] == "hub:alpha"
    assert context["task_goal"] == "plan rollout"
    assert context["summary"]["node_total"] == 2
    assert context["nodes"][1]["id"] == "member:beta"
    assert context["constraints"]["roles_allowed"] == ["role:infra-operator"]
    assert context["source_projection_ids"]["task_packet"] == "projection:hub:alpha/task-packet"


def test_node_control_plane_neighborhood_projection_returns_canonical_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_neighborhood_projection",
        lambda object_id=None, webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/neighborhood",
            kind="neighborhood",
            title="Hub Alpha neighborhood",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online"),
            objects=[
                CanonicalObject(id="member:beta", kind="member", title="Member Beta", status="online"),
                CanonicalObject(id="root:eu", kind="root", title="Root EU", status="online"),
            ],
        ),
    )

    client = TestClient(app)
    resp = client.get("/api/node/control-plane/projections/neighborhood")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["projection"]["id"] == "projection:hub:alpha/neighborhood"
    assert payload["projection"]["objects"][0]["id"] == "member:beta"


def test_node_control_plane_object_topology_and_task_packet_projections_return_payload(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    fake_y_py = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        apply_update=lambda *args, **kwargs: None,
    )
    sys.modules.setdefault("y_py", fake_y_py)
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.apps.api import node_api

    app = FastAPI()
    app.include_router(node_api.router, prefix="/api/node")
    app.dependency_overrides[require_token] = lambda: None

    monkeypatch.setattr(
        node_api,
        "current_object_projection",
        lambda object_id, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/object",
            kind="object",
            title="Object projection",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather", status="warning"),
        ),
    )
    monkeypatch.setattr(
        node_api,
        "current_object_inspector",
        lambda object_id, task_goal=None, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/inspector",
            kind="inspector",
            title="Inspector",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather", status="warning"),
            context={"task_goal": task_goal},
        ),
    )
    monkeypatch.setattr(
        node_api,
        "current_topology_projection",
        lambda object_id, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/topology",
            kind="topology",
            title="Topology projection",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather", status="warning"),
        ),
    )
    monkeypatch.setattr(
        node_api,
        "current_task_packet",
        lambda object_id, task_goal=None, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/task-packet",
            kind="task_packet",
            title="Task packet",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather", status="warning"),
            context={"task_goal": task_goal},
        ),
    )
    monkeypatch.setattr(
        node_api,
        "current_subnet_planning_context",
        lambda object_id=None, task_goal=None, webspace_id=None: {
            "object_id": object_id or "local",
            "task_goal": task_goal,
            "summary": {"node_total": 2, "freshness_totals": {"fresh": 2}},
            "nodes": [{"id": "hub:alpha"}, {"id": "member:beta"}],
        },
    )

    client = TestClient(app)
    object_resp = client.get("/api/node/control-plane/projections/object", params={"object_id": "skill:weather"})
    inspector_resp = client.get(
        "/api/node/control-plane/projections/object-inspector",
        params={"object_id": "skill:weather", "task_goal": "inspect weather"},
    )
    topology_resp = client.get("/api/node/control-plane/projections/topology", params={"object_id": "skill:weather"})
    task_resp = client.get(
        "/api/node/control-plane/projections/task-packet",
        params={"object_id": "skill:weather", "task_goal": "diagnose weather"},
    )
    subnet_planning_resp = client.get(
        "/api/node/control-plane/contexts/subnet-planning",
        params={"object_id": "skill:weather", "task_goal": "diagnose weather"},
    )

    assert object_resp.status_code == 200
    assert object_resp.json()["projection"]["id"] == "projection:skill:weather/object"
    assert inspector_resp.status_code == 200
    assert inspector_resp.json()["projection"]["kind"] == "inspector"
    assert inspector_resp.json()["projection"]["context"]["task_goal"] == "inspect weather"
    assert topology_resp.status_code == 200
    assert topology_resp.json()["projection"]["kind"] == "topology"
    assert task_resp.status_code == 200
    assert task_resp.json()["projection"]["context"]["task_goal"] == "diagnose weather"
    assert subnet_planning_resp.status_code == 200
    assert subnet_planning_resp.json()["context"]["summary"]["node_total"] == 2
    assert subnet_planning_resp.json()["context"]["task_goal"] == "diagnose weather"


def test_sdk_control_plane_get_self_object(monkeypatch) -> None:
    sys.modules.setdefault("nats", types.SimpleNamespace())
    sys.modules.setdefault("y_py", types.SimpleNamespace(YDoc=type("YDoc", (), {}), apply_update=lambda *args, **kwargs: None))
    fake_ystore_module = types.ModuleType("ypy_websocket.ystore")
    fake_ystore_module.BaseYStore = object
    fake_ystore_module.YDocNotFound = RuntimeError
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ypy_websocket.ystore = fake_ystore_module
    sys.modules.setdefault("ypy_websocket", fake_ypy_websocket)
    sys.modules.setdefault("ypy_websocket.ystore", fake_ystore_module)
    from adaos.sdk import control_plane

    monkeypatch.setattr(control_plane, "get_self_model", lambda: CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha"))
    payload = control_plane.get_self_object()

    assert payload["id"] == "hub:alpha"
    assert payload["kind"] == "hub"


def test_sdk_control_plane_list_skill_objects(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "list_skill_models",
        lambda: [CanonicalObject(id="skill:weather_skill", kind="skill", title="weather_skill")],
    )
    payload = control_plane.list_skill_objects()

    assert payload[0]["id"] == "skill:weather_skill"
    assert payload[0]["kind"] == "skill"


def test_sdk_control_plane_get_reliability_projection(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_reliability_model",
        lambda webspace_id=None: CanonicalProjection(
            id="projection:member:node-1/reliability",
            kind="reliability",
            title="Node reliability",
            subject=CanonicalObject(id="member:node-1", kind="member", title="Node 1", status="online"),
            objects=[CanonicalObject(id="connection:member:node-1/route", kind="connection", title="Route", status="online")],
        ),
    )

    payload = control_plane.get_reliability_projection(webspace_id="desk")
    objects = control_plane.get_reliability_objects(webspace_id="desk")

    assert payload["subject"]["id"] == "member:node-1"
    assert objects[0]["id"] == "connection:member:node-1/route"


def test_sdk_control_plane_object_topology_and_task_packet_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_overview_model",
        lambda webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/overview",
            kind="overview",
            title="Overview",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha"),
            context={"summary_tile": {"value": "online"}},
        ),
    )
    monkeypatch.setattr(
        control_plane,
        "get_object_model",
        lambda object_id, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/object",
            kind="object",
            title="Object",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather"),
        ),
    )
    monkeypatch.setattr(
        control_plane,
        "get_object_inspector_model",
        lambda object_id, task_goal=None, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/inspector",
            kind="inspector",
            title="Inspector",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather"),
            context={"task_goal": task_goal},
        ),
    )
    monkeypatch.setattr(
        control_plane,
        "get_topology_model",
        lambda object_id, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/topology",
            kind="topology",
            title="Topology",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather"),
        ),
    )
    monkeypatch.setattr(
        control_plane,
        "get_task_packet_model",
        lambda object_id, task_goal=None, webspace_id=None: CanonicalProjection(
            id=f"projection:{object_id}/task-packet",
            kind="task_packet",
            title="Task packet",
            subject=CanonicalObject(id=object_id, kind="skill", title="Weather"),
            context={"task_goal": task_goal},
        ),
    )
    monkeypatch.setattr(
        control_plane,
        "get_subnet_planning_context",
        lambda object_id=None, task_goal=None, webspace_id=None: {
            "object_id": object_id or "local",
            "task_goal": task_goal,
            "summary": {"node_total": 2},
            "nodes": [{"id": "hub:alpha"}, {"id": "member:beta"}],
        },
    )

    overview = control_plane.get_overview_projection(webspace_id="desk")
    object_projection = control_plane.get_object_projection("skill:weather", webspace_id="desk")
    inspector_projection = control_plane.get_object_inspector_projection(
        "skill:weather",
        task_goal="inspect weather",
        webspace_id="desk",
    )
    topology_projection = control_plane.get_topology_projection("skill:weather", webspace_id="desk")
    task_packet = control_plane.get_task_packet("skill:weather", task_goal="diagnose weather", webspace_id="desk")
    subnet_planning = control_plane.get_subnet_planning_context(
        "skill:weather",
        task_goal="diagnose weather",
        webspace_id="desk",
    )

    assert overview["id"] == "projection:hub:alpha/overview"
    assert object_projection["id"] == "projection:skill:weather/object"
    assert inspector_projection["kind"] == "inspector"
    assert inspector_projection["context"]["task_goal"] == "inspect weather"
    assert topology_projection["kind"] == "topology"
    assert task_packet["context"]["task_goal"] == "diagnose weather"
    assert subnet_planning["summary"]["node_total"] == 2
    assert subnet_planning["task_goal"] == "diagnose weather"


def test_sdk_control_plane_root_runtime_and_connection_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane
    from adaos.sdk.data import control_plane as data_control_plane

    projection = CanonicalProjection(
        id="projection:hub:alpha/reliability",
        kind="reliability",
        title="Hub Alpha reliability",
        subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha", status="online"),
        objects=[
            CanonicalObject(id="root:eu", kind="root", title="Root EU", status="online"),
            CanonicalObject(id="connection:hub:alpha/root-control", kind="connection", title="Root control", status="online"),
            CanonicalObject(id="runtime:hub:alpha/yjs-sync", kind="runtime", title="Yjs sync", status="online"),
        ],
    )

    monkeypatch.setattr(
        data_control_plane,
        "get_reliability_model",
        lambda webspace_id=None: projection,
    )

    root = control_plane.get_root_object(webspace_id="desk")
    runtimes = control_plane.list_runtime_objects(webspace_id="desk")
    connections = control_plane.list_connection_objects(webspace_id="desk")

    assert root["id"] == "root:eu"
    assert runtimes[0]["kind"] == "runtime"
    assert connections[0]["kind"] == "connection"


def test_sdk_control_plane_get_current_profile_object(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_current_profile_model",
        lambda: CanonicalObject(id="profile:owner", kind="profile", title="Owner"),
    )

    payload = control_plane.get_current_profile_object()

    assert payload["id"] == "profile:owner"
    assert payload["kind"] == "profile"


def test_sdk_control_plane_workspace_and_inventory_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "list_workspace_models",
        lambda: [CanonicalObject(id="workspace:desk", kind="workspace", title="Desk")],
    )
    monkeypatch.setattr(
        control_plane,
        "get_inventory_model",
        lambda: CanonicalProjection(
            id="projection:hub:alpha/inventory",
            kind="inventory",
            title="Inventory",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha"),
            objects=[CanonicalObject(id="capacity:alpha", kind="capacity", title="Capacity")],
        ),
    )

    workspaces = control_plane.list_workspace_objects()
    inventory = control_plane.get_inventory_projection()
    inventory_objects = control_plane.get_inventory_objects()

    assert workspaces[0]["id"] == "workspace:desk"
    assert inventory["id"] == "projection:hub:alpha/inventory"
    assert inventory_objects[0]["kind"] == "capacity"


def test_sdk_control_plane_local_capacity_and_io_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_local_capacity_model",
        lambda: CanonicalObject(id="capacity:node-1", kind="capacity", title="Local capacity"),
    )
    monkeypatch.setattr(
        control_plane,
        "list_local_io_models",
        lambda: [CanonicalObject(id="io:node-1:say", kind="io_endpoint", title="say")],
    )

    capacity = control_plane.get_local_capacity_object()
    io_items = control_plane.list_local_io_objects()

    assert capacity["id"] == "capacity:node-1"
    assert io_items[0]["kind"] == "io_endpoint"


def test_capacity_loader_reuses_cached_node_yaml_until_mtime_changes(tmp_path: Path, monkeypatch) -> None:
    from adaos.services import capacity as mod

    base = tmp_path / "base"
    base.mkdir(parents=True, exist_ok=True)
    node_yaml = base / "node.yaml"
    node_yaml.write_text(
        "capacity:\n"
        "  io:\n"
        "    - io_type: git\n"
        "      capabilities: [git]\n",
        encoding="utf-8",
    )

    real_safe_load = mod.yaml.safe_load
    calls = {"count": 0}

    def _counting_safe_load(text):
        calls["count"] += 1
        return real_safe_load(text)

    monkeypatch.setattr(mod.yaml, "safe_load", _counting_safe_load)
    mod._CAPACITY_CACHE.clear()

    first = mod.load_capacity_from_node_yaml(base)
    second = mod.load_capacity_from_node_yaml(base)

    assert calls["count"] == 1
    assert first == second
    assert first is not second


def test_sdk_control_plane_device_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "list_device_models",
        lambda: [CanonicalObject(id="device:tablet-kitchen", kind="device", title="tablet-kitchen")],
    )

    devices = control_plane.list_device_objects()

    assert devices[0]["id"] == "device:tablet-kitchen"
    assert devices[0]["kind"] == "device"


def test_sdk_control_plane_quota_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "list_quota_models",
        lambda webspace_id=None: [CanonicalObject(id="quota:telegram-outbox", kind="quota", title="telegram outbox quota")],
    )

    quotas = control_plane.list_quota_objects(webspace_id="desk")

    assert quotas[0]["id"] == "quota:telegram-outbox"
    assert quotas[0]["kind"] == "quota"


def test_sdk_control_plane_neighborhood_helpers(monkeypatch) -> None:
    from adaos.sdk import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_neighborhood_model",
        lambda object_id=None, webspace_id=None: CanonicalProjection(
            id="projection:hub:alpha/neighborhood",
            kind="neighborhood",
            title="Neighborhood",
            subject=CanonicalObject(id="hub:alpha", kind="hub", title="Hub Alpha"),
            objects=[CanonicalObject(id="member:beta", kind="member", title="Member Beta")],
        ),
    )

    projection = control_plane.get_neighborhood_projection(object_id="hub:alpha", webspace_id="desk")
    objects = control_plane.get_neighborhood_objects(object_id="hub:alpha", webspace_id="desk")

    assert projection["id"] == "projection:hub:alpha/neighborhood"
    assert objects[0]["id"] == "member:beta"
