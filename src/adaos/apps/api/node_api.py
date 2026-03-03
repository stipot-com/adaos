# src/adaos/api/node_api.py
# TODO Вместо захардкоженного "dev-local-token" можно дернуть требуемый токен из единого места (например, from adaos.seervice.node_config import load_config и взять load_config().token). Главное — чтобы токен, с которым member ходит на hub, совпадал с тем, который hub ожидает.
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from typing import Optional

from adaos.apps.api.auth import require_token
from adaos.services.bootstrap import is_ready, switch_role, load_config
from adaos.services.subnet.link_client import get_member_link_client

router = APIRouter()


class NodeStatus(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    ready: bool
    route_mode: Optional[str] = None
    connected_to_hub: Optional[bool] = None


class RoleChangeRequest(BaseModel):
    role: str = Field(..., pattern="^(hub|member)$")
    hub_url: Optional[str] = None  # deprecated; ignored
    subnet_id: Optional[str] = None


class RoleChangeResponse(BaseModel):
    ok: bool
    node: NodeStatus
    diagnostics: dict


@router.get("/status", response_model=NodeStatus, dependencies=[Depends(require_token)])
async def node_status():
    conf = load_config()
    route_mode = None
    connected = None
    try:
        if conf.role == "hub":
            route_mode = "hub"
        elif conf.role == "member":
            connected = bool(get_member_link_client().is_connected())
            route_mode = "ws" if connected else "none"
    except Exception:
        route_mode = None
        connected = None
    return NodeStatus(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        ready=is_ready(),
        route_mode=route_mode,
        connected_to_hub=connected,
    )


@router.post("/role", response_model=RoleChangeResponse, dependencies=[Depends(require_token)])
async def node_change_role(req: Request, payload: RoleChangeRequest):
    """
    Переключение роли узла на лету.
    Для role=member обязателен hub_url.
    Можно (опционально) передать subnet_id для миграции в другую подсеть.
    """
    new_role = payload.role.lower()
    # New behavior: hub_url is deprecated and ignored (kept for backward-compatibility only).
    new_role = new_role.strip()
    sub_id = payload.subnet_id
    deprecated_fields: list[str] = []
    if payload.hub_url:
        deprecated_fields.append("hub_url")

    conf = await switch_role(req.app, new_role, hub_url=None, subnet_id=sub_id)

    route_mode = None
    connected = None
    try:
        if conf.role == "hub":
            route_mode = "hub"
        elif conf.role == "member":
            connected = bool(get_member_link_client().is_connected())
            route_mode = "ws" if connected else "none"
    except Exception:
        route_mode = None
        connected = None

    diags = {
        "requested_role": new_role,
        "subnet_id_used": sub_id,
        "now_ready": is_ready(),
        "route_mode": route_mode,
        "connected_to_hub": connected,
        "deprecated_fields": deprecated_fields,
    }
    return RoleChangeResponse(
        ok=True,
        node=NodeStatus(
            node_id=conf.node_id,
            subnet_id=conf.subnet_id,
            role=conf.role,
            ready=is_ready(),
            route_mode=route_mode,
            connected_to_hub=connected,
        ),
        diagnostics=diags,
    )
    if new_role == "member" and not payload.hub_url:
        raise HTTPException(status_code=400, detail="hub_url is required for role=member")

    # если уходим в member и subnet_id не указан — попробуем подтянуть его у хаба
    sub_id = payload.subnet_id
    if new_role == "member" and not sub_id:
        try:
            # используем токен из нашего конфига
            token = load_config().token
            r = requests.get(payload.hub_url.rstrip("/") + "/api/node/status", headers={"X-AdaOS-Token": token}, timeout=3)
            if r.status_code == 200:
                sub_id = (r.json() or {}).get("subnet_id")
        except Exception:
            pass
        if not sub_id:
            raise HTTPException(status_code=400, detail="subnet_id is required (could not fetch from hub)")

    # достаём app из Request
    app = req.app

    conf = await switch_role(app, new_role, hub_url=payload.hub_url, subnet_id=sub_id)
    diags = {
        "requested_role": new_role,
        "hub_url": payload.hub_url,
        "subnet_id_used": sub_id,
        "now_ready": is_ready(),
    }
    return RoleChangeResponse(
        ok=True,
        node=NodeStatus(
            node_id=conf.node_id,
            subnet_id=conf.subnet_id,
            role=conf.role,
            hub_url=conf.hub_url,
            ready=is_ready(),
        ),
        diagnostics=diags,
    )
