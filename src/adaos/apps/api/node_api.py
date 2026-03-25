from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from adaos.apps.api.auth import ensure_token, require_token, resolve_presented_token
from adaos.services.bootstrap import is_ready, load_config, request_hub_root_reconnect, switch_role
from adaos.services.media_library import (
    ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
    guess_media_type,
    list_media_files,
    media_capabilities,
    media_file_path,
    media_snapshot,
)
from adaos.services.node_config import set_node_names as save_node_names_config
from adaos.services.reliability import reliability_snapshot, yjs_sync_runtime_snapshot
from adaos.services.scenario.webspace_runtime import reload_webspace_from_scenario
from adaos.services.realtime_sidecar import (
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
)
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.yjs.store import get_ystore_for_webspace

router = APIRouter()


class NodeStatus(BaseModel):
    node_id: str
    subnet_id: str
    role: str
    node_names: list[str] = Field(default_factory=list)
    primary_node_name: str = ""
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


class SidecarRestartRequest(BaseModel):
    reconnect_hub_root: bool = True


class NodeNamesUpdateRequest(BaseModel):
    node_names: list[str] | None = None
    value: str | None = None


class MemberUpdateRequest(BaseModel):
    action: str = Field(..., pattern="^(update|start|cancel|rollback)$")
    target_rev: str | None = None
    target_version: str | None = None
    countdown_sec: float | None = None
    drain_timeout_sec: float | None = None
    signal_delay_sec: float | None = None
    reason: str | None = None


class WebspaceYjsActionRequest(BaseModel):
    scenario_id: str | None = None


def _raise_400(detail: str) -> None:
    raise HTTPException(status_code=400, detail=detail)


async def _require_request_token(
    request: Request,
    *,
    authorization: str | None = Header(default=None),
    x_adaos_token: str | None = Header(default=None),
) -> None:
    ensure_token(
        resolve_presented_token(
            x_adaos_token=x_adaos_token,
            authorization=authorization,
            query_token=str(request.query_params.get("token") or "").strip() or None,
        )
    )


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
        node_names=list(getattr(conf, "node_names", []) or []),
        primary_node_name=str(getattr(conf, "primary_node_name", "") or ""),
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
        node_names=list(getattr(conf, "node_names", []) or []),
    )


@router.post("/hub-root/reconnect", dependencies=[Depends(require_token)])
async def hub_root_reconnect(payload: HubRootReconnectRequest) -> dict[str, Any]:
    return await request_hub_root_reconnect(transport=payload.transport, url_override=payload.url_override)


@router.get("/sidecar/status", dependencies=[Depends(require_token)])
async def sidecar_status(request: Request) -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = _route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    reliability = reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        local_ready=is_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
        node_names=list(getattr(conf, "node_names", []) or []),
    )
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    process = realtime_sidecar_listener_snapshot(getattr(request.app.state, "realtime_sidecar_proc", None))
    return {
        "ok": True,
        "runtime": runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {},
        "process": process,
    }


@router.post("/sidecar/restart", dependencies=[Depends(require_token)])
async def sidecar_restart(request: Request, payload: SidecarRestartRequest) -> dict[str, Any]:
    conf = load_config()
    proc = getattr(request.app.state, "realtime_sidecar_proc", None)
    new_proc, restart_result = await restart_realtime_sidecar_subprocess(proc=proc, role=conf.role)
    request.app.state.realtime_sidecar_proc = new_proc
    reconnect_result: dict[str, Any] | None = None
    if bool(payload.reconnect_hub_root) and str(conf.role or "").strip().lower() == "hub":
        reconnect_result = await request_hub_root_reconnect()
    route_mode, connected = _route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    reliability = reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        local_ready=is_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
        node_names=list(getattr(conf, "node_names", []) or []),
    )
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    return {
        "ok": True,
        "restart": restart_result,
        "reconnect": reconnect_result,
        "runtime": runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {},
        "process": realtime_sidecar_listener_snapshot(new_proc),
    }


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
            node_names=list(getattr(conf, "node_names", []) or []),
            primary_node_name=str(getattr(conf, "primary_node_name", "") or ""),
            ready=is_ready(),
            node_state=str(runtime_lifecycle_snapshot().get("node_state") or "ready"),
            draining=bool(runtime_lifecycle_snapshot().get("draining")),
            route_mode=route_mode,
            connected_to_hub=connected,
        ),
        diagnostics=diags,
    )


@router.get("/names", dependencies=[Depends(require_token)])
async def node_names() -> dict[str, Any]:
    conf = load_config()
    return {
        "ok": True,
        "node_id": conf.node_id,
        "role": conf.role,
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
    }


@router.post("/names", dependencies=[Depends(require_token)])
async def update_node_names(payload: NodeNamesUpdateRequest) -> dict[str, Any]:
    source = payload.node_names if payload.node_names is not None else payload.value
    conf = save_node_names_config(source)
    return {
        "ok": True,
        "node_id": conf.node_id,
        "role": conf.role,
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
    }


