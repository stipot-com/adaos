from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adaos.sdk.core.exporter import export as sdk_export
from adaos.services.agent_context import get_ctx
from adaos.services.system_model import CANONICAL_KIND_REGISTRY, CANONICAL_RELATION_REGISTRY
from adaos.services.system_model.model import (
    CanonicalStatus,
    ConnectivityStatus,
    InstallationStatus,
    ResourcePressureStatus,
    SyncStatus,
    TrustStatus,
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _skill_manifest_schema() -> dict[str, Any]:
    return _load_json(_package_root() / "services" / "skill" / "skill_schema.json")


def _scenario_manifest_schema() -> dict[str, Any]:
    return _load_json(_package_root() / "abi" / "scenario.schema.json")


def _status_vocab() -> dict[str, list[str]]:
    return {
        "operational": [item.value for item in CanonicalStatus],
        "connectivity": [item.value for item in ConnectivityStatus],
        "trust": [item.value for item in TrustStatus],
        "resource_pressure": [item.value for item in ResourcePressureStatus],
        "sync": [item.value for item in SyncStatus],
        "installation": [item.value for item in InstallationStatus],
    }


def _template_names(raw: Any) -> list[str]:
    try:
        path = Path(raw() if callable(raw) else raw)
    except Exception:
        return []
    if not path.exists() or not path.is_dir():
        return []
    return sorted(item.name for item in path.iterdir() if item.is_dir() and not item.name.startswith("."))


def _template_catalog() -> dict[str, Any]:
    ctx = get_ctx()
    return {
        "skills": _template_names(getattr(ctx.paths, "skill_templates_dir", None)),
        "scenarios": _template_names(getattr(ctx.paths, "scenario_templates_dir", None)),
    }


def _system_model_vocabulary() -> dict[str, Any]:
    return {
        "kinds": sorted(CANONICAL_KIND_REGISTRY),
        "relations": sorted(CANONICAL_RELATION_REGISTRY),
        "statuses": _status_vocab(),
        "projection_classes": ["object", "reliability", "inventory", "neighborhood", "task_packet"],
    }


def _descriptor_entry(
    descriptor_id: str,
    *,
    title: str,
    summary: str,
    stability: str = "experimental",
    source_kind: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "descriptor_id": descriptor_id,
        "title": title,
        "summary": summary,
        "stability": stability,
        "publication_mode": "root-curated",
        "source": {
            "kind": source_kind,
            "published_by": "root",
        },
        "tags": list(tags or []),
    }


def list_descriptor_sets() -> list[dict[str, Any]]:
    return [
        _descriptor_entry(
            "sdk_metadata",
            title="SDK metadata",
            summary="Root-curated metadata view over the AdaOS SDK exporter.",
            source_kind="internal_sdk_export",
            tags=["development", "sdk", "metadata"],
        ),
        _descriptor_entry(
            "system_model_vocabulary",
            title="System model vocabulary",
            summary="Canonical kinds, relations, statuses, and projection classes used by AdaOS.",
            source_kind="system_model_registry",
            tags=["development", "system-model", "vocabulary"],
        ),
        _descriptor_entry(
            "skill_manifest_schema",
            title="Skill manifest schema",
            summary="Current JSON schema for skill manifests and related runtime metadata.",
            source_kind="skill_manifest_schema",
            tags=["development", "skill", "schema"],
        ),
        _descriptor_entry(
            "scenario_manifest_schema",
            title="Scenario manifest schema",
            summary="Current JSON schema for scenario manifests.",
            source_kind="scenario_manifest_schema",
            tags=["development", "scenario", "schema"],
        ),
        _descriptor_entry(
            "template_catalog",
            title="Template catalog",
            summary="Built-in skill and scenario template names available for scaffolding workflows.",
            source_kind="template_catalog",
            tags=["development", "templates", "scaffold"],
        ),
    ]


def descriptor_registry_summary() -> dict[str, Any]:
    items = list_descriptor_sets()
    return {
        "available": True,
        "publication_mode": "root-curated",
        "descriptor_count": len(items),
        "descriptors": [item["descriptor_id"] for item in items],
    }


def get_descriptor_set(descriptor_id: str, *, level: str = "std") -> dict[str, Any]:
    token = str(descriptor_id or "").strip().lower()
    if token == "sdk_metadata":
        effective_level = str(level or "std").strip().lower() or "std"
        if effective_level not in {"mini", "std", "rich"}:
            effective_level = "std"
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {
            **entry,
            "level": effective_level,
            "payload": sdk_export(level=effective_level),
        }
    if token == "system_model_vocabulary":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": _system_model_vocabulary()}
    if token == "skill_manifest_schema":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": _skill_manifest_schema()}
    if token == "scenario_manifest_schema":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": _scenario_manifest_schema()}
    if token == "template_catalog":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": _template_catalog()}
    raise KeyError(token)


__all__ = [
    "descriptor_registry_summary",
    "get_descriptor_set",
    "list_descriptor_sets",
]
