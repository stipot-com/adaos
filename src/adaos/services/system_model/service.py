from __future__ import annotations

from typing import Any

from adaos.services.bootstrap import is_ready, load_config
from adaos.services.reliability import reliability_snapshot
from adaos.services.registry.subnet_directory import get_directory
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
    canonical_object_from_subnet_directory_node,
)
from adaos.services.system_model.projections import (
    canonical_inventory_projection,
    canonical_neighborhood_projection,
    canonical_projection_from_reliability_snapshot,
)


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


def current_neighborhood_projection():
    tenant_id, owner_id = _control_plane_scope_refs()
    subject = current_node_object()
    node_ref = _node_ref(subject.id)
    reliability = current_reliability_projection()

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
    "current_inventory_projection",
    "current_neighborhood_projection",
    "current_node_object",
    "current_node_status_payload",
    "current_reliability_payload",
    "current_reliability_projection",
    "route_info",
]
