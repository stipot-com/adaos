from __future__ import annotations

import os

from adaos.services.agent_context import get_ctx

from .model import RootMcpManagedTarget


def _test_hub_descriptor() -> RootMcpManagedTarget:
    ctx = get_ctx()
    conf = getattr(ctx, "config", None)
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "test-subnet"
    zone = str(os.getenv("ADAOS_ROOT_ZONE") or "local-dev")
    return RootMcpManagedTarget(
        target_id=f"hub:{subnet_id}",
        title="Test Hub",
        kind="hub",
        environment="test",
        status="planned",
        zone=zone,
        subnet_id=subnet_id,
        transport={"channel": "hub_root_protocol", "mode": "existing-control-channel"},
        operational_surface={
            "published_by": "skill:infra_access_skill",
            "enabled": False,
            "availability": "planned",
            "capabilities": [
                "hub.get_status",
                "hub.get_runtime_summary",
                "hub.get_logs",
                "hub.run_healthchecks",
                "hub.issue_access_token",
            ],
        },
        access={
            "client_transport": "root_http_mcp",
            "client_config_fields": ["root_url", "subnet_id", "access_token", "zone"],
            "token_issuer": "skill:infra_access_skill",
            "status": "planned",
            "recommended_client": "RootMcpClient",
        },
        policy={
            "write_scope": "test-only",
            "target_role": "managed-target",
        },
        meta={
            "phase": "phase-1-skeleton",
            "notes": "Managed target descriptor published before target-side execution is wired.",
        },
    )


def list_managed_targets(*, environment: str | None = None) -> list[RootMcpManagedTarget]:
    items = [_test_hub_descriptor()]
    if environment:
        token = str(environment or "").strip().lower()
        items = [item for item in items if item.environment == token]
    return items


def get_managed_target(target_id: str) -> RootMcpManagedTarget | None:
    token = str(target_id or "").strip()
    if not token:
        return None
    for item in list_managed_targets():
        if item.target_id == token:
            return item
    return None


__all__ = ["get_managed_target", "list_managed_targets"]
