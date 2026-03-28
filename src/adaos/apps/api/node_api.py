from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from adaos.domain import Event
from adaos.adapters.db import SqliteSkillRegistry
from adaos.apps.api.auth import ensure_token, require_token, resolve_presented_token
from adaos.services.agent_context import get_ctx
from adaos.services.bootstrap import is_ready, load_config, request_hub_root_reconnect, switch_role
from adaos.services.io_web.desktop import WebDesktopService
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
from adaos.services.scenario.webspace_runtime import (
    WebspaceService,
    describe_webspace_operational_state,
    describe_webspace_projection_state,
    ensure_dev_webspace_for_scenario,
    go_home_webspace,
    reload_webspace_from_scenario,
    restore_webspace_from_snapshot,
    switch_webspace_scenario,
)
from adaos.services.skill.manager import SkillManager
from adaos.services.realtime_sidecar import (
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
)
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.yjs.store import get_ystore_for_webspace

router = APIRouter()
_log = logging.getLogger("adaos.api.node_api")


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
    set_home: bool | None = None
    requested_id: str | None = None
    title: str | None = None


class WebspaceCreateRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    scenario_id: str | None = None
    dev: bool = False


class WebspaceUpdateRequest(BaseModel):
    title: str | None = None
    home_scenario: str | None = None


class WebspaceToggleInstallRequest(BaseModel):
    type: str = Field(..., pattern="^(app|widget)$")
    id: str = Field(..., min_length=1)


class InfrastateActionRequest(BaseModel):
    id: str = Field(..., min_length=1)
    webspace_id: str | None = None
    node_id: str | None = None
    value: str | None = None


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
async def node_yjs_runtime(webspace_id: str | None = None) -> dict[str, Any]:
    conf = load_config()
    return {
        "ok": True,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=str(webspace_id or "").strip() or None,
        ),
    }


@router.get("/infrastate/snapshot", dependencies=[Depends(require_token)])
async def node_infrastate_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "default").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    ctx = get_ctx()
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )

    def _fallback_snapshot(exc: Exception) -> dict[str, Any]:
        lifecycle = runtime_lifecycle_snapshot()
        yjs_runtime = yjs_sync_runtime_snapshot(
            role=str(getattr(conf, "role", "") or ""),
            webspace_id=target_webspace_id,
        )
        error_text = f"{type(exc).__name__}: {exc}"
        return {
            "summary": {
                "label": "Infra State",
                "value": str(lifecycle.get("node_state") or "degraded"),
                "subtitle": f"webspace {target_webspace_id}",
                "description": f"fallback snapshot: {error_text}",
                "updated_at": time.time(),
            },
            "actions": [],
            "update_actions": [],
            "nodes": [],
            "yjs_webspaces": [],
            "node_editor": {"names_csv": "", "editable": False, "scope": "fallback"},
            "build": [],
            "steps": [
                {
                    "id": "lifecycle",
                    "title": "Lifecycle",
                    "status": str(lifecycle.get("node_state") or "degraded"),
                    "description": str(lifecycle.get("reason") or "runtime fallback snapshot"),
                },
                {
                    "id": "yjs_runtime",
                    "title": "Yjs runtime",
                    "status": "ok" if yjs_runtime else "idle",
                    "description": str(
                        (yjs_runtime.get("assessment") or {}).get("state")
                        if isinstance(yjs_runtime, dict)
                        else "unknown"
                    ),
                },
            ],
            "realtime": [],
            "slots": [],
            "skills": [],
            "logs": [
                {
                    "id": "snapshot-error",
                    "title": "snapshot-error",
                    "status": "warn",
                    "preview": error_text,
                    "content": error_text,
                }
            ],
            "events": [],
            "lifecycle": lifecycle,
            "yjs_runtime": yjs_runtime,
            "last_refresh_ts": time.time(),
            "fallback": True,
            "errors": [error_text],
        }

    def _load_snapshot() -> dict[str, Any]:
        try:
            result = mgr.run_tool("infrastate_skill", "get_snapshot", {"webspace_id": target_webspace_id})
            return result if isinstance(result, dict) else {"summary": {}, "raw": result}
        except Exception as exc:
            _log.warning("node infrastate snapshot fallback webspace=%s", target_webspace_id, exc_info=True)
            return _fallback_snapshot(exc)

    snapshot = await anyio.to_thread.run_sync(_load_snapshot)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "degraded": bool(snapshot.get("fallback")) if isinstance(snapshot, dict) else False,
        "error": (snapshot.get("errors") or [None])[0] if isinstance(snapshot, dict) else None,
        "snapshot": snapshot,
    }


