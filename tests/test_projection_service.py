from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from contextlib import asynccontextmanager

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.services.scenario import projection_service as projection_service_module


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


def _fake_async_get_ydoc(state: dict[str, _FakeMap], calls: list[dict[str, object]] | None = None):
    def _factory(_ws: str, **kwargs) -> _FakeAsyncDoc:
        if calls is not None:
            calls.append(dict(kwargs))
        return _FakeAsyncDoc(state)

    return _factory


@asynccontextmanager
async def _fake_ystore_write_metadata(**kwargs):
    yield kwargs


def test_projection_service_merges_deep_yjs_paths_without_overwriting_siblings(monkeypatch) -> None:
    fake_state = {"data": _FakeMap()}

    target = SimpleNamespace(
        backend="yjs",
        path="data/skills/profile/{user_id}/settings",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(projection_service_module, "mutate_live_room", lambda _ws, _mutator: False)
    monkeypatch.setattr(projection_service_module, "async_get_ydoc", _fake_async_get_ydoc(fake_state))

    asyncio.run(
        service.apply(
            "current_user",
            "profile.settings",
            {"theme": "dark"},
            user_id="u1",
            webspace_id="ws-test",
        )
    )
    asyncio.run(
        service.apply(
            "current_user",
            "profile.settings",
            {"theme": "light"},
            user_id="u2",
            webspace_id="ws-test",
        )
    )

    assert fake_state["data"]["skills"]["profile"]["u1"]["settings"] == {"theme": "dark"}
    assert fake_state["data"]["skills"]["profile"]["u2"]["settings"] == {"theme": "light"}


def test_projection_service_skips_identical_flat_yjs_update(monkeypatch) -> None:
    class _CountingMap(_FakeMap):
        def __init__(self) -> None:
            super().__init__()
            self.set_calls: list[tuple[str, object]] = []

        def set(self, txn, key: str, value: object) -> None:  # noqa: ARG002
            self.set_calls.append((key, value))
            super().set(txn, key, value)

    fake_root = _CountingMap()
    fake_state = {"data": fake_root}

    target = SimpleNamespace(
        backend="yjs",
        path="data/weather",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(projection_service_module, "mutate_live_room", lambda _ws, _mutator: False)
    monkeypatch.setattr(projection_service_module, "async_get_ydoc", _fake_async_get_ydoc(fake_state))

    asyncio.run(service.apply("runtime", "weather", {"city": "Moscow"}, webspace_id="ws-test"))
    asyncio.run(service.apply("runtime", "weather", {"city": "Moscow"}, webspace_id="ws-test"))

    assert fake_root["weather"] == {"city": "Moscow"}
    assert len(fake_root.set_calls) == 1


def test_projection_service_skips_identical_deep_yjs_update(monkeypatch) -> None:
    class _CountingMap(_FakeMap):
        def __init__(self) -> None:
            super().__init__()
            self.set_calls: list[tuple[str, object]] = []

        def set(self, txn, key: str, value: object) -> None:  # noqa: ARG002
            self.set_calls.append((key, value))
            super().set(txn, key, value)

    fake_root = _CountingMap()
    fake_state = {"data": fake_root}

    target = SimpleNamespace(
        backend="yjs",
        path="data/skills/profile/u1/settings",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(projection_service_module, "mutate_live_room", lambda _ws, _mutator: False)
    monkeypatch.setattr(projection_service_module, "async_get_ydoc", _fake_async_get_ydoc(fake_state))

    asyncio.run(service.apply("runtime", "profile", {"theme": "dark"}, webspace_id="ws-test"))
    asyncio.run(service.apply("runtime", "profile", {"theme": "dark"}, webspace_id="ws-test"))

    assert fake_root["skills"]["profile"]["u1"]["settings"] == {"theme": "dark"}
    assert len(fake_root.set_calls) == 1


def test_projection_service_passes_target_root_to_async_get_ydoc(monkeypatch) -> None:
    fake_state = {"data": _FakeMap()}
    calls: list[dict[str, object]] = []

    target = SimpleNamespace(
        backend="yjs",
        path="data/weather",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(projection_service_module, "mutate_live_room", lambda _ws, _mutator: False)
    monkeypatch.setattr(
        projection_service_module,
        "async_get_ydoc",
        _fake_async_get_ydoc(fake_state, calls),
    )

    asyncio.run(service.apply("runtime", "weather", {"city": "Moscow"}, webspace_id="ws-test"))

    assert calls == [{"load_mark_roots": ["data"]}]


def test_projection_service_marks_skill_owner_in_write_metadata(monkeypatch) -> None:
    fake_state = {"data": _FakeMap()}
    metadata_calls: list[dict[str, object]] = []

    @asynccontextmanager
    async def _capture_metadata(**kwargs):
        metadata_calls.append(dict(kwargs))
        yield kwargs

    target = SimpleNamespace(
        backend="yjs",
        path="data/weather",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(projection_service_module, "mutate_live_room", lambda _ws, _mutator: False)
    monkeypatch.setattr(projection_service_module, "async_get_ydoc", _fake_async_get_ydoc(fake_state))
    monkeypatch.setattr(projection_service_module, "ystore_write_metadata", _capture_metadata)
    monkeypatch.setattr(
        projection_service_module,
        "get_current_skill",
        lambda: SimpleNamespace(name="infrastate_skill"),
    )

    asyncio.run(service.apply("subnet", "infrastate.snapshot", {"ok": True}, webspace_id="ws-test"))

    assert metadata_calls == [
        {
            "root_names": ["data"],
            "source": "projection_service",
            "owner": "skill:infrastate_skill",
            "channel": "projection.yjs",
        }
    ]


def test_projection_service_skips_live_room_fast_path_for_skill_owned_writes(monkeypatch) -> None:
    fake_state = {"data": _FakeMap()}
    calls: list[dict[str, object]] = []
    mutate_calls: list[str] = []

    target = SimpleNamespace(
        backend="yjs",
        path="data/weather",
        webspace_id=None,
    )
    registry = SimpleNamespace(resolve=lambda scope, slot: [target])  # noqa: ARG005
    service = projection_service_module.ProjectionService(
        ctx=SimpleNamespace(),
        registry=registry,
    )

    monkeypatch.setattr(
        projection_service_module,
        "mutate_live_room",
        lambda ws, _mutator: mutate_calls.append(ws) or True,
    )
    monkeypatch.setattr(
        projection_service_module,
        "async_get_ydoc",
        _fake_async_get_ydoc(fake_state, calls),
    )
    monkeypatch.setattr(projection_service_module, "ystore_write_metadata", _fake_ystore_write_metadata)
    monkeypatch.setattr(
        projection_service_module,
        "get_current_skill",
        lambda: SimpleNamespace(name="infra_access_skill"),
    )

    asyncio.run(service.apply("subnet", "infra_access.snapshot", {"ok": True}, webspace_id="ws-test"))

    assert mutate_calls == []
    assert calls == [{"load_mark_roots": ["data"]}]
