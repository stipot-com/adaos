from __future__ import annotations

from typing import Any

from adaos.services.bootstrap import is_ready, load_config
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.system_model.mappers import canonical_object_from_node_status


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


__all__ = ["current_node_object", "current_node_status_payload", "route_info"]
