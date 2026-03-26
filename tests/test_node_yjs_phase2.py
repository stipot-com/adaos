from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

if "nats" not in sys.modules:
    sys.modules["nats"] = types.ModuleType("nats")
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.api import node_api as node_api_module
from adaos.apps.cli.commands import node as node_cli_module


def test_node_yjs_switch_scenario_endpoint_forwards_set_home(monkeypatch) -> None:
    captured: list[tuple[str, str, bool]] = []

    async def _fake_switch(webspace_id: str, scenario_id: str, *, set_home: bool = False) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"role": kwargs.get("role"), "webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(scenario_id="prompt_engineer_scenario", set_home=True),
        )
    )

    assert captured == [("phase2-node", "prompt_engineer_scenario", True)]
    assert result["ok"] is True
    assert result["runtime"]["webspace_id"] == "phase2-node"


def test_node_yjs_go_home_endpoint_uses_helper(monkeypatch) -> None:
    captured: list[str] = []

    async def _fake_go_home(webspace_id: str) -> dict[str, object]:
        captured.append(webspace_id)
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": "prompt_engineer_scenario"}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "go_home_webspace", _fake_go_home)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_go_home("phase2-home"))

    assert captured == ["phase2-home"]
    assert result["scenario_id"] == "prompt_engineer_scenario"
    assert result["runtime"]["webspace_id"] == "phase2-home"


def test_node_yjs_set_home_requires_scenario_id(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))

    result = asyncio.run(node_api_module.node_yjs_set_home("phase2-home", node_api_module.WebspaceYjsActionRequest()))

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["error"] == "scenario_id_required"


def test_node_cli_yjs_control_action_includes_set_home(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    rendered: list[tuple[object, bool]] = []

    monkeypatch.setattr(node_cli_module, "load_config", lambda: SimpleNamespace(role="hub", hub_url=None, token="secret"))
    monkeypatch.setitem(
        sys.modules,
        "adaos.apps.cli.active_control",
        types.SimpleNamespace(
            resolve_control_base_url=lambda explicit=None, hub_url=None: explicit or "http://127.0.0.1:8080",
            resolve_control_token=lambda explicit=None: explicit or "secret",
        ),
    )
    monkeypatch.setattr(
        node_cli_module,
        "_control_post_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "webspace_id": "phase2-home", "scenario_id": "prompt_engineer_scenario"})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_control_action(
        action="scenario",
        webspace="phase2-home",
        scenario_id="prompt_engineer_scenario",
        set_home=True,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces/phase2-home/scenario",
            "body": {"scenario_id": "prompt_engineer_scenario", "set_home": True},
        }
    ]
    assert rendered[-1][1] is True
