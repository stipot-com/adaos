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


async def _awaitable(value):
    return value


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


def test_node_yjs_switch_scenario_endpoint_preserves_implicit_set_home(monkeypatch) -> None:
    captured: list[tuple[str, str, bool | None]] = []

    async def _fake_switch(webspace_id: str, scenario_id: str, *, set_home: bool | None = None) -> dict[str, object]:
        captured.append((webspace_id, scenario_id, set_home))
        return {"ok": True, "accepted": True, "webspace_id": webspace_id, "scenario_id": scenario_id, "set_home": set_home}

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "switch_webspace_scenario", _fake_switch)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_switch_scenario(
            "phase2-node",
            node_api_module.WebspaceYjsActionRequest(scenario_id="prompt_engineer_scenario"),
        )
    )

    assert captured == [("phase2-node", "prompt_engineer_scenario", None)]
    assert result["ok"] is True
    assert result["set_home"] is None


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


def test_node_yjs_toggle_install_endpoint_uses_desktop_service(monkeypatch) -> None:
    captured: list[tuple[str, str, str]] = []

    class _Installed:
        def to_dict(self) -> dict[str, list[str]]:
            return {"apps": ["scenario:prompt_engineer_scenario"], "widgets": ["weather"]}

    class _DesktopService:
        def toggle_install_with_live_room(self, item_type: str, item_id: str, webspace_id: str | None = None) -> None:
            captured.append((item_type, item_id, str(webspace_id or "")))

        async def get_installed_async(self, webspace_id: str | None = None) -> _Installed:
            assert webspace_id == "default"
            return _Installed()

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebDesktopService", _DesktopService)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_toggle_install(
            "default",
            node_api_module.WebspaceToggleInstallRequest(type="widget", id="weather"),
        )
    )

    assert captured == [("widget", "weather", "default")]
    assert result["ok"] is True
    assert result["installed"]["widgets"] == ["weather"]
    assert result["runtime"]["webspace_id"] == "default"


def test_node_infrastate_snapshot_endpoint_runs_skill_tool(monkeypatch) -> None:
    captured: list[tuple[str, str, dict[str, object]]] = []

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            captured.append((skill_name, tool_name, dict(payload)))
            return {"summary": {"label": "Core update", "value": "idle"}}

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "get_ctx", lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=None, caps=None, settings=None))
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(node_api_module.node_infrastate_snapshot("default"))

    assert captured == [("infrastate_skill", "get_snapshot", {"webspace_id": "default"})]
    assert result["ok"] is True
    assert result["snapshot"]["summary"]["value"] == "idle"


def test_node_infrastate_snapshot_endpoint_returns_fallback_on_tool_error(monkeypatch) -> None:
    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            raise RuntimeError(f"boom:{skill_name}:{tool_name}:{payload.get('webspace_id')}")

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub", node_id="hub-1"))
    monkeypatch.setattr(node_api_module, "runtime_lifecycle_snapshot", lambda: {"node_state": "ready", "reason": "ok"})
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"assessment": {"state": "nominal"}, "webspace_id": kwargs.get("webspace_id")})
    monkeypatch.setattr(node_api_module, "get_ctx", lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=None, caps=None, settings=None))
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(node_api_module.node_infrastate_snapshot("default"))

    assert result["ok"] is True
    assert result["degraded"] is True
    assert "RuntimeError" in str(result["error"])
    assert result["snapshot"]["fallback"] is True
    assert result["snapshot"]["summary"]["label"] == "Infra State"


