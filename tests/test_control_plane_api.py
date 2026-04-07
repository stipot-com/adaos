from __future__ import annotations

import sys
import types

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
