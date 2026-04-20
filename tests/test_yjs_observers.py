from __future__ import annotations
import sys
import types

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
            self.rooms_ready = object()
            self.log = object()
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

    class _StubGatewayYDoc:
        def get_map(self, name: str) -> _StubMap:  # noqa: ARG002
            return _StubMap()

    class _StubYRoom:
        def __init__(self, *, ready=None, ystore=None, log=None) -> None:
            self.ready = ready
            self.ystore = ystore
            self.log = log
            self.ydoc = _StubGatewayYDoc()

        async def stop(self) -> None:
            return None

    websocket_mod = types.ModuleType("ypy_websocket.websocket")
    websocket_mod.Websocket = object

    websocket_server_mod = types.ModuleType("ypy_websocket.websocket_server")
    websocket_server_mod.WebsocketServer = _StubWebsocketServer

    yroom_mod = types.ModuleType("ypy_websocket.yroom")
    yroom_mod.YRoom = _StubYRoom

    ypy_websocket_mod = types.ModuleType("ypy_websocket")
    ypy_websocket_mod.__path__ = []  # type: ignore[attr-defined]
    ypy_websocket_mod.ystore = ystore_mod

    sys.modules["ypy_websocket"] = ypy_websocket_mod
    sys.modules["ypy_websocket.ystore"] = ystore_mod
    sys.modules["ypy_websocket.websocket"] = websocket_mod
    sys.modules["ypy_websocket.websocket_server"] = websocket_server_mod
    sys.modules["ypy_websocket.yroom"] = yroom_mod

from adaos.services.weather import observer as weather_observer
from adaos.services.yjs import observers as yjs_observers


class _FakeYDoc:
    def __init__(self) -> None:
        self.observe_after_transaction_calls = []
        self.unobserve_after_transaction_calls = []

    def observe_after_transaction(self, callback):
        self.observe_after_transaction_calls.append(callback)
        return len(self.observe_after_transaction_calls)

    def unobserve_after_transaction(self, sub_id):
        self.unobserve_after_transaction_calls.append(sub_id)


def _reset_yjs_observer_state(monkeypatch) -> None:
    monkeypatch.setattr(yjs_observers, "_OBSERVERS", [])
    monkeypatch.setattr(yjs_observers, "_ATTACHED_OBSERVERS", {})
    monkeypatch.setattr(yjs_observers, "_ACTIVE_YDOC_IDS", {})


