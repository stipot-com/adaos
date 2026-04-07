from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from adaos.services.system_model.model import (
    CanonicalGovernance,
    CanonicalObject,
    CanonicalStatus,
    compact_mapping,
    normalize_connectivity_status,
    normalize_installation_status,
    normalize_operational_status,
)


def coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    dict_dump = getattr(value, "dict", None)
    if callable(dict_dump):
        dumped = dict_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    if is_dataclass(value):
        dumped = asdict(value)
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    tuple_dump = getattr(value, "_asdict", None)
    if callable(tuple_dump):
        dumped = tuple_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    obj_dict = getattr(value, "__dict__", None)
    if isinstance(obj_dict, Mapping):
        return {str(key): item for key, item in obj_dict.items() if not str(key).startswith("_")}
    return {}


def canonical_object_from_node_status(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    node_id = str(data.get("node_id") or "unknown").strip() or "unknown"
    role = str(data.get("role") or "node").strip().lower() or "node"
    subnet_id = str(data.get("subnet_id") or "").strip()
    primary_name = str(data.get("primary_node_name") or "").strip()
    node_names = [str(item or "").strip() for item in list(data.get("node_names") or []) if str(item or "").strip()]
    route_mode = str(data.get("route_mode") or "").strip() or None
    connected_to_hub = data.get("connected_to_hub")
    ready = data.get("ready")
    draining = bool(data.get("draining"))
    node_state = str(data.get("node_state") or "").strip() or None

    status = normalize_operational_status(node_state)
    if ready is True and not draining:
        status = CanonicalStatus.ONLINE
    elif draining:
        status = CanonicalStatus.WARNING
    elif ready is False and connected_to_hub is False:
        status = CanonicalStatus.OFFLINE
    elif ready is False and status == CanonicalStatus.UNKNOWN:
        status = CanonicalStatus.WARNING

    title = primary_name or (node_names[0] if node_names else node_id)
    relations = {"subnet": [f"subnet:{subnet_id}"]} if subnet_id else {}
    health = compact_mapping(
        {
            "availability": "ready" if ready is True else "not_ready" if ready is False else None,
            "connectivity": normalize_connectivity_status(
                connected_to_hub if connected_to_hub is not None else route_mode
            ),
            "route_mode": route_mode,
        }
    )
    runtime = compact_mapping(
        {
            "node_state": node_state,
            "draining": draining,
        }
    )
    representations = compact_mapping(
        {
            "llm": {
                "role": role,
                "node_names": node_names,
                "route_mode": route_mode,
            }
        }
    )
    summary = f"{role} node" + (f" in subnet {subnet_id}" if subnet_id else "")

    return CanonicalObject(
        id=f"{role}:{node_id}" if role in {"hub", "member"} else f"node:{node_id}",
        kind=role if role in {"hub", "member"} else "node",
        title=title,
        summary=summary,
        status=status,
        health=health,
        relations=relations,
        runtime=runtime,
        representations=representations,
    )


def canonical_object_from_skill_status(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("name") or data.get("id") or "unknown").strip() or "unknown"
    slot = str(data.get("slot") or data.get("active_slot") or "").strip() or None
    update_available = bool(data.get("update_available"))
    local_version = str(data.get("version") or "").strip() or None
    remote_version = str(data.get("remote_version") or "").strip() or None

    status = CanonicalStatus.WARNING if update_available else CanonicalStatus.ONLINE if slot else CanonicalStatus.UNKNOWN
    runtime = compact_mapping(
        {
            "slot": slot,
            "installation_status": normalize_installation_status("active" if slot else "pending_update" if update_available else "installed"),
        }
    )
    versioning = compact_mapping(
        {
            "actual": local_version,
            "desired": remote_version,
            "drift": bool(update_available),
        }
    )

    return CanonicalObject(
        id=f"skill:{name}",
        kind="skill",
        title=name,
        summary="Skill catalog object",
        status=status,
        runtime=runtime,
        versioning=versioning,
    )


def canonical_object_from_scenario_item(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    name = str(data.get("name") or data.get("id") or "unknown").strip() or "unknown"
    version = str(data.get("version") or "").strip() or None
    path = str(data.get("path") or "").strip() or None
    runtime = compact_mapping({"installation_status": normalize_installation_status("installed")})
    actual_state = compact_mapping({"path": path})
    versioning = compact_mapping({"actual": version})
    return CanonicalObject(
        id=f"scenario:{name}",
        kind="scenario",
        title=name,
        summary="Scenario catalog object",
        status=CanonicalStatus.UNKNOWN,
        runtime=runtime,
        actual_state=actual_state,
        versioning=versioning,
    )


def canonical_object_from_browser_session(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    device_id = str(data.get("device_id") or data.get("id") or "unknown").strip() or "unknown"
    webspace_id = str(data.get("webspace_id") or "").strip() or None
    connection_state = str(data.get("connection_state") or "").strip().lower() or None
    events_channel_state = str(data.get("events_channel_state") or "").strip().lower() or None
    yjs_channel_state = str(data.get("yjs_channel_state") or "").strip().lower() or None

    status = normalize_operational_status(connection_state)
    if connection_state in {"connecting", "new"}:
        status = CanonicalStatus.WARNING

    relations = compact_mapping(
        {
            "workspace": [f"workspace:{webspace_id}"] if webspace_id else [],
        }
    )
    health = compact_mapping(
        {
            "connectivity": normalize_connectivity_status(connection_state),
            "events_channel": normalize_connectivity_status(events_channel_state),
            "yjs_channel": normalize_connectivity_status(yjs_channel_state),
        }
    )
    runtime = compact_mapping(
        {
            "connection_state": connection_state,
            "events_channel_state": events_channel_state,
            "yjs_channel_state": yjs_channel_state,
            "incoming_audio_tracks": data.get("incoming_audio_tracks"),
            "incoming_video_tracks": data.get("incoming_video_tracks"),
            "loopback_audio_tracks": data.get("loopback_audio_tracks"),
            "loopback_video_tracks": data.get("loopback_video_tracks"),
            "media_track_total": data.get("media_track_total"),
        }
    )
    summary = f"Browser session {device_id}" + (f" in webspace {webspace_id}" if webspace_id else "")
    return CanonicalObject(
        id=f"browser:{device_id}",
        kind="browser_session",
        title=device_id,
        summary=summary,
        status=status,
        health=health,
        relations=relations,
        runtime=runtime,
    )


def canonical_object_from_capacity_snapshot(payload: Any, *, node_id: str | None = None) -> CanonicalObject:
    data = coerce_mapping(payload)
    io_items = [item for item in list(data.get("io") or []) if isinstance(item, Mapping)]
    skill_items = [item for item in list(data.get("skills") or []) if isinstance(item, Mapping)]
    scenario_items = [item for item in list(data.get("scenarios") or []) if isinstance(item, Mapping)]
    active_skill_total = sum(1 for item in skill_items if bool(item.get("active", True)))
    active_scenario_total = sum(1 for item in scenario_items if bool(item.get("active", True)))
    title = "Local capacity"
    status = CanonicalStatus.ONLINE if io_items or skill_items or scenario_items else CanonicalStatus.UNKNOWN
    relations = compact_mapping({"hosted_on": [f"node:{node_id}"] if node_id else []})
    resources = compact_mapping(
        {
            "io_total": len(io_items),
            "skill_total": len(skill_items),
            "scenario_total": len(scenario_items),
            "active_skill_total": active_skill_total,
            "active_scenario_total": active_scenario_total,
        }
    )
    runtime = compact_mapping(
        {
            "io_types": [str(item.get("io_type") or item.get("type") or "").strip() for item in io_items if str(item.get("io_type") or item.get("type") or "").strip()],
            "skills": [str(item.get("name") or "").strip() for item in skill_items if str(item.get("name") or "").strip()],
            "scenarios": [str(item.get("name") or "").strip() for item in scenario_items if str(item.get("name") or "").strip()],
        }
    )
    return CanonicalObject(
        id=f"capacity:{node_id}" if node_id else "capacity:local",
        kind="capacity",
        title=title,
        summary="Local node capacity and runtime inventory",
        status=status,
        relations=relations,
        resources=resources,
        runtime=runtime,
        actual_state=compact_mapping(data),
    )


def canonical_object_from_io_capacity_entry(payload: Any, *, node_id: str | None = None) -> CanonicalObject:
    data = coerce_mapping(payload)
    io_type = str(data.get("io_type") or data.get("type") or "unknown").strip() or "unknown"
    capabilities = [str(item or "").strip() for item in list(data.get("capabilities") or []) if str(item or "").strip()]
    state_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("state:")), "")
    mode_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("mode:")), "")
    reason_token = next((item.split(":", 1)[1] for item in capabilities if item.startswith("reason:")), "")
    status = CanonicalStatus.OFFLINE if state_token == "unavailable" else CanonicalStatus.ONLINE
    if not capabilities and status == CanonicalStatus.ONLINE:
        status = CanonicalStatus.UNKNOWN
    return CanonicalObject(
        id=f"io:{node_id}:{io_type}" if node_id else f"io:{io_type}",
        kind="io_endpoint",
        title=io_type,
        summary=f"Local IO endpoint {io_type}",
        status=status,
        relations=compact_mapping({"hosted_on": [f"node:{node_id}"] if node_id else []}),
        runtime=compact_mapping(
            {
                "io_type": io_type,
                "priority": data.get("priority"),
                "mode": mode_token or None,
                "reason": reason_token or None,
            }
        ),
        actual_state=compact_mapping({"capabilities": capabilities}),
    )


