from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .model import RootMcpToolContract
from .targets import get_managed_target


DEFAULT_BEARER_CAPABILITIES: list[str] = [
    "development.read.foundation",
    "development.read.contracts",
    "development.read.descriptors",
    "development.read.system_model",
    "development.read.skill_contracts",
    "development.read.scenario_contracts",
    "operations.read.contracts",
    "operations.read.targets",
    "audit.read",
]


def _parse_capabilities(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item for item in (token.strip() for token in str(raw).split(",")) if item]


def _normalize_target_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _normalize_surface_capabilities(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _surface_supports(surface_capabilities: list[str], *, tool_id: str, required_capability: str | None) -> bool:
    if not surface_capabilities:
        return False
    if "*" in surface_capabilities:
        return True
    if tool_id in surface_capabilities:
        return True
    token = str(required_capability or "").strip()
    if token and token in surface_capabilities:
        return True
    if token:
        prefix = token.split(".", 1)[0] + ".*"
        if prefix in surface_capabilities:
            return True
    return False


def _capability_entry(
    capability: str,
    *,
    surface: str,
    risk: str,
    summary: str,
    default_grants: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "capability": capability,
        "surface": surface,
        "risk": risk,
        "summary": summary,
        "default_grants": list(default_grants or []),
    }


def list_capability_classes() -> list[dict[str, Any]]:
    return [
        _capability_entry(
            "development.read.foundation",
            surface="development",
            risk="low",
            summary="Read Root MCP foundation summary and surface status.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "development.read.contracts",
            surface="development",
            risk="low",
            summary="Read Root MCP development and operational tool contracts.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "development.read.descriptors",
            surface="development",
            risk="low",
            summary="Read root-curated descriptor catalog and descriptor payloads.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "development.read.system_model",
            surface="development",
            risk="low",
            summary="Read canonical system-model vocabulary and projection classes.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "development.read.skill_contracts",
            surface="development",
            risk="low",
            summary="Read skill manifest schemas and related development contracts.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "development.read.scenario_contracts",
            surface="development",
            risk="low",
            summary="Read scenario manifest schemas and related development contracts.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "operations.read.contracts",
            surface="operations",
            risk="low",
            summary="Read operational tool contract catalog.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "operations.read.targets",
            surface="operations",
            risk="low",
            summary="Read managed-target registry and target descriptors.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "operations.write.targets",
            surface="operations",
            risk="medium",
            summary="Register or update managed-target descriptors on root.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "operations.read.tokens",
            surface="operations",
            risk="medium",
            summary="Read bounded Root MCP access-token registry state for management UIs.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "operations.issue.tokens",
            surface="operations",
            risk="medium",
            summary="Issue bounded Root MCP access tokens for external clients.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "operations.revoke.tokens",
            surface="operations",
            risk="high",
            summary="Revoke previously issued Root MCP access tokens.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "audit.read",
            surface="audit",
            risk="medium",
            summary="Read Root MCP audit trail and operational event history.",
            default_grants=["owner_token", "bearer"],
        ),
        _capability_entry(
            "hub.get_status",
            surface="operations",
            risk="low",
            summary="Read target hub summary via infra_access_skill.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_runtime_summary",
            surface="operations",
            risk="low",
            summary="Read target hub runtime summary via infra_access_skill.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_operational_surface",
            surface="operations",
            risk="low",
            summary="Inspect the published infra_access_skill surface, web UI, and token-management state for a target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_activity_log",
            surface="operations",
            risk="low",
            summary="Read recent target activity derived from Root MCP audit and control-report history.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_capability_usage_summary",
            surface="operations",
            risk="low",
            summary="Read aggregated target capability-usage summaries for infra_access_skill observability.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_logs",
            surface="operations",
            risk="medium",
            summary="Read bounded target logs via infra_access_skill.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.run_healthchecks",
            surface="operations",
            risk="medium",
            summary="Run bounded healthchecks on a managed target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.issue_access_token",
            surface="operations",
            risk="medium",
            summary="Issue a bounded managed-target access token for external MCP clients.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.list_access_tokens",
            surface="operations",
            risk="medium",
            summary="List bounded managed-target access tokens for web UI and external MCP client management.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.revoke_access_token",
            surface="operations",
            risk="high",
            summary="Revoke a previously issued managed-target access token.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.issue_mcp_session",
            surface="operations",
            risk="medium",
            summary="Issue a target-scoped MCP session lease for external MCP clients such as Codex.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.list_mcp_sessions",
            surface="operations",
            risk="medium",
            summary="List target-scoped MCP session leases with deadline and usage metadata for operator UX.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.revoke_mcp_session",
            surface="operations",
            risk="high",
            summary="Revoke a previously issued target-scoped MCP session lease.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.get_status",
            surface="operations",
            risk="low",
            summary="Read root-published profiler status and latest session summary for a managed target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.list_sessions",
            surface="operations",
            risk="low",
            summary="List root-published profiler sessions for a managed target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.get_session",
            surface="operations",
            risk="low",
            summary="Read one root-published profiler session for a managed target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.list_incidents",
            surface="operations",
            risk="low",
            summary="List suspected profiler incidents for a managed target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.list_artifacts",
            surface="operations",
            risk="low",
            summary="List published profiler artifacts for a root-known profiler session.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.memory.get_artifact",
            surface="operations",
            risk="low",
            summary="Read one published profiler artifact for a root-known profiler session.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.deploy_ref",
            surface="operations",
            risk="high",
            summary="Deploy a ref to a test target through infra_access_skill.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.restart_service",
            surface="operations",
            risk="high",
            summary="Restart an allowlisted service on a test target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.run_allowed_tests",
            surface="operations",
            risk="high",
            summary="Run allowlisted tests on a test target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.get_test_results",
            surface="operations",
            risk="medium",
            summary="Read bounded test results from a target.",
            default_grants=["owner_token"],
        ),
        _capability_entry(
            "hub.rollback_last_test_deploy",
            surface="operations",
            risk="high",
            summary="Rollback the latest test deployment through infra_access_skill.",
            default_grants=["owner_token"],
        ),
    ]


def capability_registry_payload() -> dict[str, Any]:
    return {
        "classes": list_capability_classes(),
        "defaults": {
            "owner_token": ["*"],
            "bearer": list(DEFAULT_BEARER_CAPABILITIES),
        },
        "notes": {
            "write_policy": "Write-like operational capabilities are owner-token only by default in Phase 1.",
            "managed_targets": "Operational capabilities are expected to be mediated by infra_access_skill.",
        },
    }


def capability_registry_summary() -> dict[str, Any]:
    items = list_capability_classes()
    return {
        "available": True,
        "class_count": len(items),
        "capabilities": [item["capability"] for item in items],
    }


def _default_capabilities_for_auth_method(auth_method: str) -> tuple[list[str], str]:
    token = str(auth_method or "").strip().lower()
    if token == "owner_token":
        raw = _parse_capabilities(os.getenv("ADAOS_ROOT_MCP_OWNER_CAPABILITIES"))
        return (raw or ["*"], "owner_token_default")
    if token == "bearer":
        raw = _parse_capabilities(os.getenv("ADAOS_ROOT_MCP_BEARER_CAPABILITIES"))
        return (raw or list(DEFAULT_BEARER_CAPABILITIES), "bearer_default")
    return ([], "anonymous")


def has_capability(grants: list[str], required_capability: str | None) -> bool:
    token = str(required_capability or "").strip()
    if not token:
        return True
    if "*" in grants:
        return True
    if token in grants:
        return True
    prefix = token.split(".", 1)[0] + ".*"
    return prefix in grants


@dataclass(slots=True)
class RootMcpPolicyDecision:
    allowed: bool
    code: str = "allowed"
    message: str = ""
    policy_decision: str = "allow"
    required_capability: str | None = None
    granted_capabilities: list[str] = field(default_factory=list)
    grant_source: str | None = None
    target_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_meta(self) -> dict[str, Any]:
        return {
            "policy_decision": self.policy_decision,
            "required_capability": self.required_capability,
            "grant_source": self.grant_source,
            "granted_capabilities": list(self.granted_capabilities),
            **dict(self.meta or {}),
        }


def evaluate_direct_access(
    required_capability: str,
    *,
    actor: str,
    auth_method: str,
    auth_context: dict[str, Any] | None = None,
) -> RootMcpPolicyDecision:
    context = dict(auth_context or {})
    grants = _parse_capabilities(",".join(str(item) for item in context.get("capabilities") or []))
    grant_source = str(context.get("grant_source") or "").strip() or None
    if not grants:
        grants, grant_source = _default_capabilities_for_auth_method(auth_method)
    if not has_capability(grants, required_capability):
        return RootMcpPolicyDecision(
            allowed=False,
            code="forbidden",
            message=f"Actor '{actor}' is not allowed to use capability '{required_capability}'.",
            policy_decision="deny",
            required_capability=required_capability,
            granted_capabilities=grants,
            grant_source=grant_source,
            meta={"actor": actor},
        )
    return RootMcpPolicyDecision(
        allowed=True,
        required_capability=required_capability,
        granted_capabilities=grants,
        grant_source=grant_source,
        meta={"actor": actor},
    )


def evaluate_tool_access(
    contract: RootMcpToolContract,
    *,
    arguments: dict[str, Any] | None,
    actor: str,
    auth_method: str,
    scope: dict[str, Any] | None = None,
    auth_context: dict[str, Any] | None = None,
) -> RootMcpPolicyDecision:
    context = dict(auth_context or {})
    grants = _parse_capabilities(",".join(str(item) for item in context.get("capabilities") or []))
    grant_source = str(context.get("grant_source") or "").strip() or None
    if not grants:
        grants, grant_source = _default_capabilities_for_auth_method(auth_method)
    required_capability = contract.required_capability
    payload = dict(arguments or {})
    target_id = str(payload.get("target_id") or "").strip() or None
    effective_scope = dict(scope or {})

    if not has_capability(grants, required_capability):
        return RootMcpPolicyDecision(
            allowed=False,
            code="forbidden",
            message=f"Actor '{actor}' is not allowed to use MCP tool '{contract.id}'.",
            policy_decision="deny",
            required_capability=required_capability,
            granted_capabilities=grants,
            grant_source=grant_source,
            target_id=target_id,
            meta={"actor": actor},
        )

    if target_id:
        allowed_target_ids = _normalize_target_ids(context.get("allowed_target_ids") or [])
        if allowed_target_ids and target_id not in allowed_target_ids:
            return RootMcpPolicyDecision(
                allowed=False,
                code="target_forbidden",
                message=f"Actor '{actor}' is not allowed to access target '{target_id}'.",
                policy_decision="deny",
                required_capability=required_capability,
                granted_capabilities=grants,
                grant_source=grant_source,
                target_id=target_id,
                meta={"allowed_target_ids": allowed_target_ids},
            )
        target = get_managed_target(target_id)
        if target is None:
            return RootMcpPolicyDecision(
                allowed=False,
                code="target_not_found",
                message=f"Managed target '{target_id}' is not registered on root.",
                policy_decision="deny",
                required_capability=required_capability,
                granted_capabilities=grants,
                grant_source=grant_source,
                target_id=target_id,
            )

        scope_subnet = str(effective_scope.get("subnet_id") or "").strip()
        if scope_subnet and target.subnet_id and scope_subnet != target.subnet_id:
            return RootMcpPolicyDecision(
                allowed=False,
                code="scope_mismatch",
                message=f"Target '{target_id}' is outside the requested subnet scope.",
                policy_decision="deny",
                required_capability=required_capability,
                granted_capabilities=grants,
                grant_source=grant_source,
                target_id=target_id,
                meta={"scope_subnet_id": scope_subnet, "target_subnet_id": target.subnet_id},
            )

        scope_zone = str(effective_scope.get("zone") or "").strip()
        if scope_zone and target.zone and scope_zone != target.zone:
            return RootMcpPolicyDecision(
                allowed=False,
                code="zone_mismatch",
                message=f"Target '{target_id}' is outside the requested zone scope.",
                policy_decision="deny",
                required_capability=required_capability,
                granted_capabilities=grants,
                grant_source=grant_source,
                target_id=target_id,
                meta={"scope_zone": scope_zone, "target_zone": target.zone},
            )

        environment_scope = str(contract.metadata.get("environment_scope") or "").strip().lower()
        if contract.side_effects == "write" and environment_scope == "test-only" and target.environment != "test":
            return RootMcpPolicyDecision(
                allowed=False,
                code="environment_forbidden",
                message=f"Tool '{contract.id}' is restricted to test targets.",
                policy_decision="deny",
                required_capability=required_capability,
                granted_capabilities=grants,
                grant_source=grant_source,
                target_id=target_id,
                meta={"target_environment": target.environment},
            )

        if contract.id.startswith("hub."):
            operational_surface = dict(target.operational_surface or {})
            published_by = str(operational_surface.get("published_by") or "").strip()
            surface_enabled = bool(operational_surface.get("enabled"))
            surface_capabilities = _normalize_surface_capabilities(operational_surface.get("capabilities"))
            execution_mode = str(operational_surface.get("execution_mode") or "").strip().lower()
            token_management = dict(operational_surface.get("token_management") or {}) if isinstance(operational_surface.get("token_management"), dict) else {}
            report_verified = bool((target.meta or {}).get("report_verified"))
            require_verified_reports = str(os.getenv("ADAOS_ROOT_MCP_REQUIRE_VERIFIED_REPORTS") or "").strip().lower() in {"1", "true", "yes", "on"}

            if published_by and published_by != "skill:infra_access_skill":
                return RootMcpPolicyDecision(
                    allowed=False,
                    code="surface_mismatch",
                    message=f"Target '{target_id}' publishes an unexpected operational surface '{published_by}'.",
                    policy_decision="deny",
                    required_capability=required_capability,
                    granted_capabilities=grants,
                    grant_source=grant_source,
                    target_id=target_id,
                    meta={"published_by": published_by},
                )

            if not surface_enabled:
                return RootMcpPolicyDecision(
                    allowed=False,
                    code="surface_unavailable",
                    message=f"Target '{target_id}' does not currently expose an enabled infra_access_skill surface.",
                    policy_decision="deny",
                    required_capability=required_capability,
                    granted_capabilities=grants,
                    grant_source=grant_source,
                    target_id=target_id,
                    meta={"published_by": published_by or "skill:infra_access_skill"},
                )

            if not _surface_supports(surface_capabilities, tool_id=contract.id, required_capability=required_capability):
                return RootMcpPolicyDecision(
                    allowed=False,
                    code="capability_not_published",
                    message=f"Target '{target_id}' does not publish capability '{contract.id}'.",
                    policy_decision="deny",
                    required_capability=required_capability,
                    granted_capabilities=grants,
                    grant_source=grant_source,
                    target_id=target_id,
                    meta={"published_capabilities": surface_capabilities},
                )

            if contract.id in {
                "hub.issue_access_token",
                "hub.list_access_tokens",
                "hub.revoke_access_token",
                "hub.issue_mcp_session",
                "hub.list_mcp_sessions",
                "hub.revoke_mcp_session",
            }:
                if not bool(token_management.get("enabled")):
                    return RootMcpPolicyDecision(
                        allowed=False,
                        code="token_management_unavailable",
                        message=f"Target '{target_id}' does not currently expose token management through infra_access_skill.",
                        policy_decision="deny",
                        required_capability=required_capability,
                        granted_capabilities=grants,
                        grant_source=grant_source,
                        target_id=target_id,
                        meta={"token_management": token_management},
                    )
                issuer_mode = str(token_management.get("issuer_mode") or "").strip().lower() or "unknown"
                if issuer_mode != "root_mcp":
                    return RootMcpPolicyDecision(
                        allowed=False,
                        code="token_management_route_unavailable",
                        message=f"Target '{target_id}' does not publish a root_mcp token-management route.",
                        policy_decision="deny",
                        required_capability=required_capability,
                        granted_capabilities=grants,
                        grant_source=grant_source,
                        target_id=target_id,
                        meta={"issuer_mode": issuer_mode},
                    )

            required_execution_mode = str(contract.metadata.get("required_execution_mode") or "").strip().lower()
            if required_execution_mode and execution_mode != required_execution_mode:
                return RootMcpPolicyDecision(
                    allowed=False,
                    code="execution_route_unavailable",
                    message=f"Target '{target_id}' does not expose execution mode '{required_execution_mode}' for '{contract.id}'.",
                    policy_decision="deny",
                    required_capability=required_capability,
                    granted_capabilities=grants,
                    grant_source=grant_source,
                    target_id=target_id,
                    meta={
                        "required_execution_mode": required_execution_mode,
                        "target_execution_mode": execution_mode or "reported_only",
                    },
                )

            if require_verified_reports and not report_verified:
                return RootMcpPolicyDecision(
                    allowed=False,
                    code="report_unverified",
                    message=f"Target '{target_id}' has not produced a verified control report.",
                    policy_decision="deny",
                    required_capability=required_capability,
                    granted_capabilities=grants,
                    grant_source=grant_source,
                    target_id=target_id,
                    meta={"report_verified": report_verified},
                )

        return RootMcpPolicyDecision(
            allowed=True,
            required_capability=required_capability,
            granted_capabilities=grants,
            grant_source=grant_source,
            target_id=target_id,
            meta={
                "target_environment": target.environment,
                "target_subnet_id": target.subnet_id,
                "target_zone": target.zone,
                "target_surface_published_by": str((target.operational_surface or {}).get("published_by") or ""),
                "target_surface_enabled": bool((target.operational_surface or {}).get("enabled")),
                "target_surface_capabilities": _normalize_surface_capabilities((target.operational_surface or {}).get("capabilities")),
                "target_execution_mode": str((target.operational_surface or {}).get("execution_mode") or "").strip().lower() or "reported_only",
                "target_token_management_enabled": bool(token_management.get("enabled")),
                "target_token_issuer_mode": str(token_management.get("issuer_mode") or "").strip().lower() or "unknown",
                "report_verified": bool((target.meta or {}).get("report_verified")),
            },
        )

    return RootMcpPolicyDecision(
        allowed=True,
        required_capability=required_capability,
        granted_capabilities=grants,
        grant_source=grant_source,
        meta={"actor": actor},
    )


__all__ = [
    "DEFAULT_BEARER_CAPABILITIES",
    "RootMcpPolicyDecision",
    "capability_registry_payload",
    "capability_registry_summary",
    "evaluate_direct_access",
    "evaluate_tool_access",
    "has_capability",
    "list_capability_classes",
]
