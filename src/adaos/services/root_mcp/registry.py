from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from adaos.build_info import BUILD_INFO
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
from .sessions import DEFAULT_CAPABILITY_PROFILES, mcp_session_registry_summary
from .targets import managed_target_registry_summary
from .tokens import DEFAULT_ACCESS_TOKEN_CAPABILITIES, access_token_registry_summary


DESCRIPTOR_CACHE_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "sdk": {"ttl_seconds": 900, "stability": "experimental", "freshness": "fresh"},
    "vocabulary": {"ttl_seconds": 3600, "stability": "stable", "freshness": "fresh"},
    "schema": {"ttl_seconds": 3600, "stability": "stable", "freshness": "fresh"},
    "templates": {"ttl_seconds": 900, "stability": "experimental", "freshness": "fresh"},
    "architecture": {"ttl_seconds": 1800, "stability": "experimental", "freshness": "fresh"},
    "policy": {"ttl_seconds": 600, "stability": "experimental", "freshness": "fresh"},
    "client": {"ttl_seconds": 600, "stability": "experimental", "freshness": "fresh"},
    "auth": {"ttl_seconds": 300, "stability": "experimental", "freshness": "fresh"},
    "registry": {"ttl_seconds": 300, "stability": "experimental", "freshness": "fresh"},
    "build": {"ttl_seconds": 600, "stability": "experimental", "freshness": "fresh"},
    "bundle": {"ttl_seconds": 600, "stability": "experimental", "freshness": "fresh"},
}


def _plane_registry_payload() -> dict[str, Any]:
    return {
        "available": True,
        "kind": "mcp_plane_registry",
        "planes": [
            {
                "plane_id": "adaos_dev",
                "title": "AdaOSDevPlane",
                "enabled": True,
                "surface": "development",
                "mode": "typed_descriptive_plane",
                "published_by": "root",
                "preferred_for": ["llm_programmer", "authoring", "architecture_assistance"],
                "descriptor_ids": [
                    "architecture_catalog",
                    "sdk_metadata",
                    "template_catalog",
                    "public_skill_registry_summary",
                    "public_scenario_registry_summary",
                ],
                "tool_prefixes": ["adaos_dev."],
                "capability_profiles": [],
                "backing_store": "root_descriptor_cache",
            },
            {
                "plane_id": "profile_ops",
                "title": "ProfileOpsPlane",
                "enabled": True,
                "surface": "operations",
                "mode": "typed_operational_plane",
                "published_by": "root",
                "preferred_for": ["profiler_inspection", "profiler_control", "operator_workflows"],
                "descriptor_ids": ["capability_profiles", "mcp_session_profile"],
                "tool_prefixes": ["hub.memory."],
                "capability_profiles": ["ProfileOpsRead", "ProfileOpsControl"],
                "backing_store": "root_descriptor_cache + supervisor_authority",
            },
        ],
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _with_ttl(issued_at: str, ttl_seconds: int) -> str:
    base = datetime.fromisoformat(issued_at)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=max(1, int(ttl_seconds)))).replace(microsecond=0).isoformat()


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _descriptor_cache_state_path() -> Path:
    ctx = get_ctx()
    path = Path(ctx.paths.state_dir()) / "root_mcp" / "descriptor_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_descriptor_cache_state() -> dict[str, Any]:
    return _load_json(_descriptor_cache_state_path())


