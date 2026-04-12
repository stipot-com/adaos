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

from adaos.apps.cli import active_control
from adaos.services.subnet.link_client import MemberLinkClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = int(status_code)
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def test_probe_control_api_returns_runtime_ping_payload(monkeypatch) -> None:
    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/node/status"):
                raise RuntimeError("node status unavailable")
            if url.endswith("/api/ping"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "runtime": {
                            "transition_role": "candidate",
                            "runtime_instance_id": "rt-b-c-12345678",
                            "admin_mutation_allowed": False,
                        },
                    },
                )
            raise AssertionError(url)

    monkeypatch.setattr(active_control.requests, "Session", _FakeSession)

    code, payload = active_control.probe_control_api(
        base_url="http://127.0.0.1:8778",
        token="dev-local-token",
        timeout_s=0.2,
    )

    assert code == 200
    assert payload["runtime"]["transition_role"] == "candidate"
    assert payload["runtime"]["admin_mutation_allowed"] is False


def test_looks_like_control_api_response_rejects_candidate_runtime_payload() -> None:
    assert (
        active_control._looks_like_control_api_response(
            200,
            {
                "ok": True,
                "runtime": {
                    "transition_role": "candidate",
                    "admin_mutation_allowed": False,
                },
            },
        )
        is False
    )
    assert (
        active_control._looks_like_control_api_response(
            200,
            {
                "ok": True,
                "runtime": {
                    "transition_role": "active",
                    "admin_mutation_allowed": True,
                },
            },
        )
        is True
    )


def test_resolve_control_base_url_skips_candidate_ping_candidates(monkeypatch) -> None:
    monkeypatch.setattr(active_control, "_node_config_control_url", lambda: ("hub", None))
    monkeypatch.setattr(active_control, "_pick_env_override_url", lambda: "")
    monkeypatch.setattr(active_control, "_pick_local_env_url", lambda: "")
    monkeypatch.setattr(active_control, "_autostart_control_url", lambda: "")
    monkeypatch.setattr(active_control, "_pidfile_control_urls", lambda: [])
    monkeypatch.setattr(active_control, "resolve_control_token", lambda *args, **kwargs: "dev-local-token")

    def _probe(*, base_url: str, token: str, timeout_s: float = 2.0):
        if base_url.endswith(":8778"):
            return 200, {"ok": True, "runtime": {"transition_role": "candidate", "admin_mutation_allowed": False}}
        if base_url.endswith(":8777"):
            return 200, {"ok": True, "runtime": {"transition_role": "active", "admin_mutation_allowed": True}}
        return None, None

    monkeypatch.setattr(active_control, "probe_control_api", _probe)

    base = active_control.resolve_control_base_url()

    assert base == "http://127.0.0.1:8777"


def test_member_link_resolve_local_control_base_skips_candidate_ping(monkeypatch) -> None:
    monkeypatch.delenv("ADAOS_SUPERVISOR_URL", raising=False)
    monkeypatch.delenv("ADAOS_SELF_BASE_URL", raising=False)
    monkeypatch.delenv("ADAOS_CONTROL_URL", raising=False)
    monkeypatch.delenv("ADAOS_CONTROL_BASE", raising=False)

    class _FakeSession:
        trust_env = False

        def get(self, url: str, headers=None, timeout=None):
            if url.endswith("/api/supervisor/public/update-status"):
                return _FakeResponse(503)
            if url.startswith("http://127.0.0.1:8777") and url.endswith("/api/ping"):
                raise RuntimeError("active port down")
            if url.startswith("http://127.0.0.1:8778") and url.endswith("/api/ping"):
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
            if url.startswith("http://127.0.0.1:8779") and url.endswith("/api/ping"):
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
            raise RuntimeError(f"unexpected url: {url}")

    monkeypatch.setattr("adaos.services.subnet.link_client.requests.Session", _FakeSession)

    base = MemberLinkClient._resolve_local_control_base()

    assert base == "http://127.0.0.1:8779"
