from __future__ import annotations
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime
import uuid

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx, AgentContext
from adaos.services.scenario.manager import ScenarioManager
from adaos.adapters.db import SqliteScenarioRegistry


router = APIRouter(tags=["scenarios"], dependencies=[Depends(require_token)])


# --- Модели для запуска сценариев -------------------------------------------

class ExecutionPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class ScenarioRunRequest(BaseModel):
    id: str = Field(..., description="ID сценария для запуска")
    ctx: Optional[Dict[str, Any]] = Field(None, description="Контекст выполнения")
    priority: ExecutionPriority = Field(
        ExecutionPriority.NORMAL, 
        description="Приоритет выполнения: low, normal, high"
    )
    force: bool = Field(
        False, 
        description="Принудительный запуск (игнорировать проверку установленности)"
    )


class ScenarioRunResponse(BaseModel):
    run_id: str = Field(..., description="Идентификатор запуска для отслеживания")
    scenario_id: str = Field(..., description="ID запущенного сценария")
    status: str = Field("pending", description="Текущий статус выполнения")
    created_at: datetime = Field(..., description="Время создания запуска")
    priority: ExecutionPriority = Field(..., description="Приоритет выполнения")


class ScenarioStatusResponse(BaseModel):
    run_id: str = Field(..., description="Идентификатор запуска")
    scenario_id: str = Field(..., description="ID сценария")
    state: RunState = Field(..., description="Текущее состояние выполнения")
    step: Optional[str] = Field(None, description="Текущий выполняемый шаг")
    started_at: Optional[datetime] = Field(None, description="Время начала выполнения")
    finished_at: Optional[datetime] = Field(None, description="Время завершения")
    error: Optional[str] = Field(None, description="Ошибка выполнения, если есть")
    result: Optional[Dict[str, Any]] = Field(None, description="Результат выполнения")


class ScenarioCancelRequest(BaseModel):
    run_id: str = Field(..., description="ID запуска для отмены")


class ScenarioCancelResponse(BaseModel):
    success: bool = Field(..., description="Успешность отмены")
    message: str = Field(..., description="Сообщение о результате")


# --- DI: получаем менеджер так же, как в CLI ---------------------------------
def _get_manager(ctx: AgentContext = Depends(get_ctx)) -> ScenarioManager:
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


# --- API для запуска сценариев -----------------------------------------------

@router.post("/run", response_model=ScenarioRunResponse)
async def run_scenario(
    request: ScenarioRunRequest,
    background_tasks: BackgroundTasks,
    mgr: ScenarioManager = Depends(_get_manager)
):
    """
    Запуск выполнения сценария
    
    Параметры:
    - `id`: ID сценария (например, "s1")
    - `ctx`: Контекст выполнения (переменные для подстановки в сценарий)
    - `priority`: Приоритет выполнения (low, normal, high)
    - `force`: Принудительный запуск (игнорировать проверку установленности)
    
    Возвращает `run_id` для отслеживания статуса выполнения.
    """
    try:
        # Запускаем сценарий через менеджер
        run_id = await mgr.run_scenario(
            scenario_id=request.id,
            ctx=request.ctx or {},
            priority=request.priority,
            force=request.force
        )
        
        # Получаем статус для ответа
        status_unit = await mgr.get_scenario_status(run_id)
        if not status_unit:
            raise HTTPException(
                status_code=500,
                detail="Failed to get scenario status after launch"
            )
        
        return ScenarioRunResponse(
            run_id=run_id,
            scenario_id=request.id,
            status=status_unit.state.value,
            created_at=datetime.utcnow(),
            priority=request.priority
        )
        
    except ValueError as e:
        # Ошибка валидации (сценарий не найден, не установлен и т.д.)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Внутренняя ошибка сервера
        raise HTTPException(status_code=500, detail=f"Failed to run scenario: {str(e)}")


