from __future__ import annotations

import sys
import types

if "nats" not in sys.modules:
    sys.modules["nats"] = types.SimpleNamespace()
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

from adaos.services import autostart


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = int(status_code)
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def test_http_probe_local_control_rejects_candidate_runtime(monkeypatch) -> None:
    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "candidate",
                            "admin_mutation_allowed": False,
                        },
                    },
                )
            raise AssertionError(url)

    monkeypatch.setattr(autostart.requests, "Session", _FakeSession)

    assert autostart._http_probe_local_control("127.0.0.1", 8778) is False


def test_http_probe_local_control_accepts_active_runtime(monkeypatch) -> None:
    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "active",
                            "admin_mutation_allowed": True,
                        },
                    },
                )
            raise AssertionError(url)

    monkeypatch.setattr(autostart.requests, "Session", _FakeSession)

    assert autostart._http_probe_local_control("127.0.0.1", 8777) is True
