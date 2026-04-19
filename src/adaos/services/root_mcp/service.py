from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from adaos.build_info import BUILD_INFO
from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id

from .audit import append_audit_event, list_audit_events, target_activity_feed, target_capability_usage_summary
from .infra_access import (
    deploy_local_ref,
    read_deploy_state,
    read_local_logs,
    read_test_results,
    restart_local_service,
    rollback_local_deploy,
    run_allowed_tests,
    run_local_healthchecks,
)
from .infra_access_skill import skill_state as infra_access_skill_state
from .model import (
    ROOT_MCP_RESPONSE_SCHEMA,
    RootMcpAuditEvent,
    RootMcpAvailability,
    RootMcpError,
    RootMcpResponseEnvelope,
    RootMcpSurface,
    RootMcpToolContract,
    schema_object,
)
from .policy import evaluate_tool_access
from .registry import descriptor_registry_summary, get_descriptor_set, list_descriptor_sets
from .reports import control_report_registry_summary, list_control_reports
from .sessions import (
    DEFAULT_CAPABILITY_PROFILES,
    get_mcp_session_lease_record,
    issue_mcp_session_lease,
    list_mcp_session_leases,
    mcp_session_registry_summary,
    revoke_mcp_session_lease,
)
from .targets import get_managed_target as get_target_descriptor
from .targets import list_managed_targets as list_target_descriptors
from .targets import managed_target_registry_summary
from .tokens import access_token_registry_summary, get_access_token_record, issue_access_token, list_access_tokens, revoke_access_token


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _root_host_summary() -> dict[str, Any]:
    ctx = get_ctx()
    conf = getattr(ctx, "config", None)
    role = str(getattr(conf, "role", "") or "").strip().lower() or "unknown"
    return {
        "node_id": getattr(conf, "node_id", None),
        "subnet_id": getattr(conf, "subnet_id", None),
        "host_role": role,
        "root_hosting_mode": "embedded_dev_root" if role == "hub" else "external_root_compatible",
    }


def _foundation_summary() -> dict[str, Any]:
    descriptors = list_descriptor_sets()
    managed_targets = list_target_descriptors()
    contracts = list_tool_contracts()
    return {
        "id": "root-mcp-foundation",
        "status": "experimental",
        "build": {
            "version": BUILD_INFO.version,
            "build_date": BUILD_INFO.build_date,
        },
        "root": _root_host_summary(),
        "surfaces": {
            "development": {
                "enabled": True,
                "mode": "root_descriptor_cache",
                "scope": "descriptor registry over internal services, schemas, and stable vocabularies",
            },
            "operations": {
                "enabled": True,
                "mode": "managed-target skeleton",
                "scope": "root-hosted contracts and managed-target descriptors before target-side infra_access_skill execution",
            },
        },
        "planes": {
            "adaos_dev": {
                "enabled": True,
                "mode": "typed_descriptive_plane",
                "backing_store": "root_descriptor_cache",
                "preferred_for": ["llm_programmer", "authoring", "architecture_assistance"],
            },
        },
        "entrypoints": {
            "foundation": "/v1/root/mcp/foundation",
            "contracts": "/v1/root/mcp/contracts",
            "descriptors": "/v1/root/mcp/descriptors",
            "targets": "/v1/root/mcp/targets",
            "access_tokens": "/v1/root/mcp/access-tokens",
            "mcp_sessions": "/v1/root/mcp/sessions",
            "call": "/v1/root/mcp/call",
            "audit": "/v1/root/mcp/audit",
            "control_reports": "/v1/hubs/control/reports",
        },
        "registries": {
            "descriptor_registry": descriptor_registry_summary(),
            "managed_target_registry": managed_target_registry_summary(),
            "control_report_registry": control_report_registry_summary(),
            "access_token_registry": access_token_registry_summary(),
            "mcp_session_registry": mcp_session_registry_summary(),
        },
        "managed_targets": {
            "count": len(managed_targets),
            "first_target": "test hub",
            "publication_model": "skill-mediated",
            "preferred_target_surface": "infra_access_skill",
            "registry_status": "report-backed skeleton",
        },
        "infra_access_skill": {
            "state": infra_access_skill_state(),
        },
        "client": {
            "recommended_client": "RootMcpClient",
            "configuration_fields": ["root_url", "access_token", "subnet_id?", "zone?"],
            "intended_use": "external MCP clients such as Codex/VS Code integrations",
            "preferred_bootstrap": "root-issued MCP Session Lease for bearer-only routing context",
        },
        "capability_profiles": {
            "available": True,
            "profiles": sorted(DEFAULT_CAPABILITY_PROFILES.keys()),
        },
        "descriptor_cache": {
            "enabled": True,
            "mode": "root_descriptor_cache",
            "freshness_contract": "ttl-backed pseudo-static descriptors served directly from root",
        },
        "preferred_descriptive_surface": "adaos_dev",
        "tool_contract_count": len(contracts),
    }


def _placeholder_operational_contracts() -> list[RootMcpToolContract]:
    return []