def test_node_infrastate_action_endpoint_publishes_event_and_returns_snapshot(monkeypatch) -> None:
    published: list[object] = []
    wait_calls: list[float] = []
    captured: list[tuple[str, str, dict[str, object]]] = []

    class _FakeBus:
        def publish(self, event) -> None:
            published.append(event)

        async def wait_for_idle(self, timeout: float = 0.0) -> bool:
            wait_calls.append(timeout)
            return True

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
            captured.append((skill_name, tool_name, dict(payload)))
            return {"summary": {"label": "Infra State", "value": "ready"}, "last_refresh_ts": 123.0}

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "get_ctx",
        lambda: SimpleNamespace(skills_repo=None, sql=None, git=None, paths=None, bus=_FakeBus(), caps=None, settings=None),
    )
    monkeypatch.setattr(node_api_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(node_api_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_api_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(
        node_api_module.node_infrastate_action(
            node_api_module.InfrastateActionRequest(
                id="select_node",
                webspace_id="default",
                node_id="member-1",
            )
        )
    )

    assert len(published) == 1
    assert getattr(published[0], "type", "") == "infrastate.action"
    assert getattr(published[0], "payload", {})["node_id"] == "member-1"
    assert wait_calls == [2.5]
    assert captured == [("infrastate_skill", "get_snapshot", {"webspace_id": "default"})]
    assert result["ok"] is True
    assert result["action"] == "select_node"
    assert result["snapshot"]["summary"]["value"] == "ready"


def test_node_yjs_set_home_requires_scenario_id(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))

    result = asyncio.run(node_api_module.node_yjs_set_home("phase2-home", node_api_module.WebspaceYjsActionRequest()))

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["error"] == "scenario_id_required"


def test_node_yjs_ensure_dev_endpoint_forwards_requested_id_and_title(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    async def _fake_ensure_dev(scenario_id: str, *, requested_id: str | None = None, title: str | None = None) -> dict[str, object]:
        captured.append(
            {
                "scenario_id": scenario_id,
                "requested_id": requested_id,
                "title": title,
            }
        )
        return {
            "ok": True,
            "accepted": True,
            "created": False,
            "webspace_id": requested_id or "dev_prompt_engineer_scenario",
            "scenario_id": scenario_id,
            "home_scenario": scenario_id,
        }

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "ensure_dev_webspace_for_scenario", _fake_ensure_dev)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_ensure_dev(
            node_api_module.WebspaceYjsActionRequest(
                scenario_id="prompt_engineer_scenario",
                requested_id="dev_prompt",
                title="Prompt IDE",
            )
        )
    )

    assert captured == [
        {
            "scenario_id": "prompt_engineer_scenario",
            "requested_id": "dev_prompt",
            "title": "Prompt IDE",
        }
    ]
    assert result["ok"] is True
    assert result["runtime"]["webspace_id"] == "dev_prompt"


def test_node_yjs_webspaces_endpoint_returns_manifest_listing(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "WebspaceService",
        lambda: SimpleNamespace(
            list=lambda mode="mixed": [
                SimpleNamespace(
                    id="default",
                    title="Desktop",
                    created_at=123.0,
                    kind="workspace",
                    home_scenario="web_desktop",
                    source_mode="workspace",
                ),
                SimpleNamespace(
                    id="dev_prompt",
                    title="Prompt IDE",
                    created_at=456.0,
                    kind="dev",
                    home_scenario="prompt_engineer_scenario",
                    source_mode="dev",
                ),
            ]
        ),
    )

    result = asyncio.run(node_api_module.node_yjs_webspaces())

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["items"][0]["id"] == "default"
    assert result["items"][1]["kind"] == "dev"


def test_node_yjs_create_webspace_endpoint_uses_service(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _Svc:
        async def create(self, requested_id, title, *, scenario_id="web_desktop", dev=False):
            captured.append(
                {
                    "requested_id": requested_id,
                    "title": title,
                    "scenario_id": scenario_id,
                    "dev": dev,
                }
            )
            return SimpleNamespace(
                id="preview-space",
                title=title or "Preview Space",
                created_at=123.0,
                kind="dev" if dev else "workspace",
                home_scenario=scenario_id,
                source_mode="dev" if dev else "workspace",
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebspaceService", _Svc)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_create_webspace(
            node_api_module.WebspaceCreateRequest(
                id="preview-space",
                title="Preview Space",
                scenario_id="prompt_engineer_scenario",
                dev=True,
            )
        )
    )

    assert captured == [
        {
            "requested_id": "preview-space",
            "title": "Preview Space",
            "scenario_id": "prompt_engineer_scenario",
            "dev": True,
        }
    ]
    assert result["ok"] is True
    assert result["webspace"]["id"] == "preview-space"
    assert result["runtime"]["webspace_id"] == "preview-space"


