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
from adaos.services.io_web.desktop import WebDesktopInstalled, WebDesktopService, WebDesktopSnapshot
from adaos.services.media_library import (
    ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
    guess_media_type,
    list_media_files,
    media_capabilities,
    media_file_path,
    media_snapshot,
)
from adaos.services.node_config import set_node_names as save_node_names_config
from adaos.services.reliability import media_plane_runtime_snapshot, yjs_sync_runtime_snapshot
from adaos.services.scenario.webspace_runtime import (
    WebspaceService,
    describe_webspace_operational_state,
    describe_webspace_overlay_state,
    describe_webspace_projection_state,
    describe_webspace_rebuild_state,
    ensure_dev_webspace_for_scenario,
    go_home_webspace,
    reload_webspace_from_scenario,
    restore_webspace_from_snapshot,
    set_current_webspace_home,
    switch_webspace_scenario,
)
from adaos.services.skill.manager import SkillManager
from adaos.services.realtime_sidecar import (
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
)
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.system_model.service import (
    current_node_object,
    current_node_status_payload,
    current_reliability_payload,
    current_reliability_projection,
    route_info,
)
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.store import get_ystore_for_webspace

router = APIRouter()
_log = logging.getLogger("adaos.api.node_api")


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _publish_yjs_control_event(
    *,
    action: str,
    webspace_id: str,
    result: dict[str, Any],
    scenario_id: str | None = None,
) -> None:
    payload = {
        "action": str(action or "").strip(),
        "webspace_id": str(webspace_id or "").strip() or "default",
        "scenario_id": str(scenario_id or result.get("scenario_id") or "").strip() or None,
        "ok": bool(result.get("ok")),
        "accepted": bool(result.get("accepted")),
        "source_of_truth": str(result.get("source_of_truth") or "").strip() or None,
        "home_scenario": str(result.get("home_scenario") or "").strip() or None,
        "error": str(result.get("error") or "").strip() or None,
    }
    event_type = "node.yjs.control.completed" if payload["ok"] and payload["accepted"] else "node.yjs.control.failed"
    try:
        get_ctx().bus.publish(
            Event(
                type=event_type,
                payload=payload,
                source="node.api",
                ts=time.time(),
            )
        )
    except Exception:
        _log.debug("failed to publish %s for action=%s webspace=%s", event_type, action, webspace_id, exc_info=True)