def _write_descriptor_cache_state(payload: dict[str, Any]) -> None:
    _descriptor_cache_state_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def record_descriptor_refresh(
    *,
    reason: str,
    descriptor_ids: list[str],
    source_kind: str,
    artifact_kind: str | None = None,
    artifact_name: str | None = None,
) -> dict[str, Any]:
    now = _iso_now()
    current = _read_descriptor_cache_state()
    refresh_count = max(0, int(current.get("refresh_count") or 0)) + 1
    payload = {
        "enabled": True,
        "cache_mode": "root_descriptor_cache",
        "updated_at": now,
        "refresh_count": refresh_count,
        "last_refresh": {
            "at": now,
            "reason": str(reason or "").strip() or "manual",
            "source_kind": str(source_kind or "").strip() or "unknown",
            "artifact_kind": str(artifact_kind or "").strip() or None,
            "artifact_name": str(artifact_name or "").strip() or None,
            "descriptor_ids": [str(item).strip() for item in descriptor_ids if str(item).strip()],
        },
    }
    _write_descriptor_cache_state(payload)
    return payload


def descriptor_cache_summary() -> dict[str, Any]:
    current = _read_descriptor_cache_state()
    return {
        "enabled": True,
        "cache_mode": "root_descriptor_cache",
        "state_path": str(_descriptor_cache_state_path()),
        "refresh_count": int(current.get("refresh_count") or 0),
        "updated_at": current.get("updated_at"),
        "last_refresh": dict(current.get("last_refresh") or {}) if isinstance(current.get("last_refresh"), dict) else None,
    }


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


def _workspace_registry_path() -> Path:
    ctx = get_ctx()
    return Path(ctx.paths.workspace_dir()) / "registry.json"


def _workspace_registry() -> dict[str, Any]:
    return _load_json(_workspace_registry_path())


def _registry_entries(kind: str) -> list[dict[str, Any]]:
    registry = _workspace_registry()
    items = registry.get(kind) if isinstance(registry.get(kind), list) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def _public_registry_summary(kind: str) -> dict[str, Any]:
    token = str(kind or "").strip().lower()
    items = _registry_entries(token)
    normalized: list[dict[str, Any]] = []
    for item in items[:50]:
        normalized.append(
            {
                "id": str(item.get("id") or item.get("name") or "").strip(),
                "name": str(item.get("name") or item.get("id") or "").strip(),
                "version": str(item.get("version") or "").strip() or None,
                "updated_at": str(item.get("updated_at") or "").strip() or None,
                "description": str(item.get("description") or "").strip() or None,
                "manifest": str(item.get("manifest") or "").strip() or None,
            }
        )
    registry_payload = _workspace_registry()
    return {
        "kind": token,
        "available": True,
        "registry_path": str(_workspace_registry_path()),
        "updated_at": str(registry_payload.get("updated_at") or "").strip() or None,
        "item_count": len(items),
        "items": normalized,
    }


def _architecture_catalog() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[4] / "docs" / "architecture" / "index.md"
    pages: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    pattern = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<link>[^\)]+)\):\s*(?P<summary>.+)$", re.MULTILINE)
    for match in pattern.finditer(text):
        pages.append(
            {
                "title": match.group("title").strip(),
                "path": match.group("link").strip(),
                "summary": match.group("summary").strip(),
            }
        )
    return {
        "available": True,
        "index_path": str(path),
        "page_count": len(pages),
        "pages": pages,
    }


def _descriptor_build_profile() -> dict[str, Any]:
    sdk_meta = dict(sdk_export(level="mini").get("meta") or {})
    return {
        "available": True,
        "build_pipeline": "prototype",
        "generator": "adaos.sdk.core.exporter.export",
        "lifecycle_hooks": {
            "publish_refresh_enabled": True,
            "cache_state": descriptor_cache_summary(),
        },
        "input_sources": [
            "adaos.sdk.manage",
            "adaos.sdk.data",
            "docs/architecture/index.md",
            ".adaos/workspace/registry.json",
        ],
        "published_descriptor_ids": [
            "sdk_metadata",
            "system_model_vocabulary",
            "skill_manifest_schema",
            "scenario_manifest_schema",
            "template_catalog",
            "architecture_catalog",
            "public_skill_registry_summary",
            "public_scenario_registry_summary",
            "descriptor_bundle",
        ],
        "sdk_export_meta": sdk_meta,
    }