def test_node_yjs_update_webspace_endpoint_uses_service(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _Svc:
        async def update_metadata(self, webspace_id, *, title=None, home_scenario=None):
            captured.append(
                {
                    "webspace_id": webspace_id,
                    "title": title,
                    "home_scenario": home_scenario,
                }
            )
            return SimpleNamespace(
                id=webspace_id,
                title=title or "Desktop",
                created_at=123.0,
                kind="workspace",
                home_scenario=home_scenario or "web_desktop",
                source_mode="workspace",
            )

    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(node_api_module, "WebspaceService", _Svc)
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(
        node_api_module.node_yjs_update_webspace(
            "default",
            node_api_module.WebspaceUpdateRequest(
                title="Desktop",
                home_scenario="prompt_engineer_scenario",
            ),
        )
    )

    assert captured == [
        {
            "webspace_id": "default",
            "title": "Desktop",
            "home_scenario": "prompt_engineer_scenario",
        }
    ]
    assert result["ok"] is True
    assert result["webspace"]["home_scenario"] == "prompt_engineer_scenario"
    assert result["runtime"]["webspace_id"] == "default"


def test_node_yjs_webspace_state_endpoint_returns_operational_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(node_api_module, "load_config", lambda: SimpleNamespace(role="hub"))
    monkeypatch.setattr(
        node_api_module,
        "describe_webspace_operational_state",
        lambda webspace_id: _awaitable(
            SimpleNamespace(
                to_dict=lambda: {
                    "webspace_id": webspace_id,
                    "kind": "dev",
                    "source_mode": "dev",
                    "home_scenario": "prompt_engineer_scenario",
                    "current_scenario": "prompt_engineer_runtime",
                    "current_matches_home": False,
                }
            )
        ),
    )
    monkeypatch.setattr(node_api_module, "yjs_sync_runtime_snapshot", lambda **kwargs: {"webspace_id": kwargs.get("webspace_id")})

    result = asyncio.run(node_api_module.node_yjs_webspace_state("dev_prompt"))

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["webspace"]["webspace_id"] == "dev_prompt"
    assert result["webspace"]["source_mode"] == "dev"
    assert result["runtime"]["webspace_id"] == "dev_prompt"


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


def test_node_cli_scenario_command_omits_set_home_when_flag_is_absent(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    monkeypatch.setattr(
        node_cli_module,
        "_node_yjs_control_action",
        lambda **kwargs: captured.append(dict(kwargs)),
    )

    node_cli_module.node_yjs_scenario(
        webspace="phase2-home",
        scenario_id="prompt_engineer_scenario",
        set_home=False,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "action": "scenario",
            "webspace": "phase2-home",
            "scenario_id": "prompt_engineer_scenario",
            "set_home": None,
            "control": "http://127.0.0.1:8080",
            "json_output": True,
        }
    ]


def test_node_cli_ensure_dev_posts_requested_id_and_title(monkeypatch) -> None:
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
            or (200, {"ok": True, "accepted": True, "created": False, "webspace_id": "dev_prompt", "scenario_id": "prompt_engineer_scenario"})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_ensure_dev_action(
        scenario_id="prompt_engineer_scenario",
        requested_id="dev_prompt",
        title="Prompt IDE",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/dev-webspaces/ensure",
            "body": {
                "scenario_id": "prompt_engineer_scenario",
                "requested_id": "dev_prompt",
                "title": "Prompt IDE",
            },
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_create_posts_metadata(monkeypatch) -> None:
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
            or (200, {"ok": True, "accepted": True, "webspace": {"id": "preview-space", "home_scenario": "prompt_engineer_scenario"}})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_create_action(
        webspace="preview-space",
        title="Preview Space",
        scenario_id="prompt_engineer_scenario",
        dev=True,
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces",
            "body": {
                "id": "preview-space",
                "title": "Preview Space",
                "scenario_id": "prompt_engineer_scenario",
                "dev": True,
            },
        }
    ]
    assert rendered[-1][1] is True


def test_node_cli_update_patches_metadata(monkeypatch) -> None:
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
        "_control_patch_json",
        lambda **kwargs: (
            captured.append(
                {
                    "path": kwargs.get("path"),
                    "body": dict(kwargs.get("body") or {}),
                }
            )
            or (200, {"ok": True, "accepted": True, "webspace": {"id": "default", "home_scenario": "prompt_engineer_scenario"}})
        ),
    )
    monkeypatch.setattr(node_cli_module, "_print", lambda data, *, json_output: rendered.append((data, json_output)))

    node_cli_module._node_yjs_update_action(
        webspace="default",
        title="Desktop",
        home_scenario="prompt_engineer_scenario",
        control="http://127.0.0.1:8080",
        json_output=True,
    )

    assert captured == [
        {
            "path": "/api/node/yjs/webspaces/default",
            "body": {
                "title": "Desktop",
                "home_scenario": "prompt_engineer_scenario",
            },
        }
    ]
    assert rendered[-1][1] is True
