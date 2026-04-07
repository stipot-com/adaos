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

from .policy import capability_registry_payload, capability_registry_summary
from .reports import control_report_registry_summary
from .targets import managed_target_registry_summary
from .tokens import DEFAULT_ACCESS_TOKEN_CAPABILITIES, access_token_registry_summary


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


def _client_profile() -> dict[str, Any]:
    return {
        "recommended_client": "RootMcpClient",
        "connection": {
            "root_url": {"required": True, "type": "string"},
            "subnet_id": {"required": True, "type": "string"},
            "access_token": {"required": True, "type": "string"},
            "zone": {"required": False, "type": "string"},
        },
        "headers": {
            "Authorization": "Bearer <access_token>",
            "X-AdaOS-Subnet-Id": "<subnet_id>",
            "X-AdaOS-Zone": "<zone>",
        },
        "access_token_defaults": {
            "capabilities": list(DEFAULT_ACCESS_TOKEN_CAPABILITIES),
        },
        "entrypoints": [
            "/v1/root/mcp/foundation",
            "/v1/root/mcp/contracts",
            "/v1/root/mcp/descriptors",
            "/v1/root/mcp/descriptors/{descriptor_id}",
            "/v1/root/mcp/targets",
            "/v1/root/mcp/call",
            "/v1/root/mcp/audit",
        ],
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
        _descriptor_entry(
            "capability_registry",
            title="Capability registry",
            summary="Root MCP capability classes, default grants, and risk hints.",
            source_kind="root_mcp_policy_registry",
            tags=["development", "policy", "capabilities"],
        ),
        _descriptor_entry(
            "mcp_client_profile",
            title="MCP client profile",
            summary="Root MCP client configuration shape for external tools such as Codex or VS Code integrations.",
            source_kind="root_mcp_client_profile",
            tags=["development", "client", "integration"],
        ),
        _descriptor_entry(
            "access_token_profile",
            title="Access token profile",
            summary="Bounded Root MCP access-token defaults and registry summary.",
            source_kind="root_mcp_access_token_registry",
            tags=["development", "auth", "tokens"],
        ),
    ]


def descriptor_registry_summary() -> dict[str, Any]:
    items = list_descriptor_sets()
    return {
        "available": True,
        "publication_mode": "root-curated",
        "descriptor_count": len(items),
        "descriptors": [item["descriptor_id"] for item in items],
        "capability_registry": capability_registry_summary(),
        "managed_target_registry": managed_target_registry_summary(),
        "control_report_registry": control_report_registry_summary(),
        "access_token_registry": access_token_registry_summary(),
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
    if token == "capability_registry":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": capability_registry_payload()}
    if token == "mcp_client_profile":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": _client_profile()}
    if token == "access_token_profile":
        entry = next(item for item in list_descriptor_sets() if item["descriptor_id"] == token)
        return {**entry, "payload": access_token_registry_summary()}
    raise KeyError(token)


__all__ = [
    "descriptor_registry_summary",
    "get_descriptor_set",
    "list_descriptor_sets",
]