def _client_profile() -> dict[str, Any]:
    return {
        "recommended_client": "RootMcpClient",
        "connection": {
            "root_url": {"required": True, "type": "string"},
            "subnet_id": {"required": False, "type": "string"},
            "access_token": {"required": True, "type": "string"},
            "zone": {"required": False, "type": "string"},
            "mcp_session_lease": {
                "required": False,
                "type": "bearer",
                "summary": "When a root-issued MCP session lease is used, subnet and zone are restored server-side from the lease.",
            },
        },
        "headers": {
            "Authorization": "Bearer <access_token>",
            "X-AdaOS-Subnet-Id": "<subnet_id> (optional with session lease)",
            "X-AdaOS-Zone": "<zone> (optional with session lease)",
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
    source_kind: str,
    descriptor_class: str,
    stability: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    defaults = dict(DESCRIPTOR_CACHE_CLASS_DEFAULTS.get(str(descriptor_class), {}))
    ttl_seconds = int(defaults.get("ttl_seconds") or 600)
    effective_stability = str(stability or defaults.get("stability") or "experimental")
    return {
        "descriptor_id": descriptor_id,
        "title": title,
        "summary": summary,
        "descriptor_class": descriptor_class,
        "stability": effective_stability,
        "publication_mode": "root-curated",
        "source": {
            "kind": source_kind,
            "published_by": "root",
        },
        "cache": {
            "enabled": True,
            "mode": "root_descriptor_cache",
            "ttl_seconds": ttl_seconds,
            "freshness_policy": str(defaults.get("freshness") or "fresh"),
        },
        "tags": list(tags or []),
    }


def _descriptor_bundle_metadata(entry: dict[str, Any], payload: Any, *, level: str = "std") -> dict[str, Any]:
    issued_at = _iso_now()
    cache = dict(entry.get("cache") or {})
    ttl_seconds = int(cache.get("ttl_seconds") or 600)
    return {
        "descriptor_id": entry["descriptor_id"],
        "level": level,
        "generated_at": issued_at,
        "fresh_until": _with_ttl(issued_at, ttl_seconds),
        "ttl_seconds": ttl_seconds,
        "freshness": {
            "state": "fresh",
            "cache_mode": str(cache.get("mode") or "root_descriptor_cache"),
            "served_from": "root",
        },
        "provenance": {
            "source_kind": entry["source"]["kind"],
            "published_by": entry["source"]["published_by"],
            "build_version": BUILD_INFO.version,
            "build_date": BUILD_INFO.build_date,
            "content_hash": _json_hash(payload),
        },
    }


def _descriptor_payload(descriptor_id: str, *, level: str = "std") -> Any:
    token = str(descriptor_id or "").strip().lower()
    if token == "sdk_metadata":
        effective_level = str(level or "std").strip().lower() or "std"
        if effective_level not in {"mini", "std", "rich"}:
            effective_level = "std"
        return sdk_export(level=effective_level)
    if token == "system_model_vocabulary":
        return _system_model_vocabulary()
    if token == "skill_manifest_schema":
        return _skill_manifest_schema()
    if token == "scenario_manifest_schema":
        return _scenario_manifest_schema()
    if token == "template_catalog":
        return _template_catalog()
    if token == "capability_registry":
        return capability_registry_payload()
    if token == "mcp_plane_registry":
        return _plane_registry_payload()
    if token == "mcp_client_profile":
        return _client_profile()
    if token == "access_token_profile":
        return access_token_registry_summary()
    if token == "capability_profiles":
        return {
            "available": True,
            "kind": "named_capability_profiles",
            "profiles": [
                {
                    "profile_id": profile_id,
                    "capabilities": list(capabilities),
                }
                for profile_id, capabilities in sorted(DEFAULT_CAPABILITY_PROFILES.items())
            ],
        }
    if token == "mcp_session_profile":
        return {
            "session_registry": mcp_session_registry_summary(),
            "client_bootstrap": {
                "mode": "bearer_only",
                "subnet_transport_params_required": False,
                "issuer": "root",
            },
        }
    if token == "architecture_catalog":
        return _architecture_catalog()
    if token == "public_skill_registry_summary":
        return _public_registry_summary("skills")
    if token == "public_scenario_registry_summary":
        return _public_registry_summary("scenarios")
    if token == "descriptor_build_profile":
        return _descriptor_build_profile()
    if token == "descriptor_bundle":
        descriptor_ids = [
            item["descriptor_id"]
            for item in list_descriptor_sets()
            if item["descriptor_id"] != "descriptor_bundle"
        ]
        items = [get_descriptor_set(item_id, level=level) for item_id in descriptor_ids]
        return {
            "bundle_id": "root_descriptor_bundle",
            "descriptor_count": len(items),
            "descriptors": items,
        }
    raise KeyError(token)


def list_descriptor_sets() -> list[dict[str, Any]]:
    return [
        _descriptor_entry(
            "sdk_metadata",
            title="SDK metadata",
            summary="Root-curated metadata view over the AdaOS SDK exporter.",
            source_kind="internal_sdk_export",
            descriptor_class="sdk",
            tags=["development", "sdk", "metadata"],
        ),
        _descriptor_entry(
            "system_model_vocabulary",
            title="System model vocabulary",
            summary="Canonical kinds, relations, statuses, and projection classes used by AdaOS.",
            source_kind="system_model_registry",
            descriptor_class="vocabulary",
            tags=["development", "system-model", "vocabulary"],
        ),
        _descriptor_entry(
            "skill_manifest_schema",
            title="Skill manifest schema",
            summary="Current JSON schema for skill manifests and related runtime metadata.",
            source_kind="skill_manifest_schema",
            descriptor_class="schema",
            tags=["development", "skill", "schema"],
        ),
        _descriptor_entry(
            "scenario_manifest_schema",
            title="Scenario manifest schema",
            summary="Current JSON schema for scenario manifests.",
            source_kind="scenario_manifest_schema",
            descriptor_class="schema",
            tags=["development", "scenario", "schema"],
        ),
        _descriptor_entry(
            "template_catalog",
            title="Template catalog",
            summary="Built-in skill and scenario template names available for scaffolding workflows.",
            source_kind="template_catalog",
            descriptor_class="templates",
            tags=["development", "templates", "scaffold"],
        ),
        _descriptor_entry(
            "architecture_catalog",
            title="Architecture catalog",
            summary="Root-curated catalog of AdaOS architecture pages and control-plane references.",
            source_kind="docs_architecture_index",
            descriptor_class="architecture",
            tags=["development", "architecture", "docs"],
        ),
        _descriptor_entry(
            "capability_registry",
            title="Capability registry",
            summary="Root MCP capability classes, default grants, and risk hints.",
            source_kind="root_mcp_policy_registry",
            descriptor_class="policy",
            tags=["development", "policy", "capabilities"],
        ),
        _descriptor_entry(
            "mcp_plane_registry",
            title="MCP plane registry",
            summary="Published Root MCP plane registry covering descriptive and operational product surfaces over the foundation.",
            source_kind="root_mcp_plane_registry",
            descriptor_class="registry",
            tags=["development", "planes", "registry"],
        ),
        _descriptor_entry(
            "mcp_client_profile",
            title="MCP client profile",
            summary="Root MCP client configuration shape for external tools such as Codex or VS Code integrations.",
            source_kind="root_mcp_client_profile",
            descriptor_class="client",
            tags=["development", "client", "integration"],
        ),
        _descriptor_entry(
            "access_token_profile",
            title="Access token profile",
            summary="Bounded Root MCP access-token defaults and registry summary.",
            source_kind="root_mcp_access_token_registry",
            descriptor_class="auth",
            tags=["development", "auth", "tokens"],
        ),
        _descriptor_entry(
            "capability_profiles",
            title="Capability profiles",
            summary="Named capability profiles for root-issued MCP session leases and future plane-scoped bootstrap flows.",
            source_kind="root_mcp_capability_profiles",
            descriptor_class="auth",
            tags=["development", "auth", "profiles"],
        ),
        _descriptor_entry(
            "mcp_session_profile",
            title="MCP session profile",
            summary="Root-issued MCP session lease registry and bearer-only client bootstrap guidance.",
            source_kind="root_mcp_session_registry",
            descriptor_class="auth",
            tags=["development", "auth", "sessions"],
        ),
        _descriptor_entry(
            "public_skill_registry_summary",
            title="Public skill registry summary",
            summary="Root-curated summary of published workspace skill entries for descriptive MCP clients.",
            source_kind="workspace_registry",
            descriptor_class="registry",
            tags=["development", "skills", "registry"],
        ),
        _descriptor_entry(
            "public_scenario_registry_summary",
            title="Public scenario registry summary",
            summary="Root-curated summary of published workspace scenario entries for descriptive MCP clients.",
            source_kind="workspace_registry",
            descriptor_class="registry",
            tags=["development", "scenarios", "registry"],
        ),
        _descriptor_entry(
            "descriptor_build_profile",
            title="Descriptor build profile",
            summary="Prototype build profile that turns SDK export, docs, and workspace registry inputs into root-curated descriptive bundles.",
            source_kind="root_descriptor_build_profile",
            descriptor_class="build",
            tags=["development", "build", "pipeline"],
        ),
        _descriptor_entry(
            "descriptor_bundle",
            title="Descriptor bundle",
            summary="Root-built bundle over the current descriptive registry for LLM bootstrap and cache-backed development workflows.",
            source_kind="root_descriptor_bundle",
            descriptor_class="bundle",
            tags=["development", "bundle", "cache"],
        ),
    ]


def descriptor_registry_summary() -> dict[str, Any]:
    items = list_descriptor_sets()
    return {
        "available": True,
        "publication_mode": "root-curated",
        "cache_mode": "root_descriptor_cache",
        "descriptor_count": len(items),
        "descriptors": [item["descriptor_id"] for item in items],
        "descriptor_classes": sorted({str(item.get("descriptor_class") or "").strip() for item in items if str(item.get("descriptor_class") or "").strip()}),
        "cache_policies": {
            key: {"ttl_seconds": int(value.get("ttl_seconds") or 0), "stability": str(value.get("stability") or "experimental")}
            for key, value in sorted(DESCRIPTOR_CACHE_CLASS_DEFAULTS.items())
        },
        "descriptor_cache": descriptor_cache_summary(),
        "capability_registry": capability_registry_summary(),
        "managed_target_registry": managed_target_registry_summary(),
        "control_report_registry": control_report_registry_summary(),
        "access_token_registry": access_token_registry_summary(),
        "mcp_session_registry": mcp_session_registry_summary(),
    }


def get_descriptor_set(descriptor_id: str, *, level: str = "std") -> dict[str, Any]:
    token = str(descriptor_id or "").strip().lower()
    effective_level = str(level or "std").strip().lower() or "std"
    if effective_level not in {"mini", "std", "rich"}:
        effective_level = "std"
    entry = next((item for item in list_descriptor_sets() if item["descriptor_id"] == token), None)
    if entry is None:
        raise KeyError(token)
    payload = _descriptor_payload(token, level=effective_level)
    return {
        **entry,
        "level": effective_level,
        "metadata": _descriptor_bundle_metadata(entry, payload, level=effective_level),
        "payload": payload,
    }


__all__ = [
    "descriptor_cache_summary",
    "descriptor_registry_summary",
    "get_descriptor_set",
    "list_descriptor_sets",
    "record_descriptor_refresh",
]
