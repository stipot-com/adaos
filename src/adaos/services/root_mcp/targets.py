from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from adaos.services.agent_context import get_ctx

from .model import RootMcpManagedTarget


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _registry_path() -> Path:
    path = _state_dir() / "root_mcp" / "managed_targets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _coerce_target(payload: Mapping[str, Any], *, source: str) -> RootMcpManagedTarget | None:
    target_id = str(payload.get("target_id") or "").strip()
    title = str(payload.get("title") or target_id or "").strip()
    kind = str(payload.get("kind") or "").strip()
    environment = str(payload.get("environment") or "").strip().lower()
    if not target_id or not title or not kind or not environment:
        return None
    meta = _normalize_mapping(payload.get("meta"))
    meta.setdefault("registry_source", source)
    return RootMcpManagedTarget(
        target_id=target_id,
        title=title,
        kind=kind,
        environment=environment,
        status=str(payload.get("status") or "unknown").strip() or "unknown",
        zone=str(payload.get("zone") or "").strip() or None,
        subnet_id=str(payload.get("subnet_id") or "").strip() or None,
        transport=_normalize_mapping(payload.get("transport")),
        operational_surface=_normalize_mapping(payload.get("operational_surface")),
        access=_normalize_mapping(payload.get("access")),
        policy=_normalize_mapping(payload.get("policy")),
        meta=meta,
    )


def _default_test_hub_descriptor() -> RootMcpManagedTarget:
    ctx = get_ctx()
    conf = getattr(ctx, "config", None)
    subnet_id = str(getattr(conf, "subnet_id", "") or "").strip() or "test-subnet"
    zone = str(os.getenv("ADAOS_ROOT_ZONE") or "local-dev")
    status = str(os.getenv("ADAOS_ROOT_TEST_HUB_STATUS") or "planned").strip().lower() or "planned"
    return RootMcpManagedTarget(
        target_id=f"hub:{subnet_id}",
        title="Test Hub",
        kind="hub",
        environment="test",
        status=status,
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
            "registry_source": "built_in",
            "notes": "Managed target descriptor published before target-side execution is wired.",
        },
    )


def _load_state_targets() -> list[RootMcpManagedTarget]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = payload if isinstance(payload, list) else payload.get("targets") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    out: list[RootMcpManagedTarget] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        coerced = _coerce_target(item, source="state_registry")
        if coerced is not None:
            out.append(coerced)
    return out


def _write_state_targets(items: list[RootMcpManagedTarget]) -> None:
    path = _registry_path()
    payload = {"targets": [item.to_dict() for item in items]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def managed_target_registry_summary() -> dict[str, Any]:
    persisted = _load_state_targets()
    merged = list_managed_targets()
    return {
        "available": True,
        "registry_path": str(_registry_path()),
        "persisted_count": len(persisted),
        "effective_count": len(merged),
        "first_target": "test hub",
    }


def list_managed_targets(
    *,
    environment: str | None = None,
    subnet_id: str | None = None,
    zone: str | None = None,
) -> list[RootMcpManagedTarget]:
    default_target = _default_test_hub_descriptor()
    merged: dict[str, RootMcpManagedTarget] = {default_target.target_id: default_target}
    for item in _load_state_targets():
        merged[item.target_id] = item
    items = sorted(merged.values(), key=lambda item: item.target_id)

    if environment:
        token = str(environment or "").strip().lower()
        items = [item for item in items if item.environment == token]
    if subnet_id:
        token = str(subnet_id or "").strip()
        items = [item for item in items if item.subnet_id == token]
    if zone:
        token = str(zone or "").strip()
        items = [item for item in items if item.zone == token]
    return items


def get_managed_target(target_id: str) -> RootMcpManagedTarget | None:
    token = str(target_id or "").strip()
    if not token:
        return None
    for item in list_managed_targets():
        if item.target_id == token:
            return item
    return None


def upsert_managed_target(payload: Mapping[str, Any]) -> RootMcpManagedTarget:
    item = _coerce_target(payload, source="state_registry")
    if item is None:
        raise ValueError("target payload must include target_id, title, kind, and environment")
    existing = {entry.target_id: entry for entry in _load_state_targets()}
    existing[item.target_id] = item
    _write_state_targets(list(existing.values()))
    return item


__all__ = [
    "get_managed_target",
    "list_managed_targets",
    "managed_target_registry_summary",
    "upsert_managed_target",
]
