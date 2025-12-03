# src\adaos\api\tool_bridge.py
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Any, Dict
import requests, os

from adaos.apps.api.auth import require_token
from adaos.services.observe import attach_http_trace_headers
from adaos.services.agent_context import get_ctx, AgentContext
from adaos.services.eventbus import emit
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.agent_context import get_ctx
from adaos.skills.runtime_runner import execute_tool


router = APIRouter()


class ToolCall(BaseModel):
    """
    Вызов инструмента навыка:
      tool: "<skill_name>:<public_tool_name>"
      arguments: {...}  # опционально
      context:   {...}  # опционально (резерв на будущее)
    """

    tool: str
    arguments: Dict[str, Any] | None = None
    context: Dict[str, Any] | None = None
    timeout: float | None = Field(default=None)
    dev: bool = Field(default=False, description="Run tool from DEV workspace instead of installed runtime")
    model_config = {"extra": "ignore"}


@router.post("/tools/call", dependencies=[Depends(require_token)])
async def call_tool(body: ToolCall, request: Request, response: Response, ctx: AgentContext = Depends(get_ctx)):
    # Разбираем "<skill_name>:<public_tool_name>"
    if ":" not in body.tool:
        raise HTTPException(status_code=400, detail="tool must be in '<skill_name>:<public_tool_name>' format")

    skill_name, public_tool = body.tool.split(":", 1)
    if not skill_name or not public_tool:
        raise HTTPException(status_code=400, detail="invalid tool spec")

    # Используем общий путь исполнения как в CLI (SkillManager.run_tool)
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )

    trace = attach_http_trace_headers(request.headers, response.headers)
    payload: Dict[str, Any] = body.arguments or {}
    # Пробуем локально; если навык отсутствует на узле-хабе — проксируем на member
    try:
        if body.dev:
            result = mgr.run_dev_tool(skill_name, public_tool, payload, timeout=body.timeout)
        else:
            result = mgr.run_tool(skill_name, public_tool, payload, timeout=body.timeout)
    except (FileNotFoundError, RuntimeError, KeyError) as e:
        # Если локально не найден навык/слот — попробуем проксировать на участника подсети (только если роль hub)
        try:
            conf = get_ctx().config
        except Exception:
            conf = None
        if not conf or conf.role != "hub":
            # На member нет прокси — вернём исходную ошибку
            raise HTTPException(status_code=404, detail=str(e))

        # Найти online-ноду с этим skill (используем только runtime; workspace-fallback отключён)
        directory = get_directory()
        candidates = directory.find_nodes_with_skill(skill_name, require_online=True)
        # Сначала активные, затем по last_seen убыв.
        candidates.sort(key=lambda n: (not bool(n.get("active"))), reverse=False)
        if not candidates:
            raise HTTPException(
                status_code=503,
                detail=f"skill '{skill_name}', tool '{public_tool}' is not available online in the subnet. In dev: {body.dev}. Candidates: {candidates}. Err: {str(e)}",
            )
        target = candidates[0]
        base_url = target.get("base_url") or directory.get_node_base_url(target.get("node_id", ""))
        if not base_url:
            raise HTTPException(status_code=503, detail="no base_url for target node")

        # Проксируем запрос прозрачно
        url = f"{base_url.rstrip('/')}/api/tools/call"
        forward = {"tool": body.tool, "arguments": payload}
        if body.timeout is not None:
            forward["timeout"] = body.timeout
        # сохраняем dev-флаг при прокси, если он был указан
        if body.dev:
            forward["dev"] = True
        token = conf.token or request.headers.get("X-AdaOS-Token") or "dev-local-token"
        try:
            r = requests.post(url, json=forward, headers={"X-AdaOS-Token": token, "Content-Type": "application/json"}, timeout=(body.timeout or 10) + 2)
        except Exception as pe:
            raise HTTPException(status_code=502, detail=f"proxy failed: {pe}")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        try:
            result_payload = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail="invalid JSON from proxied node")
        # Возвращаем payload как есть от член-узла
        return result_payload
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"run failed: {type(e).__name__}: {e}")

    # Optional routing via local bus: publish ui.notify when result looks like plain text
    try:
        text: str | None = None
        if isinstance(result, str):
            text = result
        elif isinstance(result, dict):
            t = result.get("text") if hasattr(result, "get") else None
            if isinstance(t, str) and t.strip():
                text = t
        if text:
            emit(ctx.bus, "ui.notify", {"text": text}, actor="api.tools")
    except Exception:
        # best-effort: failure to route should not break API response
        pass

    return {"ok": True, "result": result, "trace_id": trace}
