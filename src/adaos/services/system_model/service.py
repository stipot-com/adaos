from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.bootstrap import is_ready, load_config
from adaos.services.reliability import reliability_snapshot
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.runtime_paths import current_base_dir
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.system_model.catalog import (
    browser_session_objects,
    current_profile_object,
    device_objects,
    installed_scenario_objects,
    installed_skill_objects,
    local_capacity_object,
    local_io_objects,
    workspace_objects,
)
from adaos.services.system_model.governance import apply_governance_defaults, apply_projection_governance
from adaos.services.system_model.model import CanonicalKind, canonical_ref
from adaos.services.system_model.mappers import (
    canonical_object_from_capacity_snapshot,
    canonical_object_from_node_status,
    canonical_object_from_supervisor_runtime,
    canonical_object_from_subnet_directory_node,
)
from adaos.services.system_model.projections import (
    canonical_object_inspector,
    canonical_object_projection,
    canonical_overview_projection,
    canonical_inventory_projection,
    canonical_neighborhood_projection,
    canonical_task_packet,
    canonical_topology_projection,
    canonical_projection_from_reliability_snapshot,
)


_CONTROL_PLANE_CACHE_TTL_S = 1.0
_CONTROL_PLANE_CACHE: dict[str, tuple[float, list[Any]]] = {}


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def route_info(role: str) -> tuple[str | None, bool | None]:
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


def current_node_status_payload() -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    return {
        "node_id": conf.node_id,
        "subnet_id": conf.subnet_id,
        "role": conf.role,
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
        "ready": is_ready() and not bool(lifecycle.get("draining")),
        "node_state": str(lifecycle.get("node_state") or "ready"),
        "draining": bool(lifecycle.get("draining")),
        "route_mode": route_mode,
        "connected_to_hub": connected,
    }


def _control_plane_scope_refs() -> tuple[str | None, str | None]:
    conf = load_config()
    subnet_value = str(getattr(conf, "subnet_id", "") or "").strip()
    owner_value = str(getattr(conf, "owner_id", "") or "").strip()
    tenant_id = f"subnet:{subnet_value}" if subnet_value else None
    owner_id = canonical_ref(CanonicalKind.PROFILE, owner_value) or (f"profile:{owner_value}" if owner_value else None)
    return tenant_id, owner_id


def _node_ref(subject_id: str) -> str:
    if ":" in subject_id:
        _, _, node_token = subject_id.partition(":")
        return node_token or subject_id
    return subject_id


def _append_unique(objects: list[Any], item: Any, seen: set[str]) -> None:
    obj_id = str(getattr(item, "id", "") or "").strip()
    if not obj_id or obj_id in seen:
        return
    seen.add(obj_id)
    objects.append(item)


