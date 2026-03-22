from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from adaos.apps.api.auth import require_token
from adaos.services.bootstrap import is_ready, load_config, request_hub_root_reconnect, switch_role
from adaos.services.reliability import reliability_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client

router = APIRouter()


class NodeStatus(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    ready: bool
    node_state: str = "ready"
    draining: bool = False
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


class HubRootReconnectRequest(BaseModel):
    transport: Optional[str] = Field(None, pattern="^(ws|tcp|nats)?$")
    url_override: Optional[str] = None


def _route_info(role: str) -> tuple[str | None, bool | None]:
    route_mode = None
    connected = None
    try:
        if role == "hub":
            route_mode = "hub"
        elif role == "member":
            connected = bool(get_member_link_client().is_connected())
            route_mode = "ws" if connected else "none"
    except Exception:
        route_mode = None
        connected = None
    return route_mode, connected


@router.get("/status", response_model=NodeStatus, dependencies=[Depends(require_token)])
async def node_status():
    conf = load_config()
    route_mode, connected = _route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    return NodeStatus(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        ready=is_ready() and not bool(lifecycle.get("draining")),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
    )


@router.get("/reliability", dependencies=[Depends(require_token)])
async def node_reliability() -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = _route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    local_ready = is_ready()
    return reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        local_ready=local_ready,
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
    )


@router.post("/hub-root/reconnect", dependencies=[Depends(require_token)])
async def hub_root_reconnect(payload: HubRootReconnectRequest) -> dict[str, Any]:
    return await request_hub_root_reconnect(transport=payload.transport, url_override=payload.url_override)


@router.post("/role", response_model=RoleChangeResponse, dependencies=[Depends(require_token)])
async def node_change_role(req: Request, payload: RoleChangeRequest):
    """
    Switch local node role.

    Backward-compatibility: `hub_url` is accepted but ignored (deprecated).
    """
    new_role = payload.role.lower().strip()
    sub_id = payload.subnet_id
    deprecated_fields: list[str] = ["hub_url"] if payload.hub_url else []

    conf = await switch_role(req.app, new_role, hub_url=None, subnet_id=sub_id)
    route_mode, connected = _route_info(conf.role)

    diags = {
        "requested_role": new_role,
        "subnet_id_used": sub_id,
        "now_ready": is_ready(),
        "node_state": runtime_lifecycle_snapshot().get("node_state", "ready"),
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
            node_state=str(runtime_lifecycle_snapshot().get("node_state") or "ready"),
            draining=bool(runtime_lifecycle_snapshot().get("draining")),
            route_mode=route_mode,
            connected_to_hub=connected,
        ),
        diagnostics=diags,
    )