async def _describe_yjs_materialization(webspace_id: str) -> dict[str, Any]:
    target_webspace_id = str(webspace_id or "").strip() or "default"
    try:
        async with async_get_ydoc(target_webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            data_map = ydoc.get_map("data")
            application = _coerce_dict(ui_map.get("application") or {})
            desktop = _coerce_dict(application.get("desktop") or {})
            modals = _coerce_dict(application.get("modals") or {})
            catalog = _coerce_dict(data_map.get("catalog") or {})
            apps = _coerce_list(catalog.get("apps"))
            widgets = _coerce_list(catalog.get("widgets"))
            page_schema = _coerce_dict(desktop.get("pageSchema") or {})
            page_widgets = _coerce_list(page_schema.get("widgets"))
            topbar = _coerce_list(desktop.get("topbar"))
            current_scenario = str(ui_map.get("current_scenario") or "").strip() or None

            has_ui_application = bool(application)
            has_desktop_config = bool(desktop)
            has_desktop_page_schema = bool(page_schema)
            has_apps_catalog_modal = "apps_catalog" in modals
            has_widgets_catalog_modal = "widgets_catalog" in modals
            has_catalog_apps = isinstance(catalog.get("apps"), list)
            has_catalog_widgets = isinstance(catalog.get("widgets"), list)
            ready = (
                has_ui_application
                and has_desktop_config
                and has_desktop_page_schema
                and has_apps_catalog_modal
                and has_widgets_catalog_modal
                and has_catalog_apps
                and has_catalog_widgets
            )

            return {
                "ready": ready,
                "webspace_id": target_webspace_id,
                "current_scenario": current_scenario,
                "has_ui_application": has_ui_application,
                "has_desktop_config": has_desktop_config,
                "has_desktop_page_schema": has_desktop_page_schema,
                "has_apps_catalog_modal": has_apps_catalog_modal,
                "has_widgets_catalog_modal": has_widgets_catalog_modal,
                "has_catalog_apps": has_catalog_apps,
                "has_catalog_widgets": has_catalog_widgets,
                "catalog_counts": {
                    "apps": len(apps),
                    "widgets": len(widgets),
                },
                "topbar_count": len(topbar),
                "page_widget_count": len(page_widgets),
            }
    except Exception as exc:
        return {
            "ready": False,
            "webspace_id": target_webspace_id,
            "current_scenario": None,
            "has_ui_application": False,
            "has_desktop_config": False,
            "has_desktop_page_schema": False,
            "has_apps_catalog_modal": False,
            "has_widgets_catalog_modal": False,
            "has_catalog_apps": False,
            "has_catalog_widgets": False,
            "catalog_counts": {"apps": 0, "widgets": 0},
            "topbar_count": 0,
            "page_widget_count": 0,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


async def _read_live_catalog_items(webspace_id: str, kind: str) -> list[dict[str, Any]]:
    target_webspace_id = str(webspace_id or "").strip() or "default"
    bucket = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    try:
        async with async_get_ydoc(target_webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            catalog = _coerce_dict(data_map.get("catalog") or {})
            items = catalog.get(bucket)
            return [dict(it) for it in _coerce_list(items) if isinstance(it, dict)]
    except Exception:
        return []


async def _materialize_catalog_items(webspace_id: str, kind: str) -> list[dict[str, Any]]:
    bucket = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    raw_items = await _read_live_catalog_items(webspace_id, bucket)
    desktop_snapshot = await WebDesktopService().get_snapshot_async(webspace_id)
    installed_ids = set(
        list(getattr(getattr(desktop_snapshot, "installed", None), "apps", []) or [])
        if bucket == "apps"
        else list(getattr(getattr(desktop_snapshot, "installed", None), "widgets", []) or [])
    )
    pinned_ids = {
        str(item.get("id") or "").strip()
        for item in list(getattr(desktop_snapshot, "pinned_widgets", []) or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    default_icon = "apps-outline" if bucket == "apps" else "layers-outline"
    materialized: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            continue
        scenario_id = str(raw.get("scenario_id") or "").strip()
        launch_modal = str(raw.get("launchModal") or "").strip()
        source = str(raw.get("source") or raw.get("origin") or "").strip()
        installed_now = item_id in installed_ids
        pinned_now = bucket == "widgets" and item_id in pinned_ids
        kind_label = ""
        if scenario_id:
            kind_label = "Scenario"
        elif launch_modal:
            kind_label = "Modal"
        elif bucket == "widgets":
            kind_label = "Widget"
        materialized.append(
            {
                "id": item_id,
                "title": str(raw.get("title") or item_id).strip() or item_id,
                "icon": str(raw.get("icon") or "").strip() or default_icon,
                "subtitle": str(raw.get("subtitle") or "").strip() or scenario_id or launch_modal or source or "",
                "kindLabel": kind_label,
                "installType": "app" if bucket == "apps" else "widget",
                "installable": True,
                "installed": installed_now,
                "pinnable": bucket == "widgets" and (installed_now or pinned_now),
                "pinned": pinned_now,
                "scenario_id": scenario_id or None,
                "launchModal": launch_modal or None,
                "source": source or None,
                "origin": str(raw.get("origin") or "").strip() or None,
                "dev": bool(raw.get("dev")),
            }
        )
    return materialized


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


class WebspacePinnedWidgetsRequest(BaseModel):
    pinnedWidgets: list[dict[str, Any]] = Field(default_factory=list)


class WebspaceDesktopUpdateRequest(BaseModel):
    installed: dict[str, Any] | None = None
    pinnedWidgets: list[dict[str, Any]] | None = None
    topbar: list[Any] | None = None
    pageSchema: dict[str, Any] | None = None


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


def _node_status_payload() -> dict[str, Any]:
    return current_node_status_payload()


@router.get("/status", response_model=NodeStatus, dependencies=[Depends(require_token)])
async def node_status():
    return NodeStatus(**_node_status_payload())


@router.get("/control-plane/objects/self", dependencies=[Depends(require_token)])
async def node_control_plane_object_self() -> dict[str, Any]:
    canonical = current_node_object()
    return {"ok": True, "object": canonical.to_dict()}


@router.get("/control-plane/projections/reliability", dependencies=[Depends(require_token)])
async def node_control_plane_reliability_projection(webspace_id: str | None = None) -> dict[str, Any]:
    projection = current_reliability_projection(webspace_id=webspace_id)
    return {"ok": True, "projection": projection.to_dict()}


@router.get("/reliability", dependencies=[Depends(require_token)])
async def node_reliability() -> dict[str, Any]:
    return current_reliability_payload()


@router.post("/hub-root/reconnect", dependencies=[Depends(require_token)])
async def hub_root_reconnect(payload: HubRootReconnectRequest) -> dict[str, Any]:
    return await request_hub_root_reconnect(transport=payload.transport, url_override=payload.url_override)


@router.get("/sidecar/status", dependencies=[Depends(require_token)])
async def sidecar_status(request: Request) -> dict[str, Any]:
    reliability = current_reliability_payload()
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
    reliability = current_reliability_payload()
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
    route_mode, connected = route_info(conf.role)

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
    overlay = describe_webspace_overlay_state(target_webspace_id)
    projection = await describe_webspace_projection_state(target_webspace_id)
    rebuild = describe_webspace_rebuild_state(target_webspace_id)
    desktop = (await WebDesktopService().get_snapshot_async(target_webspace_id)).to_dict()
    materialization = await _describe_yjs_materialization(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace": state.to_dict(),
        "overlay": overlay,
        "desktop": desktop,
        "projection": projection,
        "rebuild": rebuild,
        "materialization": materialization,
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
    result = {
        "ok": True,
        "accepted": True,
        "webspace_id": str(webspace_id or "default") or "default",
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=str(webspace_id or "default") or "default",
        ),
    }
    _publish_yjs_control_event(
        action="backup",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
    )
    return result


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
    _publish_yjs_control_event(
        action="scenario",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
        scenario_id=scenario_id,
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
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "type": str(payload.type),
        "id": str(payload.id),
        "installed": installed.to_dict(),
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/desktop", dependencies=[Depends(require_token)])
async def node_yjs_desktop_state(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    desktop = await WebDesktopService().get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.get("/yjs/webspaces/{webspace_id}/catalog/{kind}", dependencies=[Depends(require_token)])
async def node_yjs_catalog_state(webspace_id: str, kind: str) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    normalized_kind = "widgets" if str(kind or "").strip().lower() == "widgets" else "apps"
    materialization = await _describe_yjs_materialization(target_webspace_id)
    items = await _materialize_catalog_items(target_webspace_id, normalized_kind)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "kind": normalized_kind,
        "items": items,
        "materialization": materialization,
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.post("/yjs/webspaces/{webspace_id}/desktop/pinned-widgets", dependencies=[Depends(require_token)])
async def node_yjs_set_pinned_widgets(
    webspace_id: str,
    payload: WebspacePinnedWidgetsRequest,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    svc.set_pinned_widgets_with_live_room(list(payload.pinnedWidgets or []), target_webspace_id)
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
        "runtime": yjs_sync_runtime_snapshot(
            role=conf.role,
            webspace_id=target_webspace_id,
        ),
    }


@router.patch("/yjs/webspaces/{webspace_id}/desktop", dependencies=[Depends(require_token)])
async def node_yjs_update_desktop(
    webspace_id: str,
    payload: WebspaceDesktopUpdateRequest,
) -> dict[str, Any]:
    conf = load_config()
    target_webspace_id = str(webspace_id or "").strip() or "default"
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": target_webspace_id,
            "error": "hub_role_required",
        }
    svc = WebDesktopService()
    current = await svc.get_snapshot_async(target_webspace_id)
    next_snapshot = WebDesktopSnapshot(
        installed=current.installed,
        pinned_widgets=current.pinned_widgets,
        topbar=current.topbar,
        page_schema=current.page_schema,
    )
    if payload.installed is not None:
        installed = payload.installed if isinstance(payload.installed, dict) else {}
        next_snapshot.installed = WebDesktopInstalled(
            apps=list(installed.get("apps") or []),
            widgets=list(installed.get("widgets") or []),
        )
    if payload.pinnedWidgets is not None:
        next_snapshot.pinned_widgets = list(payload.pinnedWidgets or [])
    if payload.topbar is not None:
        next_snapshot.topbar = list(payload.topbar or [])
    if payload.pageSchema is not None:
        next_snapshot.page_schema = dict(payload.pageSchema or {})
    svc.set_snapshot_with_live_room(next_snapshot, target_webspace_id)
    desktop = await svc.get_snapshot_async(target_webspace_id)
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": target_webspace_id,
        "desktop": desktop.to_dict(),
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
        wait_for_rebuild=False,
    )
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    _publish_yjs_control_event(
        action="go_home",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
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
    result = await go_home_webspace(
        str(webspace_id or "default") or "default",
        wait_for_rebuild=False,
    )
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    _publish_yjs_control_event(
        action="set_home",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
        scenario_id=scenario_id,
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
    _publish_yjs_control_event(
        action="ensure_dev",
        webspace_id=str(result.get("webspace_id") or "default") or "default",
        result=result,
        scenario_id=scenario_id,
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
    _publish_yjs_control_event(
        action="set_home_current",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
    )
    return result


@router.post("/yjs/webspaces/{webspace_id}/set-home-current", dependencies=[Depends(require_token)])
async def node_yjs_set_home_current(webspace_id: str) -> dict[str, Any]:
    conf = load_config()
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": webspace_id,
            "error": "hub_role_required",
        }
    result = await set_current_webspace_home(str(webspace_id or "default") or "default")
    result["runtime"] = yjs_sync_runtime_snapshot(
        role=conf.role,
        webspace_id=str(webspace_id or "default") or "default",
    )
    _publish_yjs_control_event(
        action="reload",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
        scenario_id=str(payload.scenario_id or "").strip() or None,
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
    _publish_yjs_control_event(
        action="reset",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
        scenario_id=str(payload.scenario_id or "").strip() or None,
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
    _publish_yjs_control_event(
        action="restore",
        webspace_id=str(webspace_id or "default") or "default",
        result=result,
    )
    return result


@router.get("/media/files", dependencies=[Depends(require_token)])
async def list_media_library() -> dict[str, Any]:
    snapshot = media_snapshot()
    snapshot["proxy_limits"] = {
        "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
        "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    }
    return snapshot


@router.get("/media/runtime", dependencies=[Depends(require_token)])
async def media_runtime() -> dict[str, Any]:
    conf = load_config()
    runtime = media_plane_runtime_snapshot(
        role=str(getattr(conf, "role", "") or ""),
        route_mode=None,
        connected_to_hub=None,
    )
    runtime["ok"] = True
    runtime["proxy_limits"] = {
        "root_routed_response_limit_bytes": ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES,
        "root_media_relay_max_upload_bytes": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    }
    runtime["capabilities"] = media_capabilities()
    runtime["files"] = {
        "items": list_media_files(),
    }
    return runtime


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
    route_mode, connected = route_info(conf.role)
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
