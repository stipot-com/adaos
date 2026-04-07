from __future__ import annotations

from typing import Any, Iterable

from adaos.services.system_model.model import (
    CanonicalActionDescriptor,
    CanonicalGovernance,
    CanonicalObject,
    CanonicalProjection,
    compact_mapping,
    normalize_kind,
)

_INFRA_ROLES = ["role:infra-operator"]
_WORKSPACE_ROLES = ["role:workspace-operator", "role:infra-operator"]
_DEVELOPER_ROLES = ["role:developer", "role:infra-operator"]
_OWNER_ROLES = ["role:owner", "role:infra-operator"]

_DEFAULT_ROLES_BY_KIND: dict[str, list[str]] = {
    "root": _INFRA_ROLES,
    "node": _INFRA_ROLES,
    "hub": _INFRA_ROLES,
    "member": _INFRA_ROLES,
    "runtime": _INFRA_ROLES,
    "connection": _INFRA_ROLES,
    "capacity": _INFRA_ROLES,
    "quota": _INFRA_ROLES,
    "io_endpoint": _INFRA_ROLES,
    "policy": _INFRA_ROLES,
    "skill": _DEVELOPER_ROLES,
    "scenario": _DEVELOPER_ROLES,
    "workspace": _WORKSPACE_ROLES,
    "browser_session": _WORKSPACE_ROLES,
    "device": _WORKSPACE_ROLES,
    "profile": _OWNER_ROLES,
}


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def default_roles_for_kind(kind: Any) -> list[str]:
    token = normalize_kind(kind)
    if not token:
        return []
    return list(_DEFAULT_ROLES_BY_KIND.get(token) or [])


def default_visibility(*, tenant_id: str | None = None, owner_id: str | None = None) -> list[str]:
    return _dedupe([tenant_id or "", owner_id or ""])


def apply_action_defaults(
    action: CanonicalActionDescriptor,
    *,
    target_kind: str | None = None,
    requires_role: str | None = None,
) -> CanonicalActionDescriptor:
    kind = normalize_kind(target_kind) or str(target_kind or "").strip().lower() or None
    fallback_role = requires_role or next(iter(default_roles_for_kind(kind)), None)
    metadata = compact_mapping(
        {
            **dict(action.metadata or {}),
            "target_kind": kind,
        }
    )
    return CanonicalActionDescriptor(
        id=action.id,
        title=action.title,
        requires_role=str(action.requires_role or fallback_role or "").strip() or None,
        risk=action.risk,
        affects=list(action.affects or []),
        preconditions=compact_mapping(dict(action.preconditions or {})),
        metadata=metadata,
    )


def apply_governance_defaults(
    obj: CanonicalObject,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
    visibility: Iterable[str] | None = None,
    roles_allowed: Iterable[str] | None = None,
    shared_with: Iterable[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CanonicalObject:
    kind = normalize_kind(obj.kind) or str(obj.kind or "").strip().lower() or "object"
    current = obj.governance if isinstance(obj.governance, CanonicalGovernance) else CanonicalGovernance()
    merged_visibility = _dedupe(
        [
            *(current.visibility or []),
            *(list(visibility) if visibility is not None else default_visibility(tenant_id=tenant_id, owner_id=owner_id)),
        ]
    )
    merged_roles = _dedupe([*(current.roles_allowed or []), *(list(roles_allowed) if roles_allowed is not None else default_roles_for_kind(kind))])
    merged_shared = _dedupe([*(current.shared_with or []), *(list(shared_with) if shared_with is not None else [])])
    merged_metadata = compact_mapping(
        {
            **dict(current.metadata or {}),
            **dict(metadata or {}),
            "kind": kind,
        }
    )
    obj.governance = CanonicalGovernance(
        tenant_id=str(current.tenant_id or tenant_id or "").strip() or None,
        owner_id=str(current.owner_id or owner_id or "").strip() or None,
        visibility=merged_visibility,
        roles_allowed=merged_roles,
        shared_with=merged_shared,
        metadata=merged_metadata,
    )
    obj.actions = [apply_action_defaults(action, target_kind=kind) for action in list(obj.actions or [])]
    return obj


def apply_projection_governance(
    projection: CanonicalProjection,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
) -> CanonicalProjection:
    apply_governance_defaults(projection.subject, tenant_id=tenant_id, owner_id=owner_id)
    projection.objects = [
        apply_governance_defaults(item, tenant_id=tenant_id, owner_id=owner_id)
        for item in list(projection.objects or [])
    ]
    llm = projection.representations.get("llm") if isinstance(projection.representations.get("llm"), dict) else {}
    llm["governance"] = compact_mapping(
        {
            "tenant_id": tenant_id,
            "owner_id": owner_id,
        }
    )
    projection.representations["llm"] = compact_mapping(llm)
    return projection


__all__ = [
    "apply_action_defaults",
    "apply_governance_defaults",
    "apply_projection_governance",
    "default_roles_for_kind",
    "default_visibility",
]