def canonical_object_from_user_profile(payload: Any) -> CanonicalObject:
    data = coerce_mapping(payload)
    user_id = str(data.get("user_id") or "unknown").strip() or "unknown"
    settings = data.get("settings") if isinstance(data.get("settings"), Mapping) else {}
    title = str(settings.get("preferred_name") or settings.get("full_name") or user_id).strip() or user_id
    representations = compact_mapping({"user": settings})
    governance = CanonicalGovernance(owner_id=f"profile:{user_id}")
    return CanonicalObject(
        id=f"profile:{user_id}",
        kind="profile",
        title=title,
        summary="User profile object",
        status=CanonicalStatus.UNKNOWN,
        governance=governance,
        representations=representations,
        actual_state=compact_mapping({"settings": settings}),
    )


def canonical_object_from_workspace_manifest(payload: Any) -> CanonicalObject:
    workspace_id = str(getattr(payload, "workspace_id", "") or "").strip() or str(
        coerce_mapping(payload).get("workspace_id") or "default"
    ).strip()
    title = str(getattr(payload, "title", "") or "").strip() or workspace_id
    effective_kind = str(getattr(payload, "effective_kind", "") or "").strip() or "workspace"
    effective_home_scenario = str(getattr(payload, "effective_home_scenario", "") or "").strip() or None
    effective_source_mode = str(getattr(payload, "effective_source_mode", "") or "").strip() or None
    owner_scope = str(getattr(payload, "owner_scope", "") or "").strip() or None
    profile_scope = str(getattr(payload, "profile_scope", "") or "").strip() or None
    device_binding = str(getattr(payload, "device_binding", "") or "").strip() or None
    has_ui_overlay = bool(getattr(payload, "has_ui_overlay", False))
    has_installed_overlay = bool(getattr(payload, "has_installed_overlay", False))
    has_pinned_widgets_overlay = bool(getattr(payload, "has_pinned_widgets_overlay", False))

    relations = compact_mapping(
        {
            "home_scenario": [f"scenario:{effective_home_scenario}"] if effective_home_scenario else [],
            "device_binding": [f"device:{device_binding}"] if device_binding else [],
        }
    )
    governance = CanonicalGovernance(
        owner_id=owner_scope,
        visibility=[profile_scope] if profile_scope else [],
        metadata=compact_mapping({"workspace_kind": effective_kind}),
    )
    actual_state = compact_mapping({"source_mode": effective_source_mode, "device_binding": device_binding})
    runtime = compact_mapping(
        {
            "overlay": {
                "has_ui_overlay": has_ui_overlay,
                "has_installed_overlay": has_installed_overlay,
                "has_pinned_widgets_overlay": has_pinned_widgets_overlay,
            }
        }
    )

    return CanonicalObject(
        id=f"workspace:{workspace_id}",
        kind="workspace",
        title=title,
        summary=f"{effective_kind} workspace",
        status=CanonicalStatus.ONLINE if effective_home_scenario else CanonicalStatus.UNKNOWN,
        relations=relations,
        runtime=runtime,
        governance=governance,
        actual_state=actual_state,
    )


__all__ = [
    "canonical_object_from_node_status",
    "canonical_object_from_browser_session",
    "canonical_object_from_capacity_snapshot",
    "canonical_object_from_io_capacity_entry",
    "canonical_object_from_scenario_item",
    "canonical_object_from_skill_status",
    "canonical_object_from_user_profile",
    "canonical_object_from_workspace_manifest",
    "coerce_mapping",
]
