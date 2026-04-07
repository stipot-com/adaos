"""SDK helpers for AdaOS control-plane object access."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.data import control_plane as _data_control_plane


def get_self_model():
    return _data_control_plane.get_self_model()


def get_self_object() -> Mapping[str, Any]:
    return get_self_model().to_dict()


def get_current_profile_model():
    return _data_control_plane.get_current_profile_model()


def get_current_profile_object() -> Mapping[str, Any]:
    return get_current_profile_model().to_dict()


def list_skill_models():
    return _data_control_plane.list_skill_models()


def list_skill_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_skill_models()]


def get_skill_model(name: str):
    return _data_control_plane.get_skill_model(name)


def get_skill_object(name: str) -> Mapping[str, Any]:
    return get_skill_model(name).to_dict()


def list_scenario_models():
    return _data_control_plane.list_scenario_models()


def list_scenario_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_scenario_models()]


def get_scenario_model(name: str):
    return _data_control_plane.get_scenario_model(name)


def get_scenario_object(name: str) -> Mapping[str, Any]:
    return get_scenario_model(name).to_dict()


def list_workspace_models():
    return _data_control_plane.list_workspace_models()


def list_workspace_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_workspace_models()]


def get_workspace_model(workspace_id: str):
    return _data_control_plane.get_workspace_model(workspace_id)


def get_workspace_object(workspace_id: str) -> Mapping[str, Any]:
    return get_workspace_model(workspace_id).to_dict()


def list_browser_session_models():
    return _data_control_plane.list_browser_session_models()


def list_browser_session_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_browser_session_models()]


def list_device_models():
    return _data_control_plane.list_device_models()


def list_device_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_device_models()]


def get_local_capacity_model():
    return _data_control_plane.get_local_capacity_model()


def get_local_capacity_object() -> Mapping[str, Any]:
    return get_local_capacity_model().to_dict()


def list_local_io_models():
    return _data_control_plane.list_local_io_models()


def list_local_io_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_local_io_models()]


def get_reliability_model(*, webspace_id: str | None = None):
    return _data_control_plane.get_reliability_model(webspace_id=webspace_id)


def get_reliability_projection(*, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_reliability_model(webspace_id=webspace_id).to_dict()


def get_reliability_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_reliability_model(webspace_id=webspace_id).objects]


def list_quota_models(*, webspace_id: str | None = None):
    return _data_control_plane.list_quota_models(webspace_id=webspace_id)


def list_quota_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in list_quota_models(webspace_id=webspace_id)]


def get_inventory_model():
    return _data_control_plane.get_inventory_model()


def get_inventory_projection() -> Mapping[str, Any]:
    return get_inventory_model().to_dict()


def get_inventory_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_inventory_model().objects]


def get_neighborhood_model():
    return _data_control_plane.get_neighborhood_model()


def get_neighborhood_projection() -> Mapping[str, Any]:
    return get_neighborhood_model().to_dict()


def get_neighborhood_objects() -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_neighborhood_model().objects]


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
    "list_quota_models",
    "list_quota_objects",
    "get_inventory_model",
    "get_inventory_projection",
    "get_inventory_objects",
    "get_neighborhood_model",
    "get_neighborhood_projection",
    "get_neighborhood_objects",
]
