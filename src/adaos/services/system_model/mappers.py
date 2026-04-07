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

    relations = {"home_scenario": [f"scenario:{effective_home_scenario}"]} if effective_home_scenario else {}
    governance = CanonicalGovernance(
        owner_id=owner_scope,
        visibility=[profile_scope] if profile_scope else [],
        metadata=compact_mapping({"workspace_kind": effective_kind}),
    )
    actual_state = compact_mapping({"source_mode": effective_source_mode})

    return CanonicalObject(
        id=f"workspace:{workspace_id}",
        kind="workspace",
        title=title,
        summary=f"{effective_kind} workspace",
        status=CanonicalStatus.UNKNOWN,
        relations=relations,
        governance=governance,
        actual_state=actual_state,
    )


__all__ = [
    "canonical_object_from_node_status",
    "canonical_object_from_scenario_item",
    "canonical_object_from_skill_status",
    "canonical_object_from_user_profile",
    "canonical_object_from_workspace_manifest",
    "coerce_mapping",
]