def test_attach_room_observers_is_idempotent_for_same_doc(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    calls: list[tuple[str, int]] = []

    def _observer(webspace_id: str, ydoc) -> None:
        calls.append((webspace_id, id(ydoc)))

    ydoc = object()
    yjs_observers.register_room_observer(_observer)

    yjs_observers.attach_room_observers("default", ydoc)
    yjs_observers.attach_room_observers("default", ydoc)

    assert calls == [("default", id(ydoc))]


def test_attach_room_observers_reattaches_for_new_doc(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    calls: list[int] = []

    def _observer(_webspace_id: str, ydoc) -> None:
        calls.append(id(ydoc))

    first_doc = object()
    second_doc = object()
    yjs_observers.register_room_observer(_observer)

    yjs_observers.attach_room_observers("default", first_doc)
    yjs_observers.attach_room_observers("default", second_doc)
    yjs_observers.attach_room_observers("default", second_doc)

    assert calls == [id(first_doc), id(second_doc)]


def test_attach_room_observers_retries_after_failed_attach(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    attempts = 0

    def _observer(_webspace_id: str, _ydoc) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("attach failed")

    ydoc = object()
    yjs_observers.register_room_observer(_observer)

    yjs_observers.attach_room_observers("default", ydoc)
    yjs_observers.attach_room_observers("default", ydoc)

    assert attempts == 2


def test_forget_room_observers_calls_detach_callbacks(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    detached: list[tuple[str, int]] = []

    def _observer(webspace_id: str, ydoc):
        def _detach() -> None:
            detached.append((webspace_id, id(ydoc)))

        return _detach

    ydoc = object()
    yjs_observers.register_room_observer(_observer)

    yjs_observers.attach_room_observers("default", ydoc)
    yjs_observers.forget_room_observers("default", ydoc)

    assert detached == [("default", id(ydoc))]


def test_attach_room_observers_detaches_previous_doc(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    detached: list[int] = []

    def _observer(_webspace_id: str, ydoc):
        def _detach() -> None:
            detached.append(id(ydoc))

        return _detach

    first_doc = object()
    second_doc = object()
    yjs_observers.register_room_observer(_observer)

    yjs_observers.attach_room_observers("default", first_doc)
    yjs_observers.attach_room_observers("default", second_doc)

    assert detached == [id(first_doc)]


def test_weather_observer_reattaches_for_new_doc(monkeypatch) -> None:
    _reset_yjs_observer_state(monkeypatch)
    monkeypatch.setattr(weather_observer, "_YDOC_OBSERVERS", {})
    monkeypatch.setattr(weather_observer, "_YDOC_LOOPS", {})
    monkeypatch.setattr(weather_observer, "_PENDING_DOC_CHECKS", {})
    monkeypatch.setattr(weather_observer, "_LAST_CITY_IN_DOC", {})
    monkeypatch.setattr(weather_observer, "_LAST_DOC_CHECK_AT", {})
    monkeypatch.setattr(weather_observer, "_LAST_NO_CITY_LOG_AT", {})
    monkeypatch.setattr(weather_observer, "_OBSERVER_STATS", {})
    monkeypatch.setattr(weather_observer, "_current_city_from_doc", lambda _ydoc: None)

    first_doc = _FakeYDoc()
    second_doc = _FakeYDoc()

    yjs_observers.register_room_observer(weather_observer._room_observer)

    yjs_observers.attach_room_observers("default", first_doc)
    yjs_observers.attach_room_observers("default", first_doc)
    yjs_observers.attach_room_observers("default", second_doc)

    assert len(first_doc.observe_after_transaction_calls) == 1
    assert first_doc.unobserve_after_transaction_calls == [1]
    assert len(second_doc.observe_after_transaction_calls) == 1
    assert weather_observer._YDOC_OBSERVERS["default"][0] == id(second_doc)


class _FakeLoop:
    def __init__(self) -> None:
        self.callbacks = []

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback) -> None:
        self.callbacks.append(callback)


def test_weather_observer_schedules_on_captured_loop(monkeypatch) -> None:
    monkeypatch.setattr(weather_observer, "_YDOC_OBSERVERS", {})
    monkeypatch.setattr(weather_observer, "_YDOC_LOOPS", {})
    monkeypatch.setattr(weather_observer, "_PENDING_DOC_CHECKS", {})
    monkeypatch.setattr(weather_observer, "_LAST_CITY_IN_DOC", {})
    monkeypatch.setattr(weather_observer, "_LAST_DOC_CHECK_AT", {})
    monkeypatch.setattr(weather_observer, "_LAST_NO_CITY_LOG_AT", {})
    monkeypatch.setattr(weather_observer, "_OBSERVER_STATS", {})
    monkeypatch.setattr(weather_observer, "_current_city_from_doc", lambda _ydoc: None)

    loop = _FakeLoop()
    monkeypatch.setattr(weather_observer.asyncio, "get_running_loop", lambda: loop)
    ydoc = _FakeYDoc()

    weather_observer._ensure_city_observer("default", ydoc)

    callback = ydoc.observe_after_transaction_calls[0]
    callback()

    assert len(loop.callbacks) == 1
    selected = weather_observer.weather_observer_snapshot(webspace_id="default")["selected"]
    assert selected["scheduled_total"] == 1
    assert selected["inline_total"] == 0
    assert selected["pending"] is True

    loop.callbacks.pop()()

    selected = weather_observer.weather_observer_snapshot(webspace_id="default")["selected"]
    assert selected["pending"] is False
    assert selected["loop_bound"] is True


def test_weather_observer_runs_inline_without_loop(monkeypatch) -> None:
    monkeypatch.setattr(weather_observer, "_YDOC_OBSERVERS", {})
    monkeypatch.setattr(weather_observer, "_YDOC_LOOPS", {})
    monkeypatch.setattr(weather_observer, "_PENDING_DOC_CHECKS", {})
    monkeypatch.setattr(weather_observer, "_LAST_CITY_IN_DOC", {})
    monkeypatch.setattr(weather_observer, "_LAST_DOC_CHECK_AT", {})
    monkeypatch.setattr(weather_observer, "_LAST_NO_CITY_LOG_AT", {})
    monkeypatch.setattr(weather_observer, "_OBSERVER_STATS", {})
    monkeypatch.setattr(weather_observer, "_current_city_from_doc", lambda _ydoc: None)

    def _raise_runtime_error():
        raise RuntimeError("no running loop")

    monkeypatch.setattr(weather_observer.asyncio, "get_running_loop", _raise_runtime_error)
    ydoc = _FakeYDoc()

    weather_observer._ensure_city_observer("default", ydoc)

    callback = ydoc.observe_after_transaction_calls[0]
    callback()

    selected = weather_observer.weather_observer_snapshot(webspace_id="default")["selected"]
    assert selected["inline_total"] == 1
    assert selected["loop_missing_total"] == 1
    assert selected["pending"] is False


def test_weather_observer_idles_when_city_missing(monkeypatch) -> None:
    monkeypatch.setattr(weather_observer, "_YDOC_OBSERVERS", {})
    monkeypatch.setattr(weather_observer, "_YDOC_LOOPS", {})
    monkeypatch.setattr(weather_observer, "_PENDING_DOC_CHECKS", {})
    monkeypatch.setattr(weather_observer, "_LAST_CITY_IN_DOC", {})
    monkeypatch.setattr(weather_observer, "_LAST_DOC_CHECK_AT", {})
    monkeypatch.setattr(weather_observer, "_LAST_NO_CITY_LOG_AT", {})
    monkeypatch.setattr(weather_observer, "_OBSERVER_STATS", {})
    monkeypatch.setattr(weather_observer, "_current_city_from_doc", lambda _ydoc: None)

    debug_calls = []

    def _debug(msg, *args, **kwargs):  # noqa: ARG001
        debug_calls.append(msg % args if args else msg)

    clock = {"now": 100.0}
    monkeypatch.setattr(weather_observer.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(weather_observer._log, "debug", _debug)
    ydoc = _FakeYDoc()

    weather_observer._ensure_city_observer("default", ydoc)

    callback = ydoc.observe_after_transaction_calls[0]
    callback()
    clock["now"] += 1.0
    callback()

    selected = weather_observer.weather_observer_snapshot(webspace_id="default")["selected"]
    assert selected["emit_check_total"] == 2
    assert selected["throttled_total"] == 1
    assert selected["idle_throttled_total"] == 1
    assert debug_calls == ["weather observer check webspace=default city=None"]