def current_node_object():
    tenant_id, owner_id = _control_plane_scope_refs()
    return apply_governance_defaults(
        canonical_object_from_node_status(current_node_status_payload()),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_supervisor_runtime_object():
    tenant_id, owner_id = _control_plane_scope_refs()
    node_payload = current_node_status_payload()
    node_id = str(node_payload.get("node_id") or "local").strip() or "local"
    base_dir = current_base_dir()
    runtime_state = _read_json_file((base_dir / "state" / "supervisor" / "runtime.json").resolve())
    update_attempt = _read_json_file((base_dir / "state" / "supervisor" / "update_attempt.json").resolve())
    update_status = read_core_update_status()
    if not runtime_state and not update_status and not update_attempt:
        return None
    return apply_governance_defaults(
        canonical_object_from_supervisor_runtime(
            {
                "node_id": node_id,
                "runtime_state": runtime_state,
                "update_status": update_status,
                "update_attempt": update_attempt,
            }
        ),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_reliability_payload(*, webspace_id: str | None = None) -> dict[str, Any]:
    conf = load_config()
    route_mode, connected = route_info(conf.role)
    lifecycle = runtime_lifecycle_snapshot()
    return reliability_snapshot(
        node_id=conf.node_id,
        subnet_id=conf.subnet_id,
        role=conf.role,
        zone_id=getattr(conf, "zone_id", None),
        local_ready=is_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected,
        node_names=list(getattr(conf, "node_names", []) or []),
        webspace_id=webspace_id,
    )


def current_reliability_projection(*, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    return apply_projection_governance(
        canonical_projection_from_reliability_snapshot(current_reliability_payload(webspace_id=webspace_id)),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def _flatten_refs(relations: Any) -> list[str]:
    data = relations if isinstance(relations, dict) else {}
    out: list[str] = []
    for value in data.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            token = str(item or "").strip()
            if token and token not in out:
                out.append(token)
    return out


def _current_node_neighborhood_projection(*, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_node_object()
    node_ref = _node_ref(subject.id)
    reliability = current_reliability_projection(webspace_id=webspace_id)

    objects: list[Any] = []
    seen: set[str] = set()
    _append_unique(objects, local_capacity_object(node_id=node_ref), seen)
    for item in reliability.objects:
        if str(item.kind or "").strip() not in {CanonicalKind.ROOT.value, CanonicalKind.CONNECTION.value}:
            continue
        _append_unique(objects, item, seen)

    try:
        directory_nodes = list(get_directory().list_known_nodes() or [])
    except Exception:
        directory_nodes = []

    for entry in sorted(directory_nodes, key=lambda item: str(item.get("node_id") or "")):
        node_id = str(entry.get("node_id") or "").strip()
        if not node_id:
            continue
        node_obj = apply_governance_defaults(
            canonical_object_from_subnet_directory_node(entry),
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if node_obj.id != subject.id:
            _append_unique(objects, node_obj, seen)

        capacity = entry.get("capacity") if isinstance(entry.get("capacity"), dict) else {}
        if not any(isinstance(capacity.get(name), list) and capacity.get(name) for name in ("io", "skills", "scenarios")):
            continue
        if node_id == node_ref:
            continue
        capacity_obj = apply_governance_defaults(
            canonical_object_from_capacity_snapshot(
                capacity,
                node_id=node_id,
                title=f"{node_obj.title} capacity",
                summary="Subnet directory capacity snapshot",
            ),
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        _append_unique(objects, capacity_obj, seen)

    return apply_projection_governance(
        canonical_neighborhood_projection(subject, objects),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_control_plane_objects(*, webspace_id: str | None = None) -> list[Any]:
    cache_key = str(webspace_id or "").strip()
    now = time.monotonic()
    cached = _CONTROL_PLANE_CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_objects = cached
        if now - cached_at <= _CONTROL_PLANE_CACHE_TTL_S:
            return list(cached_objects)

    subject = current_node_object()
    inventory = current_inventory_projection()
    reliability = current_reliability_projection(webspace_id=webspace_id)
    neighborhood = _current_node_neighborhood_projection(webspace_id=webspace_id)
    supervisor_runtime = current_supervisor_runtime_object()
    objects: list[Any] = []
    seen: set[str] = set()
    for item in [
        subject,
        inventory.subject,
        reliability.subject,
        neighborhood.subject,
        supervisor_runtime,
        *inventory.objects,
        *reliability.objects,
        *neighborhood.objects,
    ]:
        _append_unique(objects, item, seen)
    _CONTROL_PLANE_CACHE[cache_key] = (now, list(objects))
    return objects


def current_overview_projection(*, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_node_object()
    objects = [item for item in current_control_plane_objects(webspace_id=webspace_id) if str(getattr(item, "id", "") or "") != subject.id]
    return apply_projection_governance(
        canonical_overview_projection(subject, objects),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def _object_index(*, webspace_id: str | None = None) -> dict[str, Any]:
    return {str(item.id): item for item in current_control_plane_objects(webspace_id=webspace_id)}


def current_object_model(object_id: str, *, webspace_id: str | None = None):
    token = str(object_id or "").strip()
    if token in {"self", "current", "local"}:
        return current_node_object()
    obj = _object_index(webspace_id=webspace_id).get(token)
    if obj is None:
        raise KeyError(token)
    return obj


def _neighborhood_objects_for(subject: Any, universe: list[Any]) -> list[Any]:
    subject_id = str(getattr(subject, "id", "") or "")
    related_ids = set(_flatten_refs(getattr(subject, "relations", {})))
    for item in universe:
        item_id = str(getattr(item, "id", "") or "")
        if not item_id or item_id == subject_id:
            continue
        if subject_id in _flatten_refs(getattr(item, "relations", {})):
            related_ids.add(item_id)
    neighbors: list[Any] = []
    seen: set[str] = set()
    for item in universe:
        item_id = str(getattr(item, "id", "") or "")
        if not item_id or item_id == subject_id or item_id not in related_ids or item_id in seen:
            continue
        seen.add(item_id)
        neighbors.append(item)
    return neighbors


def current_object_projection(object_id: str, *, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_object_model(object_id, webspace_id=webspace_id)
    neighborhood = _neighborhood_objects_for(subject, current_control_plane_objects(webspace_id=webspace_id))
    return apply_projection_governance(
        canonical_object_projection(subject, neighborhood),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_object_inspector(object_id: str, *, task_goal: str | None = None, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_object_model(object_id, webspace_id=webspace_id)
    neighborhood = _neighborhood_objects_for(subject, current_control_plane_objects(webspace_id=webspace_id))
    return apply_projection_governance(
        canonical_object_inspector(subject, neighborhood, task_goal=task_goal),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_topology_projection(object_id: str, *, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_object_model(object_id, webspace_id=webspace_id)
    neighborhood = _neighborhood_objects_for(subject, current_control_plane_objects(webspace_id=webspace_id))
    return apply_projection_governance(
        canonical_topology_projection(subject, neighborhood),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_task_packet(object_id: str, *, task_goal: str | None = None, webspace_id: str | None = None):
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_object_model(object_id, webspace_id=webspace_id)
    neighborhood = _neighborhood_objects_for(subject, current_control_plane_objects(webspace_id=webspace_id))
    return apply_projection_governance(
        canonical_task_packet(subject, neighborhood, task_goal=task_goal),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_neighborhood_projection(object_id: str | None = None, *, webspace_id: str | None = None):
    token = str(object_id or "").strip()
    current_id = current_node_object().id
    if not token or token in {"self", "current", "local", current_id}:
        return _current_node_neighborhood_projection(webspace_id=webspace_id)
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_object_model(token, webspace_id=webspace_id)
    objects = _neighborhood_objects_for(subject, current_control_plane_objects(webspace_id=webspace_id))
    return apply_projection_governance(
        canonical_neighborhood_projection(subject, objects),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


def current_inventory_projection():
    subject = current_node_object()
    node_ref = _node_ref(subject.id)
    reliability = current_reliability_projection()
    reliability_objects = [
        item
        for item in reliability.objects
        if str(item.kind or "").strip() in {CanonicalKind.ROOT.value, CanonicalKind.QUOTA.value}
    ]
    objects = [
        current_profile_object(),
        local_capacity_object(node_id=node_ref),
        *local_io_objects(node_id=node_ref),
        *device_objects(),
        *workspace_objects(),
        *browser_session_objects(),
        *installed_skill_objects(),
        *installed_scenario_objects(),
        *reliability_objects,
    ]
    tenant_id, owner_id = _control_plane_scope_refs()
    return apply_projection_governance(
        canonical_inventory_projection(subject, objects),
        tenant_id=tenant_id,
        owner_id=owner_id,
    )


__all__ = [
    "current_control_plane_objects",
    "current_inventory_projection",
    "current_neighborhood_projection",
    "current_node_object",
    "current_node_status_payload",
    "current_object_inspector",
    "current_object_model",
    "current_object_projection",
    "current_overview_projection",
    "current_reliability_payload",
    "current_reliability_projection",
    "current_task_packet",
    "current_topology_projection",
    "route_info",
]
