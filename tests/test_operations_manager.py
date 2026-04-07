from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(
        YDoc=type("YDoc", (), {}),
        encode_state_vector=lambda *args, **kwargs: b"",
        encode_state_as_update=lambda *args, **kwargs: b"",
        apply_update=lambda *args, **kwargs: None,
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

from adaos.services.operations.manager import OperationManager
import adaos.services.operations.manager as operations_manager


class _FakeMap(dict):
    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, default)

    def set(self, txn, key, value):
        self[key] = value


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeYDoc:
    def __init__(self):
        self._maps = {"runtime": _FakeMap()}

    def get_map(self, name: str):
        return self._maps.setdefault(name, _FakeMap())

    def begin_transaction(self):
        return _FakeTxn()


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event) -> None:
        self.events.append(event)


class _FakePaths:
    def base_dir(self):
        return "test-base-dir"


class _FakeToastService:
    pushed: list[dict[str, object]] = []

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def push(self, message: str, **kwargs):
        self.pushed.append({"message": message, **kwargs})


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        bus=_FakeBus(),
        paths=_FakePaths(),
    )


def test_operation_manager_projects_active_operations_to_yjs(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)

    manager = OperationManager(_make_ctx())
    operation = manager.create_operation(
        kind="skill.install",
        target_kind="skill",
        target_id="demo_skill",
        webspace_id="default",
        scope=["global", "skill.install", "skill:demo_skill"],
        message="Accepted skill install",
    )

    manager.update_operation(
        operation.operation_id,
        status="running",
        progress=25,
        message="Installing",
        current_step="skill.install",
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["active"]
    current = next(item for item in snapshot["active_items"] if item["target_id"] == "demo_skill")
    assert current["target_id"] == "demo_skill"
    assert current["status"] == "running"

    runtime_map = docs["default"].get_map("runtime")
    operations = runtime_map.get("operations")
    assert isinstance(operations, dict)
    assert current["operation_id"] in (operations.get("by_id") or {})


def test_operation_manager_records_notifications_on_completion(monkeypatch) -> None:
    docs: dict[str, _FakeYDoc] = {}
    _FakeToastService.pushed = []

    @contextmanager
    def _get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    @asynccontextmanager
    async def _async_get_ydoc(webspace_id: str):
        yield docs.setdefault(webspace_id, _FakeYDoc())

    monkeypatch.setattr(operations_manager, "get_ydoc", _get_ydoc)
    monkeypatch.setattr(operations_manager, "async_get_ydoc", _async_get_ydoc)
    monkeypatch.setattr(operations_manager, "WebToastService", _FakeToastService)

    manager = OperationManager(_make_ctx())
    operation = manager.create_operation(
        kind="scenario.install",
        target_kind="scenario",
        target_id="welcome",
        webspace_id="default",
        scope=["global", "scenario.install", "scenario:welcome"],
    )
    manager.update_operation(
        operation.operation_id,
        status="succeeded",
        progress=100,
        message="Installed scenario welcome",
        result={"target_id": "welcome"},
        finished=True,
    )

    snapshot = manager.snapshot(webspace_id="default")
    assert snapshot["notifications"]
    assert snapshot["notifications"][-1]["operation_id"] == operation.operation_id
    assert _FakeToastService.pushed[-1]["message"] == "scenario welcome completed"

    runtime_map = docs["default"].get_map("runtime")
    assert isinstance(runtime_map.get("notifications"), list)
