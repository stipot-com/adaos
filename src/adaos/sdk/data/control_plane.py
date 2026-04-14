from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core._ctx import require_ctx
from adaos.services.system_model.model import CanonicalKind


def get_self_model():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_node_object

    return current_node_object()


def get_self_object() -> Mapping[str, Any]:
    return get_self_model().to_dict()


def get_current_profile_model():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import current_profile_object

    return current_profile_object()


def get_current_profile_object() -> Mapping[str, Any]:
    return get_current_profile_model().to_dict()


def list_skill_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import installed_skill_objects

    return installed_skill_objects()


def list_skill_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_skill_models()]


def get_skill_model(name: str):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import skill_object

    return skill_object(name)


def get_skill_object(name: str) -> Mapping[str, Any]:
    return get_skill_model(name).to_dict()


def list_scenario_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import installed_scenario_objects

    return installed_scenario_objects()


def list_scenario_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_scenario_models()]


def get_scenario_model(name: str):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import scenario_object

    return scenario_object(name)


def get_scenario_object(name: str) -> Mapping[str, Any]:
    return get_scenario_model(name).to_dict()


def list_workspace_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import workspace_objects

    return workspace_objects()


def list_workspace_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_workspace_models()]


def get_workspace_model(workspace_id: str):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import workspace_object

    return workspace_object(workspace_id)


def get_workspace_object(workspace_id: str) -> Mapping[str, Any]:
    return get_workspace_model(workspace_id).to_dict()


def list_browser_session_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import browser_session_objects

    return browser_session_objects()


def list_browser_session_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_browser_session_models()]


def list_device_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.catalog import device_objects

    return device_objects()


def list_device_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_device_models()]


def get_local_capacity_model():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_node_object
    from adaos.services.system_model.catalog import local_capacity_object

    subject = current_node_object()
    node_ref = subject.id.split(":", 1)[1] if ":" in subject.id else subject.id
    return local_capacity_object(node_id=node_ref)


def get_local_capacity_object() -> Mapping[str, Any]:
    return get_local_capacity_model().to_dict()


def list_local_io_models():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_node_object
    from adaos.services.system_model.catalog import local_io_objects

    subject = current_node_object()
    node_ref = subject.id.split(":", 1)[1] if ":" in subject.id else subject.id
    return local_io_objects(node_id=node_ref)


def list_local_io_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_local_io_models()]


