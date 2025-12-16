from __future__ import annotations

from importlib import import_module

__all__ = [
    "ScenarioControlService",
    "ScenarioManager",
    "WebUIRegistryEntry",
    "WebspaceScenarioRuntime",
    "ProjectionBackend",
    "ProjectionTarget",
    "ProjectionRule",
    "ProjectionRegistry",
    "ProjectionService",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "ScenarioControlService": ("adaos.services.scenario.control", "ScenarioControlService"),
    "ScenarioManager": ("adaos.services.scenario.manager", "ScenarioManager"),
    "WebUIRegistryEntry": ("adaos.services.scenario.webspace_runtime", "WebUIRegistryEntry"),
    "WebspaceScenarioRuntime": ("adaos.services.scenario.webspace_runtime", "WebspaceScenarioRuntime"),
    "ProjectionBackend": ("adaos.services.scenario.projection_registry", "ProjectionBackend"),
    "ProjectionTarget": ("adaos.services.scenario.projection_registry", "ProjectionTarget"),
    "ProjectionRule": ("adaos.services.scenario.projection_registry", "ProjectionRule"),
    "ProjectionRegistry": ("adaos.services.scenario.projection_registry", "ProjectionRegistry"),
    "ProjectionService": ("adaos.services.scenario.projection_service", "ProjectionService"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    mod, attr = target
    return getattr(import_module(mod), attr)