def _implemented_tool_contracts() -> list[RootMcpToolContract]:
    return [
        RootMcpToolContract(
            id="development.describe_foundation",
            title="Describe Root MCP Foundation",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return root-hosted MCP foundation summary and current surface status.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.foundation",
            metadata={"published_by": "root", "handler": "describe_foundation"},
        ),
        RootMcpToolContract(
            id="development.list_contracts",
            title="List tool contracts",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return current root MCP tool contracts, including operational placeholders.",
            input_schema=schema_object(
                properties={"surface": {"type": "string", "enum": [item.value for item in RootMcpSurface]}},
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.contracts",
            metadata={"published_by": "root", "handler": "list_contracts"},
        ),
        RootMcpToolContract(
            id="development.list_descriptor_sets",
            title="List descriptor sets",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the root-curated development descriptor catalog.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "root", "handler": "list_descriptor_sets"},
        ),
        RootMcpToolContract(
            id="development.get_descriptor_set",
            title="Get descriptor set",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return a root-curated descriptor payload such as sdk metadata, schemas, or template catalog.",
            input_schema=schema_object(
                properties={
                    "descriptor_id": {"type": "string"},
                    "level": {"type": "string", "enum": ["mini", "std", "rich"]},
                },
                required=["descriptor_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "root", "handler": "get_descriptor_set"},
        ),
        RootMcpToolContract(
            id="development.get_system_model_vocabulary",
            title="Get system model vocabulary",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the canonical system-model vocabulary published through the root descriptor registry.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.system_model",
            metadata={"published_by": "root", "handler": "get_system_model_vocabulary"},
        ),
        RootMcpToolContract(
            id="development.get_skill_manifest_schema",
            title="Get skill manifest schema",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the skill manifest schema published through the root descriptor registry.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.skill_contracts",
            metadata={"published_by": "root", "handler": "get_skill_manifest_schema"},
        ),
        RootMcpToolContract(
            id="development.get_scenario_manifest_schema",
            title="Get scenario manifest schema",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the scenario manifest schema published through the root descriptor registry.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.scenario_contracts",
            metadata={"published_by": "root", "handler": "get_scenario_manifest_schema"},
        ),
        RootMcpToolContract(
            id="adaos_dev.get_architecture_catalog",
            title="Get AdaOS architecture catalog",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the root-curated AdaOS architecture catalog for LLM-programmer and authoring workflows.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "plane:adaos_dev", "handler": "adaos_dev_get_architecture_catalog"},
        ),
        RootMcpToolContract(
            id="adaos_dev.get_sdk_metadata",
            title="Get AdaOS SDK metadata",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return SDK export metadata through the AdaOSDevPlane typed descriptive surface.",
            input_schema=schema_object(properties={"level": {"type": "string", "enum": ["mini", "std", "rich"]}}),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "plane:adaos_dev", "handler": "adaos_dev_get_sdk_metadata"},
        ),
        RootMcpToolContract(
            id="adaos_dev.get_template_catalog",
            title="Get template catalog",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return root-curated skill and scenario templates through AdaOSDevPlane.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "plane:adaos_dev", "handler": "adaos_dev_get_template_catalog"},
        ),
        RootMcpToolContract(
            id="adaos_dev.get_public_skill_registry",
            title="Get public skill registry summary",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the published workspace skill registry summary through AdaOSDevPlane.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "plane:adaos_dev", "handler": "adaos_dev_get_public_skill_registry"},
        ),
        RootMcpToolContract(
            id="adaos_dev.get_public_scenario_registry",
            title="Get public scenario registry summary",
            surface=RootMcpSurface.DEVELOPMENT,
            summary="Return the published workspace scenario registry summary through AdaOSDevPlane.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="development.read.descriptors",
            metadata={"published_by": "plane:adaos_dev", "handler": "adaos_dev_get_public_scenario_registry"},
        ),
        RootMcpToolContract(
            id="hub.get_status",
            title="Get hub status",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return the current managed-target descriptor and latest reported control state.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_status",
            metadata={"published_by": "root", "handler": "hub_get_status", "environment_scope": "test-first"},
        ),
        RootMcpToolContract(
            id="hub.get_runtime_summary",
            title="Get hub runtime summary",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return the latest reported runtime and transport summary for a managed target.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_runtime_summary",
            metadata={"published_by": "root", "handler": "hub_get_runtime_summary", "environment_scope": "test-first"},
        ),
        RootMcpToolContract(
            id="hub.get_operational_surface",
            title="Get target operational surface",
            surface=RootMcpSurface.OPERATIONS,
            summary="Inspect the published infra_access_skill surface, including web UI, observability, and token-management state.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_operational_surface",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_get_operational_surface",
                "environment_scope": "test-first",
            },
        ),
        RootMcpToolContract(
            id="hub.get_activity_log",
            title="Get target activity log",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return recent target activity derived from Root MCP audit and control-report history for infra_access_skill observability.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "errors_only": {"type": "boolean"},
                },
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_activity_log",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_get_activity_log",
                "environment_scope": "test-first",
            },
        ),
        RootMcpToolContract(
            id="hub.get_capability_usage_summary",
            title="Get target capability usage summary",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return aggregated capability-usage counts for a managed target using Root MCP audit history.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_capability_usage_summary",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_get_capability_usage_summary",
                "environment_scope": "test-first",
            },
        ),
        RootMcpToolContract(
            id="hub.get_logs",
            title="Get hub logs",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return a bounded tail of local pilot hub logs through the infra_access_skill local adapter.",
            input_schema=schema_object(
                properties={"target_id": {"type": "string"}, "tail": {"type": "integer", "minimum": 1}},
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_logs",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_get_logs",
                "environment_scope": "test-first",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.run_healthchecks",
            title="Run hub healthchecks",
            surface=RootMcpSurface.OPERATIONS,
            summary="Run bounded local pilot healthchecks through the infra_access_skill local adapter.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.run_healthchecks",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_run_healthchecks",
                "environment_scope": "test-first",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.restart_service",
            title="Restart test-hub service",
            surface=RootMcpSurface.OPERATIONS,
            summary="Restart an allowlisted local pilot service through the infra_access_skill local adapter.",
            input_schema=schema_object(
                properties={"target_id": {"type": "string"}, "service": {"type": "string"}},
                required=["target_id", "service"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.restart_service",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_restart_service",
                "environment_scope": "test-only",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.run_allowed_tests",
            title="Run allowed tests on test hub",
            surface=RootMcpSurface.OPERATIONS,
            summary="Run allowlisted local pilot tests through the infra_access_skill local adapter.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "tests": {"type": "array", "items": {"type": "string"}},
                },
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.run_allowed_tests",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_run_allowed_tests",
                "environment_scope": "test-only",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.get_test_results",
            title="Get test results",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return the latest stored local pilot test results for a managed target.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.get_test_results",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_get_test_results",
                "environment_scope": "test-first",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.deploy_ref",
            title="Deploy ref to test hub",
            surface=RootMcpSurface.OPERATIONS,
            summary="Apply a bounded local-pilot deployment ref through the infra_access_skill local adapter without direct shell or git mutation.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                required=["target_id", "ref"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.deploy_ref",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_deploy_ref",
                "environment_scope": "test-only",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="hub.rollback_last_test_deploy",
            title="Rollback last test deploy",
            surface=RootMcpSurface.OPERATIONS,
            summary="Rollback the latest bounded local-pilot deployment ref through the infra_access_skill local adapter.",
            input_schema=schema_object(properties={"target_id": {"type": "string"}}, required=["target_id"]),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.rollback_last_test_deploy",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_rollback_last_test_deploy",
                "environment_scope": "test-only",
                "required_execution_mode": "local_process",
            },
        ),
        RootMcpToolContract(
            id="operations.list_contracts",
            title="List operational contracts",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return the current operational contract catalog for managed targets and root-routed pilot operations.",
            input_schema=schema_object(),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="operations.read.contracts",
            metadata={"published_by": "root", "handler": "list_operational_contracts"},
        ),
        RootMcpToolContract(
            id="operations.list_managed_targets",
            title="List managed targets",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return root-visible managed targets and their published operational surface descriptors.",
            input_schema=schema_object(properties={"environment": {"type": "string"}}),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="operations.read.targets",
            metadata={"published_by": "root", "handler": "list_managed_targets"},
        ),
        RootMcpToolContract(
            id="operations.get_managed_target",
            title="Get managed target",
            surface=RootMcpSurface.OPERATIONS,
            summary="Return a single managed-target descriptor by target id.",
            input_schema=schema_object(
                properties={"target_id": {"type": "string"}},
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="operations.read.targets",
            metadata={"published_by": "root", "handler": "get_managed_target"},
        ),
        RootMcpToolContract(
            id="hub.issue_access_token",
            title="Issue managed-target access token",
            surface=RootMcpSurface.OPERATIONS,
            summary="Issue a bounded target-scoped access token through the target's published infra_access_skill token-management route.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "audience": {"type": "string"},
                    "ttl_seconds": {"type": "integer", "minimum": 60},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                required=["target_id", "audience"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.issue_access_token",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_issue_access_token",
                "environment_scope": "test-first",
                "token_management_route": "root_mcp",
            },
        ),
        RootMcpToolContract(
            id="hub.list_access_tokens",
            title="List managed-target access tokens",
            surface=RootMcpSurface.OPERATIONS,
            summary="List bounded target-scoped access tokens for web-client and MCP-client management.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "status": {"type": "string"},
                    "active_only": {"type": "boolean"},
                },
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.list_access_tokens",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_list_access_tokens",
                "environment_scope": "test-first",
                "token_management_route": "root_mcp",
            },
        ),
        RootMcpToolContract(
            id="hub.revoke_access_token",
            title="Revoke managed-target access token",
            surface=RootMcpSurface.OPERATIONS,
            summary="Revoke a bounded target-scoped access token through the target's published infra_access_skill token-management route.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "token_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                required=["target_id", "token_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.revoke_access_token",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_revoke_access_token",
                "environment_scope": "test-only",
                "token_management_route": "root_mcp",
            },
        ),
        RootMcpToolContract(
            id="hub.issue_mcp_session",
            title="Issue target MCP session lease",
            surface=RootMcpSurface.OPERATIONS,
            summary="Issue a target-scoped MCP session lease for external MCP clients such as Codex and VS Code integrations.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "audience": {"type": "string"},
                    "ttl_seconds": {"type": "integer", "minimum": 60},
                    "capability_profile": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                required=["target_id", "audience"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.issue_mcp_session",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_issue_mcp_session",
                "environment_scope": "test-first",
                "token_management_route": "root_mcp",
            },
        ),
        RootMcpToolContract(
            id="hub.list_mcp_sessions",
            title="List target MCP session leases",
            surface=RootMcpSurface.OPERATIONS,
            summary="List target-scoped MCP session leases with last-used and usage-count metadata for operator workflows.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "status": {"type": "string"},
                    "active_only": {"type": "boolean"},
                    "capability_profile": {"type": "string"},
                },
                required=["target_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.list_mcp_sessions",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_list_mcp_sessions",
                "environment_scope": "test-first",
                "token_management_route": "root_mcp",
            },
        ),
        RootMcpToolContract(
            id="hub.revoke_mcp_session",
            title="Revoke target MCP session lease",
            surface=RootMcpSurface.OPERATIONS,
            summary="Revoke a target-scoped MCP session lease through the target's published infra_access_skill session-management route.",
            input_schema=schema_object(
                properties={
                    "target_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                required=["target_id", "session_id"],
            ),
            output_schema=deepcopy(ROOT_MCP_RESPONSE_SCHEMA),
            required_capability="hub.revoke_mcp_session",
            side_effects="write",
            metadata={
                "published_by": "skill:infra_access_skill",
                "handler": "hub_revoke_mcp_session",
                "environment_scope": "test-only",
                "token_management_route": "root_mcp",
            },
        ),
    ]


