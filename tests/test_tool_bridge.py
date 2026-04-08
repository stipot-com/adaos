from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

if "nats" not in sys.modules:
    sys.modules["nats"] = types.ModuleType("nats")
if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.api import tool_bridge as tool_bridge_module


def _fake_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        skills_repo=None,
        sql=None,
        git=None,
        paths=None,
        caps=None,
        settings=None,
        bus=None,
    )


def test_call_tool_offloads_local_execution_to_worker(monkeypatch) -> None:
    calls: list[str] = []

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object], timeout: float | None = None) -> dict[str, object]:
            calls.append(f"{skill_name}:{tool_name}:{timeout}")
            return {"skill": skill_name, "tool": tool_name, "payload": payload}

    async def _fake_run_sync(func, *args, **kwargs):
        calls.append("run_sync")
        return func(*args, **kwargs)

    monkeypatch.setattr(tool_bridge_module, "is_accepting_new_work", lambda: True)
    monkeypatch.setattr(tool_bridge_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(tool_bridge_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_bridge_module, "attach_http_trace_headers", lambda _req, _resp: "trace-123")
    monkeypatch.setattr(tool_bridge_module.anyio.to_thread, "run_sync", _fake_run_sync)

    result = asyncio.run(
        tool_bridge_module.call_tool(
            tool_bridge_module.ToolCall(tool="prompt_engineer_skill:prompt_list_project_objects", arguments={}),
            SimpleNamespace(headers={}),
            Response(),
            ctx=_fake_ctx(),
        )
    )

    assert calls[0] == "run_sync"
    assert calls[1] == "prompt_engineer_skill:prompt_list_project_objects:None"
    assert result["ok"] is True
    assert result["trace_id"] == "trace-123"


def test_call_tool_repairs_workspace_runtime_when_runtime_missing(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    (tmp_path / "workspace" / "skills" / "infrascope_skill").mkdir(parents=True, exist_ok=True)

    class _Paths:
        def skills_workspace_dir(self):
            return tmp_path / "workspace" / "skills"

        def repo_root(self):
            return tmp_path

    ctx = SimpleNamespace(
        skills_repo=None,
        sql=None,
        git=None,
        paths=_Paths(),
        caps=None,
        settings=None,
        bus=None,
    )

    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            self.ready = False

        def runtime_status(self, _name: str) -> dict[str, object]:
            if not self.ready:
                raise RuntimeError("no versions installed")
            return {"ready": True}

        def runtime_update(self, name: str, *, space: str = "workspace") -> dict[str, object]:
            calls.append(f"update:{name}:{space}")
            return {"ok": False, "reason": "no_active_runtime"}

        def activate_for_space(
            self,
            name: str,
            *,
            space: str = "default",
            webspace_id: str | None = None,
            version: str | None = None,
            slot: str | None = None,
        ) -> str:
            calls.append(f"activate:{name}:{space}:{webspace_id}:{version}:{slot}")
            self.ready = True
            return "A"

        def run_tool(self, skill_name: str, tool_name: str, payload: dict[str, object], timeout: float | None = None) -> dict[str, object]:
            calls.append(f"run:{self.ready}:{skill_name}:{tool_name}:{timeout}")
            if not self.ready:
                raise RuntimeError("no versions installed")
            return {"skill": skill_name, "tool": tool_name, "payload": payload}

    async def _fake_run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(tool_bridge_module, "is_accepting_new_work", lambda: True)
    monkeypatch.setattr(tool_bridge_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(tool_bridge_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_bridge_module, "attach_http_trace_headers", lambda _req, _resp: "trace-123")
    monkeypatch.setattr(tool_bridge_module.anyio.to_thread, "run_sync", _fake_run_sync)
    monkeypatch.setattr(tool_bridge_module, "default_webspace_id", lambda: "default")

    result = asyncio.run(
        tool_bridge_module.call_tool(
            tool_bridge_module.ToolCall(
                tool="infrascope_skill:get_overview_summary",
                arguments={"webspace_id": "ws-1"},
            ),
            SimpleNamespace(headers={}),
            Response(),
            ctx=ctx,
        )
    )

    assert result["ok"] is True
    assert result["trace_id"] == "trace-123"
    assert calls == [
        "run:False:infrascope_skill:get_overview_summary:None",
        "update:infrascope_skill:workspace",
        "activate:infrascope_skill:default:ws-1:None:None",
        "run:True:infrascope_skill:get_overview_summary:None",
    ]


def test_call_tool_returns_gateway_timeout_when_worker_times_out(monkeypatch) -> None:
    class _FakeSkillManager:
        def __init__(self, **_kwargs) -> None:
            return None

    async def _fake_run_sync(_func, *args, **kwargs):
        raise TimeoutError("tool 'prompt_list_project_objects' timed out after 30 seconds")

    monkeypatch.setattr(tool_bridge_module, "is_accepting_new_work", lambda: True)
    monkeypatch.setattr(tool_bridge_module, "SkillManager", _FakeSkillManager)
    monkeypatch.setattr(tool_bridge_module, "SqliteSkillRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_bridge_module, "attach_http_trace_headers", lambda _req, _resp: "trace-123")
    monkeypatch.setattr(tool_bridge_module.anyio.to_thread, "run_sync", _fake_run_sync)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            tool_bridge_module.call_tool(
                tool_bridge_module.ToolCall(tool="prompt_engineer_skill:prompt_list_project_objects", arguments={}),
                SimpleNamespace(headers={}),
                Response(),
                ctx=_fake_ctx(),
            )
        )

    assert excinfo.value.status_code == 504
    assert "timed out" in str(excinfo.value.detail)
