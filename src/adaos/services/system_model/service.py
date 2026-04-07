from __future__ import annotations

from typing import Any

from adaos.services.bootstrap import is_ready, load_config
from adaos.services.reliability import reliability_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.system_model.catalog import (
    browser_session_objects,
    current_profile_object,
    installed_scenario_objects,
    installed_skill_objects,
    local_capacity_object,
    local_io_objects,
    workspace_objects,
)
from adaos.services.system_model.mappers import canonical_object_from_node_status
from adaos.services.system_model.projections import canonical_inventory_projection, canonical_projection_from_reliability_snapshot


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


def current_node_object():
    return canonical_object_from_node_status(current_node_status_payload())


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
    return canonical_projection_from_reliability_snapshot(current_reliability_payload(webspace_id=webspace_id))


def current_inventory_projection():
    subject = current_node_object()
    subject_id = subject.id
    if ":" in subject_id:
        _, _, node_token = subject_id.partition(":")
        node_ref = node_token or subject_id
    else:
        node_ref = subject_id
    objects = [
        current_profile_object(),
        local_capacity_object(node_id=node_ref),
        *local_io_objects(node_id=node_ref),
        *workspace_objects(),
        *browser_session_objects(),
        *installed_skill_objects(),
        *installed_scenario_objects(),
    ]
    return canonical_inventory_projection(subject, objects)


__all__ = [
    "current_inventory_projection",
    "current_node_object",
    "current_node_status_payload",
    "current_reliability_payload",
    "current_reliability_projection",
    "route_info",
]