def list_tool_contracts(*, surface: str | None = None) -> list[RootMcpToolContract]:
    items = [*_implemented_tool_contracts(), *_placeholder_operational_contracts()]
    if surface:
        token = str(surface or "").strip().lower()
        items = [item for item in items if item.surface.value == token]
    return items


def get_tool_contract(tool_id: str) -> RootMcpToolContract | None:
    token = str(tool_id or "").strip()
    if not token:
        return None
    for item in list_tool_contracts():
        if item.id == token:
            return item
    return None


def foundation_snapshot() -> dict[str, Any]:
    return _foundation_summary()


def list_descriptor_registry() -> list[dict[str, Any]]:
    return list_descriptor_sets()


def get_descriptor(descriptor_id: str, *, level: str = "std") -> dict[str, Any]:
    return get_descriptor_set(descriptor_id, level=level)


def list_managed_targets(
    *,
    environment: str | None = None,
    subnet_id: str | None = None,
    zone: str | None = None,
) -> list[dict[str, Any]]:
    return [item.to_dict() for item in list_target_descriptors(environment=environment, subnet_id=subnet_id, zone=zone)]


def get_managed_target(target_id: str) -> dict[str, Any] | None:
    item = get_target_descriptor(target_id)
    return item.to_dict() if item is not None else None


