# src\adaos\apps\api\subnet_api.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Any, Dict

from adaos.apps.api.auth import require_token
from adaos.services.node_config import load_config
from adaos.services.subnet_kv_file_http import get_subnet_kv
from adaos.services.subnet_registry_mem import LEASE_SECONDS_DEFAULT, DOWN_GRACE_SECONDS
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.subnet_registry_mem import get_subnet_registry

from adaos.sdk.data import bus

router = APIRouter(tags=["subnet"])


# ---------- Models ----------
class RegisterRequest(BaseModel):
    node_id: str
    subnet_id: str
    hostname: str | None = None
    roles: list[str] | None = None
    base_url: str | None = None
    capacity: Dict[str, Any] | None = None


class RegisterResponse(BaseModel):
    ok: bool
    lease_seconds: int = LEASE_SECONDS_DEFAULT


class HeartbeatRequest(BaseModel):
    node_id: str
    capacity: Dict[str, Any] | None = None


class HeartbeatResponse(BaseModel):
    ok: bool
    lease_seconds: int = LEASE_SECONDS_DEFAULT


class CtxValue(BaseModel):
    value: Any


class DeregisterRequest(BaseModel):
    node_id: str


# ---------- Endpoints (hub-only, mounted under /api) ----------


@router.post("/subnet/register", response_model=RegisterResponse, dependencies=[Depends(require_token)])
async def register(body: RegisterRequest):
    """
    Регистрация ноды на hub.
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts registrations")

    if body.subnet_id != conf.subnet_id:
        raise HTTPException(status_code=400, detail="subnet mismatch")

    # Добавляем/обновляем запись в persistent directory
    directory = get_directory()
    directory.on_register(
        {
            "node_id": body.node_id,
            "subnet_id": body.subnet_id,
            "hostname": body.hostname,
            "roles": body.roles or [],
            "base_url": body.base_url,
            "capacity": body.capacity or {},
        }
    )
    # Сигнализируем о появлении ноды (node.up)
    try:
        await bus.emit("net.subnet.node.up", {"node_id": body.node_id}, source="subnet_api", actor="system")
    except Exception:
        pass

    return RegisterResponse(ok=True, lease_seconds=LEASE_SECONDS_DEFAULT)


@router.post("/subnet/heartbeat", response_model=HeartbeatResponse, dependencies=[Depends(require_token)])
async def heartbeat(body: HeartbeatRequest):
    """
    Heartbeat от ноды к hub. Обновляет last_seen и (если надо) возвращает статус в 'up'.
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts heartbeats")

    directory = get_directory()
    # Если нода неизвестна — 404 (сохраняем поведение)
    if not directory.repo.get_node(body.node_id):
        raise HTTPException(status_code=404, detail="node not registered")
    directory.on_heartbeat(body.node_id, body.capacity or None)
    return HeartbeatResponse(ok=True, lease_seconds=LEASE_SECONDS_DEFAULT)


@router.post("/subnet/deregister", dependencies=[Depends(require_token)])
async def deregister(body: DeregisterRequest):
    """Корректная дерегистрация ноды на hub (когда нода уходит из подсети)."""
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts deregistration")
    existed = get_subnet_registry().unregister_node(body.node_id)
    if existed:
        await bus.emit("net.subnet.node.down", {"node_id": body.node_id}, source="subnet_api", actor="system")
    return {"ok": True, "existed": bool(existed)}


@router.get("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_get(key: str):
    """
    Получение значения глобального контекста подсети (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node serves context")
    return {"ok": True, "value": CTX.hub_get(key)}


@router.put("/subnet/context/{key}", dependencies=[Depends(require_token)])
async def ctx_set(key: str, body: CtxValue):
    """
    Запись значения в глобальный контекст подсети (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node serves context")
    CTX.hub_set(key, body.value)
    return {"ok": True}


@router.get("/subnet/nodes", dependencies=[Depends(require_token)])
async def nodes_list():
    """
    Список нод подсети с их статусами (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node lists nodes")
    items = get_directory().list_known_nodes()
    return {"ok": True, "nodes": items}


@router.get("/subnet/nodes/{node_id}", dependencies=[Depends(require_token)])
async def node_get(node_id: str):
    """
    Детали по конкретной ноде (hub-only).
    """
    conf = load_config()
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node has node details")
    directory = get_directory()
    info = directory.repo.get_node(node_id)
    if not info:
        raise HTTPException(status_code=404, detail="node not found")
    node = dict(info)
    node["online"] = directory.is_online(node_id)
    return {"ok": True, "node": node}
