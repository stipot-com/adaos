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
            "operations.issue.tokens",
            surface="operations",
            risk="medium",
            summary="Issue bounded Root MCP access tokens for external clients.",
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