@router.post("/infrastate/action", dependencies=[Depends(require_token)])
async def node_infrastate_action(payload: InfrastateActionRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(payload.webspace_id or "default").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    ctx = get_ctx()
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )
    event_payload: dict[str, Any] = {
        "id": str(payload.id or "").strip(),
        "webspace_id": target_webspace_id,
    }
    node_id = str(payload.node_id or "").strip()
    value = payload.value
    if node_id:
        event_payload["node_id"] = node_id
    if value is not None:
        event_payload["value"] = value
    ctx.bus.publish(Event(type="infrastate.action", payload=event_payload, source="api.node", ts=time.time()))
    waiter = getattr(ctx.bus, "wait_for_idle", None)
    if callable(waiter):
        try:
            await waiter(timeout=2.5)
        except Exception:
            _log.debug("wait_for_idle failed after infrastate.action", exc_info=True)

    def _load_snapshot() -> dict[str, Any]:
        result = mgr.run_tool("infrastate_skill", "get_snapshot", {"webspace_id": target_webspace_id})
        return result if isinstance(result, dict) else {"summary": {}, "raw": result}

    snapshot = await anyio.to_thread.run_sync(_load_snapshot)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "action": event_payload["id"],
        "snapshot": snapshot,
    }


@router.get("/yjs/webspaces", dependencies=[Depends(require_token)])
async def node_yjs_webspaces() -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    items = [
        {
            "id": item.id,
            "title": item.title,
            "created_at": item.created_at,
            "kind": item.kind,
            "home_scenario": item.home_scenario,
            "source_mode": item.source_mode,
        }
        for item in WebspaceService().list(mode="mixed")
    ]
    return {
        "ok": True,
        "accepted": True,
        "items": items,
    }


@router.post("/yjs/webspaces", dependencies=[Depends(require_token)])
async def node_yjs_create_webspace(payload: WebspaceCreateRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip() or "web_desktop"
    info = await WebspaceService().create(
        str(payload.id or "").strip() or None,
        str(payload.title or "").strip() or None,
        scenario_id=scenario_id,
        dev=bool(payload.dev),
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace": {
            "id": info.id,
            "title": info.title,
            "created_at": info.created_at,
            "kind": info.kind,
            "home_scenario": info.home_scenario,
            "source_mode": info.source_mode,
        },
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=info.id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/runtime", dependencies=[Depends(require_token)])
async def node_yjs_webspace_runtime(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    return {
        "ok": True,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=str(webspace_id or "").strip() or "default",
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}", dependencies=[Depends(require_token)])
async def node_yjs_webspace_state(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    state = await describe_webspace_operational_state(target_webspace_id)
    projection = await describe_webspace_projection_state(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace": state.to_dict(),
        "projection": projection,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.patch("/yjs/webspaces/{webspace_id}", dependencies=[Depends(require_token)])
async def node_yjs_update_webspace(webspace_id: str, payload: WebspaceUpdateRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    info = await WebspaceService().update_metadata(
        target_webspace_id,
        title=str(payload.title or "").strip() or None,
        home_scenario=str(payload.home_scenario or "").strip() or None,
    )
    if info is None:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "webspace_not_found",
        }
    return {
        "ok": True,
        "accepted": True,
        "webspace": {
            "id": info.id,
            "title": info.title,
            "created_at": info.created_at,
            "kind": info.kind,
            "home_scenario": info.home_scenario,
            "source_mode": info.source_mode,
        },
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
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
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=str(webspace_id or "default") or "default",
        ),
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
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/toggle-install", dependencies=[Depends(require_token)])
async def node_yjs_toggle_install(webspace_id: str, payload: WebspaceToggleInstallRequest) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "default").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    svc.toggle_install_with_live_room(str(payload.type), str(payload.id), target_webspace_id)
    installed = await svc.get_installed_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "type": str(payload.type),
        "id": str(payload.id),
        "installed": installed.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.post("/yjs/webspaces/{webspace_id}/scenario", dependencies=[Depends(require_token)])
async def node_yjs_switch_scenario(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": str(webspace_id or "default") or "default",
            "error": "scenario_id_required",
        }
    result = await switch_webspace_scenario(
        str(webspace_id or "default") or "default",
        scenario_id,
        set_home=payload.set_home,
    )
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/go-home", dependencies=[Depends(require_token)])
async def node_yjs_go_home(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    result = await go_home_webspace(str(webspace_id or "default") or "default")
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    return result


@router.post("/yjs/dev-webspaces/ensure", dependencies=[Depends(require_token)])
async def node_yjs_ensure_dev(payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_id_required",
        }
    result = await ensure_dev_webspace_for_scenario(
        scenario_id,
        requested_id=str(payload.requested_id or "").strip() or None,
        title=str(payload.title or "").strip() or None,
    )
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(result.get("webspace_id") or "default") or "default",
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/set-home", dependencies=[Depends(require_token)])
async def node_yjs_set_home(webspace_id: str, payload: WebspaceYjsActionRequest) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    scenario_id = str(payload.scenario_id or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": str(webspace_id or "default") or "default",
            "error": "scenario_id_required",
        }
    info = await WebspaceService().set_home_scenario(str(webspace_id or "default") or "default", scenario_id)
    result: dict[str, Any]
    if info is None:
        result = {
            "ok": False,
            "accepted": False,
            "webspace_id": str(webspace_id or "default") or "default",
            "scenario_id": scenario_id,
            "error": "webspace_not_found",
        }
    else:
        result = {
            "ok": True,
            "accepted": True,
            "webspace_id": info.id,
            "scenario_id": scenario_id,
            "home_scenario": info.home_scenario,
        }
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
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
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/restore", dependencies=[Depends(require_token)])
async def node_yjs_restore(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    result = await restore_webspace_from_snapshot(str(webspace_id or "default") or "default")
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
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