def get_reliability_model(*, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_reliability_projection

    return current_reliability_projection(webspace_id=webspace_id)


def get_reliability_projection(*, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_reliability_model(webspace_id=webspace_id).to_dict()


def get_reliability_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_reliability_model(webspace_id=webspace_id).objects]


def get_overview_model(*, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_overview_projection

    return current_overview_projection(webspace_id=webspace_id)


def get_overview_projection(*, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_overview_model(webspace_id=webspace_id).to_dict()


def get_object_model(object_id: str, *, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_object_projection

    return current_object_projection(object_id, webspace_id=webspace_id)


def get_object_projection(object_id: str, *, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_object_model(object_id, webspace_id=webspace_id).to_dict()


def get_object_inspector_model(object_id: str, *, task_goal: str | None = None, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_object_inspector

    return current_object_inspector(object_id, task_goal=task_goal, webspace_id=webspace_id)


def get_object_inspector_projection(
    object_id: str,
    *,
    task_goal: str | None = None,
    webspace_id: str | None = None,
) -> Mapping[str, Any]:
    return get_object_inspector_model(object_id, task_goal=task_goal, webspace_id=webspace_id).to_dict()


def get_topology_model(object_id: str, *, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_topology_projection

    return current_topology_projection(object_id, webspace_id=webspace_id)


def get_topology_projection(object_id: str, *, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_topology_model(object_id, webspace_id=webspace_id).to_dict()


def get_task_packet_model(object_id: str, *, task_goal: str | None = None, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_task_packet

    return current_task_packet(object_id, task_goal=task_goal, webspace_id=webspace_id)


def get_task_packet(object_id: str, *, task_goal: str | None = None, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_task_packet_model(object_id, task_goal=task_goal, webspace_id=webspace_id).to_dict()


def get_subnet_planning_context(
    object_id: str | None = None,
    *,
    task_goal: str | None = None,
    webspace_id: str | None = None,
) -> Mapping[str, Any]:
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_subnet_planning_context

    return current_subnet_planning_context(object_id=object_id, task_goal=task_goal, webspace_id=webspace_id)


def get_root_model(*, webspace_id: str | None = None):
    projection = get_reliability_model(webspace_id=webspace_id)
    for item in projection.objects:
        if str(getattr(item, "kind", "") or "") == CanonicalKind.ROOT.value:
            return item
    raise LookupError("root")


def get_root_object(*, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_root_model(webspace_id=webspace_id).to_dict()


def list_runtime_models(*, webspace_id: str | None = None):
    projection = get_reliability_model(webspace_id=webspace_id)
    return [item for item in projection.objects if str(getattr(item, "kind", "") or "") == CanonicalKind.RUNTIME.value]


def list_runtime_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_runtime_models(webspace_id=webspace_id)]


def list_connection_models(*, webspace_id: str | None = None):
    projection = get_reliability_model(webspace_id=webspace_id)
    return [item for item in projection.objects if str(getattr(item, "kind", "") or "") == CanonicalKind.CONNECTION.value]


def list_connection_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_connection_models(webspace_id=webspace_id)]


def list_quota_models(*, webspace_id: str | None = None):
    projection = get_reliability_model(webspace_id=webspace_id)
    return [item for item in projection.objects if str(getattr(item, "kind", "") or "") == CanonicalKind.QUOTA.value]


def list_quota_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_quota_models(webspace_id=webspace_id)]


def get_inventory_model():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_inventory_projection

    return current_inventory_projection()


def get_inventory_projection() -> Mapping[str, Any]:
    return get_inventory_model().to_dict()


def get_inventory_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_inventory_model().objects]


def get_neighborhood_model(object_id: str | None = None, *, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_neighborhood_projection

    return current_neighborhood_projection(object_id=object_id, webspace_id=webspace_id)


def get_neighborhood_projection(object_id: str | None = None, *, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_neighborhood_model(object_id=object_id, webspace_id=webspace_id).to_dict()


def get_neighborhood_objects(object_id: str | None = None, *, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_neighborhood_model(object_id=object_id, webspace_id=webspace_id).objects]


__all__ = [
    "get_self_model",
    "get_self_object",
    "get_current_profile_model",
    "get_current_profile_object",
    "list_skill_models",
    "list_skill_objects",
    "get_skill_model",
    "get_skill_object",
    "list_scenario_models",
    "list_scenario_objects",
    "get_scenario_model",
    "get_scenario_object",
    "list_workspace_models",
    "list_workspace_objects",
    "get_workspace_model",
    "get_workspace_object",
    "list_browser_session_models",
    "list_browser_session_objects",
    "list_device_models",
    "list_device_objects",
    "get_local_capacity_model",
    "get_local_capacity_object",
    "list_local_io_models",
    "list_local_io_objects",
    "get_reliability_model",
    "get_reliability_projection",
    "get_reliability_objects",
    "get_overview_model",
    "get_overview_projection",
    "get_object_model",
    "get_object_inspector_model",
    "get_object_inspector_projection",
    "get_object_projection",
    "get_topology_model",
    "get_topology_projection",
    "get_task_packet_model",
    "get_task_packet",
    "get_subnet_planning_context",
    "get_root_model",
    "get_root_object",
    "list_runtime_models",
    "list_runtime_objects",
    "list_connection_models",
    "list_connection_objects",
    "list_quota_models",
    "list_quota_objects",
    "get_inventory_model",
    "get_inventory_projection",
    "get_inventory_objects",
    "get_neighborhood_model",
    "get_neighborhood_projection",
    "get_neighborhood_objects",
]
