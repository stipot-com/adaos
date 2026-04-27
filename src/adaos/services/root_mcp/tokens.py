from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id

from .policy import DEFAULT_BEARER_CAPABILITIES
from .targets import get_managed_target


DEFAULT_ACCESS_TOKEN_CAPABILITIES: list[str] = list(DEFAULT_BEARER_CAPABILITIES)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _root_mcp_state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.root_mcp_state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tokens_path() -> Path:
    path = _root_mcp_state_dir() / "access_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _read_records() -> list[dict[str, Any]]:
    path = _tokens_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = payload if isinstance(payload, list) else payload.get("tokens") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    return [dict(item) for item in raw_items if isinstance(item, Mapping)]


def _write_records(items: list[dict[str, Any]]) -> None:
    path = _tokens_path()
    payload = {"tokens": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_capabilities(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _normalize_target_ids(raw: Any) -> list[str]:
    return _normalize_capabilities(raw)


def _sanitize_record(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload.pop("token_hash", None)
    return payload


def get_access_token_record(token_id: str) -> dict[str, Any] | None:
    token = str(token_id or "").strip()
    if not token:
        return None
    for item in reversed(_read_records()):
        if str(item.get("token_id") or "").strip() == token:
            return _sanitize_record(item)
    return None


def issue_access_token(
    *,
    audience: str,
    actor: str,
    auth_method: str,
    ttl_seconds: int = 3600,
    capabilities: list[str] | None = None,
    subnet_id: str | None = None,
    zone: str | None = None,
    target_id: str | None = None,
    target_ids: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    audience_token = str(audience or "").strip()
    if not audience_token:
        raise ValueError("audience is required")
    ttl = max(60, min(int(ttl_seconds or 3600), 86_400))
    token_id = new_id()
    secret = f"rmcp_{new_id()}{new_id()}"
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=ttl)

    scoped_target_ids = _normalize_target_ids(target_ids or [])
    primary_target = str(target_id or "").strip() or None
    target = get_managed_target(primary_target) if primary_target else None
    if primary_target and target is None:
        raise ValueError(f"managed target '{primary_target}' is not registered")
    if target is not None and primary_target not in scoped_target_ids:
        scoped_target_ids.append(primary_target)

    effective_subnet_id = str(subnet_id or "").strip() or (target.subnet_id if target is not None else None)
    effective_zone = str(zone or "").strip() or (target.zone if target is not None else None)
    effective_capabilities = _normalize_capabilities(capabilities or list(DEFAULT_ACCESS_TOKEN_CAPABILITIES))

    record = {
        "token_id": token_id,
        "token_hash": _hash_token(secret),
        "audience": audience_token,
        "actor": str(actor or "root:unknown"),
        "auth_method": str(auth_method or "unknown"),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl,
        "status": "active",
        "capabilities": effective_capabilities,
        "subnet_id": effective_subnet_id,
        "zone": effective_zone,
        "target_ids": scoped_target_ids,
        "primary_target_id": primary_target,
        "note": str(note or "").strip() or None,
        "revoked_at": None,
        "revoked_by": None,
        "revocation_reason": None,
    }
    items = _read_records()
    items.append(record)
    _write_records(items)
    return {
        "token_id": token_id,
        "access_token": secret,
        "audience": audience_token,
        "issued_at": record["issued_at"],
        "expires_at": record["expires_at"],
        "ttl_seconds": ttl,
        "capabilities": effective_capabilities,
        "subnet_id": effective_subnet_id,
        "zone": effective_zone,
        "target_ids": scoped_target_ids,
        "primary_target_id": primary_target,
    }


def validate_access_token(token: str) -> dict[str, Any] | None:
    token_hash = _hash_token(str(token or "").strip())
    if not token_hash:
        return None
    now = datetime.now(timezone.utc)
    for item in reversed(_read_records()):
        if str(item.get("status") or "").strip().lower() != "active":
            continue
        if str(item.get("token_hash") or "").strip() != token_hash:
            continue
        expires_at_raw = str(item.get("expires_at") or "").strip()
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except Exception:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            continue
        return dict(item)
    return None


def list_access_tokens(
    *,
    limit: int = 100,
    status: str | None = None,
    audience: str | None = None,
    target_id: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    items = list(reversed(_read_records()))
    out: list[dict[str, Any]] = []
    status_filter = str(status or "").strip().lower() or None
    audience_filter = str(audience or "").strip() or None
    target_filter = str(target_id or "").strip() or None
    now = datetime.now(timezone.utc)
    for item in items:
        record_status = str(item.get("status") or "").strip().lower() or "unknown"
        if status_filter and record_status != status_filter:
            continue
        if audience_filter and str(item.get("audience") or "").strip() != audience_filter:
            continue
        if target_filter and target_filter not in _normalize_target_ids(item.get("target_ids")):
            continue
        if active_only:
            if record_status != "active":
                continue
            try:
                expires_at = datetime.fromisoformat(str(item.get("expires_at") or ""))
            except Exception:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                continue
        out.append(_sanitize_record(item))
        if len(out) >= max(1, int(limit)):
            break
    return out


def revoke_access_token(
    token_id: str,
    *,
    actor: str,
    auth_method: str,
    reason: str | None = None,
) -> dict[str, Any]:
    token = str(token_id or "").strip()
    if not token:
        raise ValueError("token_id is required")
    items = _read_records()
    revoked: dict[str, Any] | None = None
    for item in items:
        if str(item.get("token_id") or "").strip() != token:
            continue
        if str(item.get("status") or "").strip().lower() == "revoked":
            revoked = dict(item)
            break
        item["status"] = "revoked"
        item["revoked_at"] = _iso_now()
        item["revoked_by"] = str(actor or "root:unknown")
        item["revoked_auth_method"] = str(auth_method or "unknown")
        item["revocation_reason"] = str(reason or "").strip() or None
        revoked = dict(item)
        break
    if revoked is None:
        raise KeyError(token)
    _write_records(items)
    return _sanitize_record(revoked)


def access_token_registry_summary() -> dict[str, Any]:
    items = _read_records()
    active = 0
    now = datetime.now(timezone.utc)
    for item in items:
        if str(item.get("status") or "").strip().lower() != "active":
            continue
        try:
            expires_at = datetime.fromisoformat(str(item.get("expires_at") or ""))
        except Exception:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now:
            active += 1
    return {
        "available": True,
        "registry_path": str(_tokens_path()),
        "issued_count": len(items),
        "active_count": active,
        "default_capability_count": len(DEFAULT_ACCESS_TOKEN_CAPABILITIES),
    }


__all__ = [
    "DEFAULT_ACCESS_TOKEN_CAPABILITIES",
    "access_token_registry_summary",
    "get_access_token_record",
    "issue_access_token",
    "list_access_tokens",
    "revoke_access_token",
    "validate_access_token",
]
