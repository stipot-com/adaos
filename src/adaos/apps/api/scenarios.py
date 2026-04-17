from __future__ import annotations
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx, AgentContext
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.operations import submit_install_operation
from adaos.services.yjs.webspace import default_webspace_id


router = APIRouter(tags=["scenarios"], dependencies=[Depends(require_token)])


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
    async_operation: bool = False
    webspace_id: str | None = None


class PushReq(BaseModel):
    name: str
    message: str
    signoff: bool = False

class UninstallReq(BaseModel):
    name: str
    webspace_id: str | None = None


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
    if body.async_operation:
        operation = submit_install_operation(
            target_kind="scenario",
            target_id=body.name,
            webspace_id=body.webspace_id,
        )
        return {
            "ok": True,
            "accepted": True,
            "operation_id": operation["operation_id"],
            "operation": operation,
        }
    webspace_id = body.webspace_id or default_webspace_id()
    meta = mgr.install_with_deps(body.name, pin=body.pin, webspace_id=webspace_id)
    try:
        await rebuild_webspace_from_sources(
            webspace_id,
            action="scenario_install_sync",
            scenario_id=body.name,
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
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
    try:
        await rebuild_webspace_from_sources(
            default_webspace_id(),
            action="scenario_uninstall_sync",
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    return {"ok": True}

@router.post("/uninstall")
async def uninstall(body: UninstallReq, mgr: ScenarioManager = Depends(_get_manager)):
    mgr.uninstall(body.name)
    try:
        await rebuild_webspace_from_sources(
            body.webspace_id or default_webspace_id(),
            action="scenario_uninstall_sync",
            source_of_truth="scenario_projection",
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/push")
async def push(body: PushReq, mgr: ScenarioManager = Depends(_get_manager)):
    revision = mgr.push(body.name, body.message, signoff=body.signoff)
    return {"ok": True, "revision": revision}