def recent_audit_events(
    *,
    limit: int = 50,
    tool_id: str | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
    target_id: str | None = None,
    subnet_id: str | None = None,
) -> list[dict]:
    return list_audit_events(
        limit=limit,
        tool_id=tool_id,
        trace_id=trace_id,
        actor=actor,
        target_id=target_id,
        subnet_id=subnet_id,
    )


def _handle_describe_foundation(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return foundation_snapshot()


def _handle_list_contracts(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    surface = str(arguments.get("surface") or "").strip().lower() or None
    return {"contracts": [item.to_dict() for item in list_tool_contracts(surface=surface)]}


def _handle_list_descriptor_sets(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {"descriptors": list_descriptor_registry(), "publication_mode": "root-curated"}


def _handle_get_descriptor_set(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    descriptor_id = str(arguments.get("descriptor_id") or "").strip()
    if not descriptor_id:
        raise ValueError("descriptor_id is required")
    level = str(arguments.get("level") or "std").strip().lower() or "std"
    return {"descriptor": get_descriptor(descriptor_id, level=level)}


def _handle_system_model_vocabulary(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return get_descriptor_set("system_model_vocabulary")["payload"]


def _handle_skill_manifest_schema(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {"schema": get_descriptor_set("skill_manifest_schema")["payload"]}


def _handle_scenario_manifest_schema(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {"schema": get_descriptor_set("scenario_manifest_schema")["payload"]}


def _handle_adaos_dev_descriptor(arguments: dict[str, Any], *, descriptor_id: str) -> dict[str, Any]:
    level = str(arguments.get("level") or "std").strip().lower() or "std"
    return {"descriptor": get_descriptor(descriptor_id, level=level)}


def _handle_operational_contracts(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    items = [item.to_dict() for item in list_tool_contracts(surface=RootMcpSurface.OPERATIONS.value)]
    return {
        "contracts": items,
        "managed_targets": {
            "first_target": "test hub",
            "publication_model": "skill-mediated",
            "preferred_surface": "infra_access_skill",
        },
    }


def _handle_managed_targets(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    environment = str(arguments.get("environment") or "").strip().lower() or None
    subnet_id = str(arguments.get("subnet_id") or "").strip() or None
    zone = str(arguments.get("zone") or "").strip() or None
    return {"targets": list_managed_targets(environment=environment, subnet_id=subnet_id, zone=zone)}


def _handle_get_managed_target(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_managed_target(target_id)
    if target is None:
        raise KeyError(target_id)
    return {"target": target}


def _latest_control_report(target_id: str) -> dict[str, Any] | None:
    items = list_control_reports(hub_id=target_id)
    if not items:
        return None
    latest = items[0]
    return dict(latest.get("report") or {}) if isinstance(latest, dict) else None


def _handle_hub_get_status(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_managed_target(target_id)
    if target is None:
        raise KeyError(target_id)
    report = _latest_control_report(target_id)
    return {
        "target": target,
        "lifecycle": dict((report or {}).get("lifecycle") or {}),
        "root_control": dict((report or {}).get("root_control") or {}),
        "route": dict((report or {}).get("route") or {}),
        "deployment_state": read_deploy_state(target_id=target_id),
        "last_reported_at": (report or {}).get("reported_at"),
    }


def _handle_hub_get_runtime_summary(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_managed_target(target_id)
    if target is None:
        raise KeyError(target_id)
    report = _latest_control_report(target_id)
    return {
        "target_id": target_id,
        "runtime": dict((report or {}).get("runtime") or {}),
        "transport": dict((report or {}).get("transport") or {}),
        "lifecycle": dict((report or {}).get("lifecycle") or {}),
        "operational_surface": dict((report or {}).get("operational_surface") or target.get("operational_surface") or {}),
        "deployment_state": read_deploy_state(target_id=target_id),
        "last_reported_at": (report or {}).get("reported_at"),
    }


def _handle_hub_get_operational_surface(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    report = _latest_control_report(target_id)
    surface = dict(target.operational_surface or {})
    token_management = dict(surface.get("token_management") or {}) if isinstance(surface.get("token_management"), dict) else {}
    return {
        "target_id": target_id,
        "operational_surface": surface,
        "webui": dict(surface.get("webui") or {}) if isinstance(surface.get("webui"), dict) else {},
        "observability": dict(surface.get("observability") or {}) if isinstance(surface.get("observability"), dict) else {},
        "token_management": token_management,
        "deployment_state": read_deploy_state(target_id=target_id),
        "access": dict(target.access or {}),
        "policy": dict(target.policy or {}),
        "report": {
            "last_reported_at": (report or {}).get("reported_at") or (target.meta or {}).get("last_reported_at"),
            "report_verified": bool((target.meta or {}).get("report_verified")),
            "report_auth_method": str((target.meta or {}).get("report_auth_method") or "").strip() or "unknown",
        },
    }


def _handle_hub_get_activity_log(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    limit = max(1, min(int(arguments.get("limit") or 50), 200))
    errors_only = bool(arguments.get("errors_only"))
    statuses = ["error"] if errors_only else None
    return {
        "target_id": target_id,
        "activity": target_activity_feed(
            target_id=target_id,
            limit=limit,
            statuses=statuses,
            include_control_reports=True,
        ),
    }


def _handle_hub_get_capability_usage_summary(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    limit = max(1, min(int(arguments.get("limit") or 200), 1000))
    return {
        "target_id": target_id,
        "usage": target_capability_usage_summary(
            target_id=target_id,
            limit=limit,
            include_control_reports=True,
        ),
    }


def _require_root_token_management(target_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    surface = _target_operational_surface(target_id)
    token_management = dict(surface.get("token_management") or {}) if isinstance(surface.get("token_management"), dict) else {}
    if not bool(token_management.get("enabled")):
        raise ValueError(f"target '{target_id}' does not expose token management through infra_access_skill")
    issuer_mode = str(token_management.get("issuer_mode") or "").strip().lower() or "unknown"
    if issuer_mode != "root_mcp":
        raise ValueError(f"target '{target_id}' does not expose a root_mcp token-management route")
    return surface, token_management


def _handle_hub_issue_access_token(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    audience = str(arguments.get("audience") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not audience:
        raise ValueError("audience is required")
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    surface, token_management = _require_root_token_management(target_id)
    ttl_seconds = int(arguments.get("ttl_seconds") or 3600)
    raw_caps = arguments.get("capabilities")
    capabilities = [str(item).strip() for item in raw_caps] if isinstance(raw_caps, list) else []
    note = str(arguments.get("note") or "").strip() or None
    issued = issue_access_token(
        audience=audience,
        actor="root:tool",
        auth_method="root_tool",
        ttl_seconds=ttl_seconds,
        capabilities=capabilities,
        subnet_id=target.subnet_id,
        zone=target.zone,
        target_id=target_id,
        note=note,
    )
    return {
        **issued,
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
    }


def _handle_hub_list_access_tokens(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    surface, token_management = _require_root_token_management(target_id)
    limit = max(1, min(int(arguments.get("limit") or 50), 200))
    status = str(arguments.get("status") or "").strip() or None
    active_only = bool(arguments.get("active_only"))
    return {
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
        "tokens": list_access_tokens(limit=limit, status=status, target_id=target_id, active_only=active_only),
    }


def _handle_hub_revoke_access_token(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    token_id = str(arguments.get("token_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not token_id:
        raise ValueError("token_id is required")
    surface, token_management = _require_root_token_management(target_id)
    record = get_access_token_record(token_id)
    if record is None:
        raise KeyError(token_id)
    target_ids = [str(item).strip() for item in record.get("target_ids") or [] if str(item).strip()]
    if target_id not in target_ids:
        raise ValueError(f"token '{token_id}' is not scoped to target '{target_id}'")
    reason = str(arguments.get("reason") or "").strip() or None
    revoked = revoke_access_token(
        token_id,
        actor="root:tool",
        auth_method="root_tool",
        reason=reason,
    )
    return {
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
        "token": revoked,
    }


def _handle_hub_issue_mcp_session(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    audience = str(arguments.get("audience") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not audience:
        raise ValueError("audience is required")
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    surface, token_management = _require_root_token_management(target_id)
    ttl_seconds = int(arguments.get("ttl_seconds") or 3600)
    capability_profile = str(arguments.get("capability_profile") or "").strip() or None
    raw_caps = arguments.get("capabilities")
    capabilities = [str(item).strip() for item in raw_caps] if isinstance(raw_caps, list) else []
    note = str(arguments.get("note") or "").strip() or None
    issued = issue_mcp_session_lease(
        audience=audience,
        actor="root:tool",
        auth_method="root_tool",
        ttl_seconds=ttl_seconds,
        capability_profile=capability_profile,
        capabilities=capabilities,
        subnet_id=target.subnet_id,
        zone=target.zone,
        target_id=target_id,
        note=note,
    )
    return {
        **issued,
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
    }


def _handle_hub_list_mcp_sessions(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    surface, token_management = _require_root_token_management(target_id)
    limit = max(1, min(int(arguments.get("limit") or 50), 200))
    status = str(arguments.get("status") or "").strip() or None
    capability_profile = str(arguments.get("capability_profile") or "").strip() or None
    active_only = bool(arguments.get("active_only"))
    return {
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
        "sessions": list_mcp_session_leases(
            limit=limit,
            status=status,
            target_id=target_id,
            capability_profile=capability_profile,
            active_only=active_only,
        ),
    }


def _handle_hub_revoke_mcp_session(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    session_id = str(arguments.get("session_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not session_id:
        raise ValueError("session_id is required")
    surface, token_management = _require_root_token_management(target_id)
    record = get_mcp_session_lease_record(session_id)
    if record is None:
        raise KeyError(session_id)
    target_ids = [str(item).strip() for item in record.get("target_ids") or [] if str(item).strip()]
    if target_id not in target_ids:
        raise ValueError(f"session '{session_id}' is not scoped to target '{target_id}'")
    reason = str(arguments.get("reason") or "").strip() or None
    revoked = revoke_mcp_session_lease(
        session_id,
        actor="root:tool",
        auth_method="root_tool",
        reason=reason,
    )
    return {
        "target_id": target_id,
        "issuer_mode": str(token_management.get("issuer_mode") or "").strip() or "root_mcp",
        "published_by": str(surface.get("published_by") or "").strip() or "skill:infra_access_skill",
        "session": revoked,
    }


def _target_execution_mode(target_id: str) -> str:
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    surface = dict(target.operational_surface or {})
    return str(surface.get("execution_mode") or "").strip().lower() or "reported_only"


def _target_operational_surface(target_id: str) -> dict[str, Any]:
    target = get_target_descriptor(target_id)
    if target is None:
        raise KeyError(target_id)
    return dict(target.operational_surface or {})


def _handle_hub_get_logs(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    tail = int(arguments.get("tail") or 200)
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "logs": read_local_logs(tail=tail),
    }


def _handle_hub_run_healthchecks(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "healthchecks": run_local_healthchecks(),
    }


def _handle_hub_restart_service(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    service_name = str(arguments.get("service") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not service_name:
        raise ValueError("service is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    surface = _target_operational_surface(target_id)
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "service": restart_local_service(
            service=service_name,
            allowed_services=surface.get("allowed_services") if isinstance(surface.get("allowed_services"), list) else [],
        ),
    }


def _handle_hub_run_allowed_tests(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    surface = _target_operational_surface(target_id)
    raw_tests = arguments.get("tests")
    requested_tests = [str(item).strip() for item in raw_tests if str(item).strip()] if isinstance(raw_tests, list) else []
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "tests": run_allowed_tests(
            target_id=target_id,
            allowed_test_paths=surface.get("allowed_test_paths") if isinstance(surface.get("allowed_test_paths"), list) else [],
            requested_tests=requested_tests,
        ),
    }


def _handle_hub_get_test_results(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "test_results": read_test_results(target_id=target_id),
    }


def _handle_hub_deploy_ref(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    ref = str(arguments.get("ref") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not ref:
        raise ValueError("ref is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    surface = _target_operational_surface(target_id)
    note = str(arguments.get("note") or "").strip() or None
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "deployment": deploy_local_ref(
            target_id=target_id,
            ref=ref,
            allowed_refs=surface.get("allowed_deploy_refs") if isinstance(surface.get("allowed_deploy_refs"), list) else [],
            note=note,
        ),
    }


def _handle_hub_rollback_last_test_deploy(arguments: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    target_id = str(arguments.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if _target_execution_mode(target_id) != "local_process":
        raise ValueError(f"target '{target_id}' does not expose local_process infra access execution")
    return {
        "target_id": target_id,
        "execution_mode": "local_process",
        "deployment": rollback_local_deploy(target_id=target_id),
    }


_HANDLERS: dict[str, Callable[[dict[str, Any], bool], dict[str, Any]]] = {
    "development.describe_foundation": lambda arguments, dry_run=False: _handle_describe_foundation(arguments, dry_run=dry_run),
    "development.list_contracts": lambda arguments, dry_run=False: _handle_list_contracts(arguments, dry_run=dry_run),
    "development.list_descriptor_sets": lambda arguments, dry_run=False: _handle_list_descriptor_sets(arguments, dry_run=dry_run),
    "development.get_descriptor_set": lambda arguments, dry_run=False: _handle_get_descriptor_set(arguments, dry_run=dry_run),
    "development.get_system_model_vocabulary": lambda arguments, dry_run=False: _handle_system_model_vocabulary(arguments, dry_run=dry_run),
    "development.get_skill_manifest_schema": lambda arguments, dry_run=False: _handle_skill_manifest_schema(arguments, dry_run=dry_run),
    "development.get_scenario_manifest_schema": lambda arguments, dry_run=False: _handle_scenario_manifest_schema(arguments, dry_run=dry_run),
    "adaos_dev.get_architecture_catalog": lambda arguments, dry_run=False: _handle_adaos_dev_descriptor(arguments, descriptor_id="architecture_catalog"),
    "adaos_dev.get_sdk_metadata": lambda arguments, dry_run=False: _handle_adaos_dev_descriptor(arguments, descriptor_id="sdk_metadata"),
    "adaos_dev.get_template_catalog": lambda arguments, dry_run=False: _handle_adaos_dev_descriptor(arguments, descriptor_id="template_catalog"),
    "adaos_dev.get_public_skill_registry": lambda arguments, dry_run=False: _handle_adaos_dev_descriptor(arguments, descriptor_id="public_skill_registry_summary"),
    "adaos_dev.get_public_scenario_registry": lambda arguments, dry_run=False: _handle_adaos_dev_descriptor(arguments, descriptor_id="public_scenario_registry_summary"),
    "hub.get_status": lambda arguments, dry_run=False: _handle_hub_get_status(arguments, dry_run=dry_run),
    "hub.get_runtime_summary": lambda arguments, dry_run=False: _handle_hub_get_runtime_summary(arguments, dry_run=dry_run),
    "hub.get_operational_surface": lambda arguments, dry_run=False: _handle_hub_get_operational_surface(arguments, dry_run=dry_run),
    "hub.get_activity_log": lambda arguments, dry_run=False: _handle_hub_get_activity_log(arguments, dry_run=dry_run),
    "hub.get_capability_usage_summary": lambda arguments, dry_run=False: _handle_hub_get_capability_usage_summary(arguments, dry_run=dry_run),
    "hub.get_logs": lambda arguments, dry_run=False: _handle_hub_get_logs(arguments, dry_run=dry_run),
    "hub.run_healthchecks": lambda arguments, dry_run=False: _handle_hub_run_healthchecks(arguments, dry_run=dry_run),
    "hub.restart_service": lambda arguments, dry_run=False: _handle_hub_restart_service(arguments, dry_run=dry_run),
    "hub.run_allowed_tests": lambda arguments, dry_run=False: _handle_hub_run_allowed_tests(arguments, dry_run=dry_run),
    "hub.get_test_results": lambda arguments, dry_run=False: _handle_hub_get_test_results(arguments, dry_run=dry_run),
    "hub.deploy_ref": lambda arguments, dry_run=False: _handle_hub_deploy_ref(arguments, dry_run=dry_run),
    "hub.rollback_last_test_deploy": lambda arguments, dry_run=False: _handle_hub_rollback_last_test_deploy(arguments, dry_run=dry_run),
    "hub.issue_access_token": lambda arguments, dry_run=False: _handle_hub_issue_access_token(arguments, dry_run=dry_run),
    "hub.list_access_tokens": lambda arguments, dry_run=False: _handle_hub_list_access_tokens(arguments, dry_run=dry_run),
    "hub.revoke_access_token": lambda arguments, dry_run=False: _handle_hub_revoke_access_token(arguments, dry_run=dry_run),
    "hub.issue_mcp_session": lambda arguments, dry_run=False: _handle_hub_issue_mcp_session(arguments, dry_run=dry_run),
    "hub.list_mcp_sessions": lambda arguments, dry_run=False: _handle_hub_list_mcp_sessions(arguments, dry_run=dry_run),
    "hub.revoke_mcp_session": lambda arguments, dry_run=False: _handle_hub_revoke_mcp_session(arguments, dry_run=dry_run),
    "operations.list_contracts": lambda arguments, dry_run=False: _handle_operational_contracts(arguments, dry_run=dry_run),
    "operations.list_managed_targets": lambda arguments, dry_run=False: _handle_managed_targets(arguments, dry_run=dry_run),
    "operations.get_managed_target": lambda arguments, dry_run=False: _handle_get_managed_target(arguments, dry_run=dry_run),
}


def _result_summary(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        keys = sorted(str(key) for key in result.keys())[:12]
        return {"kind": "object", "keys": keys}
    if isinstance(result, list):
        return {"kind": "list", "length": len(result)}
    return {"kind": type(result).__name__}


def _redactions_for_tool(tool_id: str) -> list[str]:
    token = str(tool_id or "").strip()
    if token in {"hub.issue_access_token", "root.access_tokens.issue"}:
        return ["result.access_token"]
    if token in {"hub.issue_mcp_session", "root.mcp_sessions.issue"}:
        return ["result.access_token"]
    return []


def _execution_adapter_for_tool(tool_id: str) -> str:
    token = str(tool_id or "").strip()
    if token in {"hub.get_status", "hub.get_runtime_summary", "hub.get_operational_surface"}:
        return "root.control_report_projection"
    if token == "hub.get_activity_log":
        return "root.audit_projection.activity"
    if token == "hub.get_capability_usage_summary":
        return "root.audit_projection.capability_usage"
    if token == "hub.get_logs":
        return "infra_access.local_process.logs"
    if token == "hub.run_healthchecks":
        return "infra_access.local_process.healthchecks"
    if token == "hub.restart_service":
        return "infra_access.local_process.service_restart"
    if token == "hub.run_allowed_tests":
        return "infra_access.local_process.pytest"
    if token == "hub.get_test_results":
        return "infra_access.local_process.test_results"
    if token == "hub.deploy_ref":
        return "infra_access.local_process.deploy_ref"
    if token == "hub.rollback_last_test_deploy":
        return "infra_access.local_process.rollback"
    if token == "hub.issue_access_token":
        return "root.access_token_issuer"
    if token == "hub.list_access_tokens":
        return "root.access_token_registry"
    if token == "hub.revoke_access_token":
        return "root.access_token_revocation"
    if token == "hub.issue_mcp_session":
        return "root.mcp_session_issuer"
    if token == "hub.list_mcp_sessions":
        return "root.mcp_session_registry"
    if token == "hub.revoke_mcp_session":
        return "root.mcp_session_revocation"
    if token.startswith("development."):
        return "root.descriptor_registry"
    if token.startswith("operations."):
        return "root.managed_target_registry"
    return "root.local_handler"


def _should_audit_tool(tool_id: str) -> bool:
    token = str(tool_id or "").strip()
    # These tools project the audit log itself; auditing every read causes
    # observability polling to continually grow the same file it scans.
    return token not in {
        "hub.get_activity_log",
        "hub.get_capability_usage_summary",
    }


def _trace_meta(
    *,
    tool_id: str,
    contract: RootMcpToolContract | None,
    arguments: dict[str, Any],
    scope_meta: dict[str, Any],
    policy_decision: Any,
    routing_mode: str,
    result_summary: dict[str, Any] | None = None,
    error_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy_meta = policy_decision.to_meta() if policy_decision is not None else {}
    return {
        "request": {
            "argument_keys": sorted(str(key) for key in arguments.keys())[:20],
            "target_id": str(arguments.get("target_id") or "").strip() or None,
            "scope": dict(scope_meta),
        },
        "policy": {
            "decision": str((policy_decision.policy_decision if policy_decision is not None else "allow") or "allow"),
            "required_capability": policy_meta.get("required_capability"),
            "grant_source": policy_meta.get("grant_source"),
        },
        "routing": {
            "mode": routing_mode,
            "surface": contract.surface.value if contract is not None else RootMcpSurface.DEVELOPMENT.value,
            "published_by": (contract.metadata.get("published_by") if contract is not None else None),
            "target_environment": policy_meta.get("target_environment"),
            "target_zone": policy_meta.get("target_zone"),
            "target_execution_mode": policy_meta.get("target_execution_mode"),
        },
        "result": dict(result_summary or {}),
        "error": dict(error_summary or {}),
        "redactions": _redactions_for_tool(tool_id),
    }


def invoke_tool(
    tool_id: str,
    *,
    arguments: dict[str, Any] | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    actor: str,
    auth_method: str,
    dry_run: bool = False,
    scope: dict[str, Any] | None = None,
    auth_context: dict[str, Any] | None = None,
) -> RootMcpResponseEnvelope:
    started_at = _iso_now()
    effective_request_id = str(request_id or new_id())
    effective_trace_id = str(trace_id or new_id())
    tool_token = str(tool_id or "").strip()
    contract = get_tool_contract(tool_token)
    payload_arguments = dict(arguments or {})
    scope_meta = dict(scope or {})
    policy_decision = None
    routing_mode = _execution_adapter_for_tool(tool_token)

    if contract is None:
        response = RootMcpResponseEnvelope(
            request_id=effective_request_id,
            trace_id=effective_trace_id,
            tool_id=tool_token or "unknown",
            surface=RootMcpSurface.DEVELOPMENT,
            ok=False,
            status="error",
            dry_run=bool(dry_run),
            error=RootMcpError(code="tool_not_found", message=f"Unknown MCP tool '{tool_token}'."),
            meta=scope_meta,
        )
    else:
        policy_decision = evaluate_tool_access(
            contract,
            arguments=payload_arguments,
            actor=actor,
            auth_method=auth_method,
            scope=scope_meta,
            auth_context=auth_context,
        )

        if not policy_decision.allowed:
            trace = _trace_meta(
                tool_id=contract.id,
                contract=contract,
                arguments=payload_arguments,
                scope_meta=scope_meta,
                policy_decision=policy_decision,
                routing_mode=routing_mode,
                error_summary={"code": policy_decision.code},
            )
            response = RootMcpResponseEnvelope(
                request_id=effective_request_id,
                trace_id=effective_trace_id,
                tool_id=contract.id,
                surface=contract.surface,
                ok=False,
                status="error",
                dry_run=bool(dry_run),
                error=RootMcpError(code=policy_decision.code, message=policy_decision.message),
                meta={**scope_meta, **policy_decision.to_meta(), "trace": trace, "redactions": _redactions_for_tool(contract.id)},
            )
        elif contract.availability is not RootMcpAvailability.ENABLED:
            trace = _trace_meta(
                tool_id=contract.id,
                contract=contract,
                arguments=payload_arguments,
                scope_meta=scope_meta,
                policy_decision=policy_decision,
                routing_mode=routing_mode,
                error_summary={"code": "tool_not_available", "availability": contract.availability.value},
            )
            response = RootMcpResponseEnvelope(
                request_id=effective_request_id,
                trace_id=effective_trace_id,
                tool_id=contract.id,
                surface=contract.surface,
                ok=False,
                status="error",
                dry_run=bool(dry_run),
                error=RootMcpError(
                    code="tool_not_available",
                    message=f"Tool '{contract.id}' is declared but not executable yet.",
                    details={"availability": contract.availability.value},
                ),
                meta={
                    "availability": contract.availability.value,
                    **scope_meta,
                    **policy_decision.to_meta(),
                    "trace": trace,
                    "redactions": _redactions_for_tool(contract.id),
                },
            )
        else:
            handler = _HANDLERS.get(contract.id)
            if handler is None:
                trace = _trace_meta(
                    tool_id=contract.id,
                    contract=contract,
                    arguments=payload_arguments,
                    scope_meta=scope_meta,
                    policy_decision=policy_decision,
                    routing_mode=routing_mode,
                    error_summary={"code": "handler_missing"},
                )
                response = RootMcpResponseEnvelope(
                    request_id=effective_request_id,
                    trace_id=effective_trace_id,
                    tool_id=contract.id,
                    surface=contract.surface,
                    ok=False,
                    status="error",
                    dry_run=bool(dry_run),
                    error=RootMcpError(code="handler_missing", message=f"No handler is registered for '{contract.id}'."),
                    meta={**scope_meta, **policy_decision.to_meta(), "trace": trace, "redactions": _redactions_for_tool(contract.id)},
                )
            else:
                try:
                    result = handler(payload_arguments, dry_run=bool(dry_run))
                    summary = _result_summary(result)
                    trace = _trace_meta(
                        tool_id=contract.id,
                        contract=contract,
                        arguments=payload_arguments,
                        scope_meta=scope_meta,
                        policy_decision=policy_decision,
                        routing_mode=routing_mode,
                        result_summary=summary,
                    )
                    response = RootMcpResponseEnvelope(
                        request_id=effective_request_id,
                        trace_id=effective_trace_id,
                        tool_id=contract.id,
                        surface=contract.surface,
                        ok=True,
                        status="ok",
                        dry_run=bool(dry_run),
                        result=result,
                        meta={
                            "availability": contract.availability.value,
                            "required_capability": contract.required_capability,
                            "stability": contract.stability,
                            "routing_mode": routing_mode,
                            **scope_meta,
                            **policy_decision.to_meta(),
                            "trace": trace,
                            "redactions": _redactions_for_tool(contract.id),
                        },
                    )
                except KeyError as exc:
                    missing = str(exc).strip("'")
                    trace = _trace_meta(
                        tool_id=contract.id,
                        contract=contract,
                        arguments=payload_arguments,
                        scope_meta=scope_meta,
                        policy_decision=policy_decision,
                        routing_mode=routing_mode,
                        error_summary={"code": "not_found", "missing": missing},
                    )
                    response = RootMcpResponseEnvelope(
                        request_id=effective_request_id,
                        trace_id=effective_trace_id,
                        tool_id=contract.id,
                        surface=contract.surface,
                        ok=False,
                        status="error",
                        dry_run=bool(dry_run),
                        error=RootMcpError(
                            code="not_found",
                            message=f"Requested MCP object '{missing}' was not found.",
                        ),
                        meta={**scope_meta, **policy_decision.to_meta(), "trace": trace, "redactions": _redactions_for_tool(contract.id)},
                    )
                except ValueError as exc:
                    trace = _trace_meta(
                        tool_id=contract.id,
                        contract=contract,
                        arguments=payload_arguments,
                        scope_meta=scope_meta,
                        policy_decision=policy_decision,
                        routing_mode=routing_mode,
                        error_summary={"code": "invalid_request"},
                    )
                    response = RootMcpResponseEnvelope(
                        request_id=effective_request_id,
                        trace_id=effective_trace_id,
                        tool_id=contract.id,
                        surface=contract.surface,
                        ok=False,
                        status="error",
                        dry_run=bool(dry_run),
                        error=RootMcpError(
                            code="invalid_request",
                            message=str(exc) or f"Invalid arguments for '{contract.id}'.",
                        ),
                        meta={**scope_meta, **policy_decision.to_meta(), "trace": trace, "redactions": _redactions_for_tool(contract.id)},
                    )
                except Exception as exc:
                    trace = _trace_meta(
                        tool_id=contract.id,
                        contract=contract,
                        arguments=payload_arguments,
                        scope_meta=scope_meta,
                        policy_decision=policy_decision,
                        routing_mode=routing_mode,
                        error_summary={"code": "execution_failed"},
                    )
                    response = RootMcpResponseEnvelope(
                        request_id=effective_request_id,
                        trace_id=effective_trace_id,
                        tool_id=contract.id,
                        surface=contract.surface,
                        ok=False,
                        status="error",
                        dry_run=bool(dry_run),
                        error=RootMcpError(
                            code="execution_failed",
                            message=f"Tool '{contract.id}' failed during execution.",
                            details={"error": str(exc)},
                        ),
                        meta={**scope_meta, **policy_decision.to_meta(), "trace": trace, "redactions": _redactions_for_tool(contract.id)},
                    )

    target_id = str(payload_arguments.get("target_id") or "").strip() or None
    audit_trace = dict(response.meta.get("trace") or {}) if isinstance(response.meta.get("trace"), dict) else {}
    if _should_audit_tool(response.tool_id):
        event = RootMcpAuditEvent(
            event_id=new_id(),
            request_id=response.request_id,
            trace_id=response.trace_id,
            tool_id=response.tool_id,
            surface=response.surface,
            actor=actor,
            auth_method=auth_method,
            capability=(contract.required_capability if contract else None),
            target_id=target_id,
            policy_decision=policy_decision.policy_decision if policy_decision is not None else "allow",
            execution_adapter=_execution_adapter_for_tool(response.tool_id),
            dry_run=bool(dry_run),
            status=response.status,
            started_at=started_at,
            finished_at=_iso_now(),
            result_summary=_result_summary(response.result) if response.ok else {},
            error=(response.error.to_dict() if response.error else {}),
            redactions=_redactions_for_tool(response.tool_id),
            meta={
                "tool_availability": contract.availability.value if contract else "missing",
                **scope_meta,
                **(policy_decision.to_meta() if policy_decision is not None else {}),
                "trace": audit_trace,
            },
        )
        append_audit_event(event)
        response.audit_event_id = event.event_id
    return response


__all__ = [
    "foundation_snapshot",
    "get_descriptor",
    "get_managed_target",
    "get_tool_contract",
    "invoke_tool",
    "list_descriptor_registry",
    "list_managed_targets",
    "list_tool_contracts",
    "recent_audit_events",
]
