"""SDK helpers for AdaOS control-plane object access."""

from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.data import control_plane as _data_control_plane


def get_self_model():
    return _data_control_plane.get_self_model()


def get_self_object() -> Mapping[str, Any]:
    return get_self_model().to_dict()


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
]