@router.get("/yjs/runtime", dependencies=[Depends(require_token)])
async def node_yjs_runtime() -> dict[str, Any]:
    conf = load_config()
    return {
        "ok": True,
        "runtime": yjs_sync_runtime_snapshot(role=conf.role),
    }


@router.post("/yjs/webspaces/{webspace_id}/backup", dependencies=[Depends(require_token)])
async def node_yjs_backup(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    store = get_ystore_for_webspace(str(webspace_id or "default") or "default")
    await store.backup_to_disk()
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": str(webspace_id or "default") or "default",
        "runtime": yjs_sync_runtime_snapshot(role=conf.role),
    }


@router.post("/yjs/webspaces/{webspace_id}/reload", dependencies=[Depends(require_token)])
async def node_yjs_reload(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    result = await reload_webspace_from_scenario(
        str(webspace_id or "default") or "default",
        scenario_id=str(payload.scenario_id or "").strip() or None,
        action="reload",
    )
    result["runtime"] = yjs_sync_runtime_snapshot(role=conf.role)
    return result


@router.post("/yjs/webspaces/{webspace_id}/reset", dependencies=[Depends(require_token)])
async def node_yjs_reset(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    result = await reload_webspace_from_scenario(
        str(webspace_id or "default") or "default",
        scenario_id=str(payload.scenario_id or "").strip() or None,
        action="reset",
    )
    result["runtime"] = yjs_sync_runtime_snapshot(role=conf.role)
    return result


@router.get("/media/files", dependencies=[Depends(require_token)])
async def list_media_library() -> dict[str, Any]:
    snapshot = media_snapshot()
    snapshot["proxy_limits"] = {
        "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
    }
    return snapshot


@router.put("/media/files/{filename}", dependencies=[Depends(require_token)])
async def upload_media_file(filename: str, request: Request) -> dict[str, Any]:
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))

    replaced = target.exists()
    tmp_path = target.with_name(f"{target.name}.upload-{os.getpid()}-{id(request)}.part")
    total_bytes = 0
    try:
        with tmp_path.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                handle.write(chunk)
                total_bytes += len(chunk)
        tmp_path.replace(target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return {
        "ok": True,
        "filename": target.name,
        "size_bytes": total_bytes,
        "mime_type": guess_media_type(target.name),
        "replaced": replaced,
    }


@router.delete("/media/files/{filename}", dependencies=[Depends(require_token)])
async def delete_media_file(filename: str) -> dict[str, Any]:
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))
    existed = target.exists()
    if existed:
        target.unlink()
    return {
        "ok": True,
        "filename": target.name,
        "deleted": existed,
        "items": list_media_files(),
    }


@router.get("/media/files/content/{filename}")
async def media_file_content(
    filename: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_adaos_token: str | None = Header(default=None),
):
    await _require_request_token(
        request,
        authorization=authorization,
        x_adaos_token=x_adaos_token,
    )
    try:
        target = media_file_path(filename)
    except ValueError as exc:
        _raise_400(str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="media_file_not_found")
    return FileResponse(
        path=target,
        media_type=guess_media_type(target.name),
        filename=target.name,
    )


@router.get("/members", dependencies=[Depends(require_token)])
async def node_members() -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = _route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    reliability = reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        local_ready=is_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
        node_names=list(getattr(conf, "node_names", []) or []),
    )
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    return {
        "ok": True,
        "hub_member_connection_state": (
            runtime.get("hub_member_connection_state")
            if isinstance(runtime.get("hub_member_connection_state"), dict)
            else {}
        ),
    }


@router.post("/members/{node_id}/snapshot/request", dependencies=[Depends(require_token)])
async def request_member_snapshot(node_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "node_id": node_id,
            "error": "hub_role_required",
        }
    from adaos.services.subnet.link_manager import get_hub_link_manager

    return await get_hub_link_manager().request_member_snapshot(node_id, reason="node_api")


@router.post("/members/{node_id}/update", dependencies=[Depends(require_token)])
async def request_member_update(node_id: str, payload: MemberUpdateRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "node_id": node_id,
            "error": "hub_role_required",
        }
    action = "update" if str(payload.action or "").strip().lower() == "start" else str(payload.action or "").strip().lower()
    from adaos.services.subnet.link_manager import get_hub_link_manager

    return await get_hub_link_manager().request_member_update(
        node_id,
        action=action,
        target_rev=str(payload.target_rev or ""),
        target_version=str(payload.target_version or ""),
        countdown_sec=payload.countdown_sec,
        drain_timeout_sec=payload.drain_timeout_sec,
        signal_delay_sec=payload.signal_delay_sec,
        reason=str(payload.reason or "node_api.member_update"),
    )