@router.get("/status", response_model=ScenarioStatusResponse)
async def get_scenario_status(
    run_id: str,
    mgr: ScenarioManager = Depends(_get_manager)
):
    """
    Получение статуса выполнения сценария
    
    Параметры:
    - `run_id`: Идентификатор запуска, полученный при вызове `/run`
    
    Возвращает текущее состояние выполнения, текущий шаг, ошибки и результат.
    """
    try:
        unit = await mgr.get_scenario_status(run_id)
        if not unit:
            raise HTTPException(
                status_code=404,
                detail=f"Scenario run with ID '{run_id}' not found"
            )
        
        return ScenarioStatusResponse(
            run_id=unit.run_id,
            scenario_id=unit.scenario_id,
            state=unit.state,
            step=unit.current_step,
            started_at=unit.started_at,
            finished_at=unit.finished_at,
            error=unit.error,
            result=unit.result
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get scenario status: {str(e)}")


@router.post("/cancel", response_model=ScenarioCancelResponse)
async def cancel_scenario(
    request: ScenarioCancelRequest,
    mgr: ScenarioManager = Depends(_get_manager)
):
    """
    Отмена выполнения сценария
    
    Параметры:
    - `run_id`: Идентификатор запуска для отмены
    
    Возвращает результат попытки отмены.
    """
    try:
        success = await mgr.cancel_scenario(request.run_id)
        
        if success:
            return ScenarioCancelResponse(
                success=True,
                message=f"Scenario run '{request.run_id}' cancelled successfully"
            )
        else:
            # Сценарий мог быть уже завершен или не найден
            return ScenarioCancelResponse(
                success=False,
                message=f"Scenario run '{request.run_id}' not found or already completed"
            )
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel scenario: {str(e)}")





# --- DI: получаем менеджер так же, как в CLI ---------------------------------
def _get_manager(ctx: AgentContext = Depends(get_ctx)) -> ScenarioManager:
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


# --- helpers -----------------------------------------------------------------
def _to_mapping(obj: Any) -> Dict[str, Any]:
    # sqlite3.Row, NamedTuple, dataclass, simple objects — мягкая нормализация
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        return obj._asdict()  # type: ignore[attr-defined]
    except Exception:
        pass
    d: Dict[str, Any] = {}
    for k in ("name", "pin", "last_updated", "id", "path", "version"):
        if hasattr(obj, k):
            v = getattr(obj, k)
            # id может быть сложным типом
            if k == "id":
                if hasattr(v, "value"):
                    v = getattr(v, "value")
                else:
                    v = str(v)
            d[k] = v
    return d or {"repr": repr(obj)}


def _meta_id(meta: Any) -> str:
    mid = getattr(meta, "id", None)
    if mid is None:
        return str(meta)
    return getattr(mid, "value", str(mid))


# --- API (тонкий фасад CLI) --------------------------------------------------
class InstallReq(BaseModel):
    name: str
    pin: Optional[str] = None


class PushReq(BaseModel):
    name: str
    message: str
    signoff: bool = False

class UninstallReq(BaseModel):
    name: str


@router.get("/list")
async def list_scenarios(fs: bool = False, mgr: ScenarioManager = Depends(_get_manager)):
    rows = mgr.list_installed()
    items = [_to_mapping(r) for r in (rows or [])]
    result: Dict[str, Any] = {"items": items}
    if fs:
        present = {_meta_id(m) for m in mgr.list_present()}
        desired = {(i.get("name") or i.get("id") or i.get("repr")) for i in items}
        missing = sorted(desired - present)
        extra = sorted(present - desired)
        result["fs"] = {
            "present": sorted(present),
            "missing": missing,
            "extra": extra,
        }
    return result


@router.post("/sync")
async def sync(mgr: ScenarioManager = Depends(_get_manager)):
    mgr.sync()
    return {"ok": True}


@router.post("/install")
async def install(body: InstallReq, mgr: ScenarioManager = Depends(_get_manager)):
    meta = mgr.install(body.name, pin=body.pin)
    # приведём к компактному виду как в CLI-эхо
    return {
        "ok": True,
        "scenario": {
            "id": _meta_id(meta),
            "version": getattr(meta, "version", None),
            "path": str(getattr(meta, "path", "")),
        },
    }


@router.delete("/{name}")
async def remove(name: str, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.uninstall(name)
    return {"ok": True}

@router.post("/uninstall")
async def uninstall(body: UninstallReq, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.uninstall(body.name)
    return {"ok": True}


@router.post("/push")
async def push(body: PushReq, mgr: ScenarioManager = Depends(_get_manager)):
    revision = mgr.push(body.name, body.message, signoff=body.signoff)
    return {"ok": True, "revision": revision}
