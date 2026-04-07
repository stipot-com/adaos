from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core._ctx import require_ctx


def get_self_model():
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_node_object

    return current_node_object()


def get_self_object() -> Mapping[str, Any]:
    return get_self_model().to_dict()


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


def get_reliability_model(*, webspace_id: str | None = None):
    require_ctx("sdk.data.control_plane")
    from adaos.services.system_model.service import current_reliability_projection

    return current_reliability_projection(webspace_id=webspace_id)


def get_reliability_projection(*, webspace_id: str | None = None) -> Mapping[str, Any]:
    return get_reliability_model(webspace_id=webspace_id).to_dict()


def get_reliability_objects(*, webspace_id: str | None = None) -> list[Mapping[str, Any]]:
    return [item.to_dict() for item in get_reliability_model(webspace_id=webspace_id).objects]


__all__ = [
    "get_self_model",
    "get_self_object",
    "list_skill_models",
    "list_skill_objects",
    "get_skill_model",
    "get_skill_object",
    "list_scenario_models",
    "list_scenario_objects",
    "get_scenario_model",
    "get_scenario_object",
    "get_reliability_model",
    "get_reliability_projection",
    "get_reliability_objects",
]
