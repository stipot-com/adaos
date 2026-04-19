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


DEFAULT_SESSION_TTL_SECONDS = 4 * 60 * 60
MAX_SESSION_TTL_SECONDS = 24 * 60 * 60

DEFAULT_CAPABILITY_PROFILES: dict[str, list[str]] = {
    "ProfileOpsRead": [
        *DEFAULT_BEARER_CAPABILITIES,
        "operations.read.targets",
        "hub.memory.get_status",
        "hub.memory.list_sessions",
        "hub.memory.get_session",
        "hub.memory.list_incidents",
        "hub.memory.list_artifacts",
        "hub.memory.get_artifact",
    ],
    "ProfileOpsControl": [
        *DEFAULT_BEARER_CAPABILITIES,
        "operations.read.targets",
        "operations.issue.tokens",
        "operations.revoke.tokens",
        "hub.memory.get_status",
        "hub.memory.list_sessions",
        "hub.memory.get_session",
        "hub.memory.list_incidents",
        "hub.memory.list_artifacts",
        "hub.memory.get_artifact",
        "hub.memory.start_profile",
        "hub.memory.stop_profile",
        "hub.memory.retry_profile",
        "hub.memory.publish_profile",
    ],
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sessions_path() -> Path:
    path = _state_dir() / "root_mcp" / "mcp_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _read_records() -> list[dict[str, Any]]:
    path = _sessions_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = payload if isinstance(payload, list) else payload.get("sessions") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    return [dict(item) for item in raw_items if isinstance(item, Mapping)]


def _write_records(items: list[dict[str, Any]]) -> None:
    _sessions_path().write_text(json.dumps({"sessions": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _sanitize_record(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload.pop("token_hash", None)
    return payload


def _effective_capabilities(*, capability_profile: str | None, capabilities: list[str] | None) -> list[str]:
    explicit = _normalize_strings(capabilities or [])
    if explicit:
        return explicit
    token = str(capability_profile or "").strip()
    if token and token in DEFAULT_CAPABILITY_PROFILES:
        return list(DEFAULT_CAPABILITY_PROFILES[token])
    return list(DEFAULT_BEARER_CAPABILITIES)


def get_mcp_session_lease_record(session_id: str) -> dict[str, Any] | None:
    token = str(session_id or "").strip()
    if not token:
        return None
    for item in reversed(_read_records()):
        if str(item.get("session_id") or "").strip() == token:
            return _sanitize_record(item)
    return None


def issue_mcp_session_lease(
    *,
    audience: str,
    actor: str,
    auth_method: str,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    capability_profile: str | None = None,
    capabilities: list[str] | None = None,
    subnet_id: str | None = None,
    zone: str | None = None,
    target_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    audience_token = str(audience or "").strip()
    target_token = str(target_id or "").strip()
    if not audience_token:
        raise ValueError("audience is required")
    if not target_token:
        raise ValueError("target_id is required")
    target = get_managed_target(target_token)
    if target is None:
        raise ValueError(f"managed target '{target_token}' is not registered")
    ttl = max(60, min(int(ttl_seconds or DEFAULT_SESSION_TTL_SECONDS), MAX_SESSION_TTL_SECONDS))
    session_id = new_id()
    secret = f"rmcp_session_{new_id()}{new_id()}"
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=ttl)
    profile_token = str(capability_profile or "").strip() or None
    effective_capabilities = _effective_capabilities(
        capability_profile=profile_token,
        capabilities=capabilities,
    )
    record = {
        "session_id": session_id,
        "token_hash": _hash_token(secret),
        "audience": audience_token,
        "actor": str(actor or "root:unknown"),
        "auth_method": str(auth_method or "unknown"),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl,
        "status": "active",
        "capability_profile": profile_token,
        "capabilities": effective_capabilities,
        "subnet_id": str(subnet_id or "").strip() or getattr(target, "subnet_id", None),
        "zone": str(zone or "").strip() or getattr(target, "zone", None),
        "target_id": target_token,
        "target_ids": [target_token],
        "note": str(note or "").strip() or None,
        "last_used_at": None,
        "use_count": 0,
        "last_tool_id": None,
        "revoked_at": None,
        "revoked_by": None,
        "revoked_auth_method": None,
        "revocation_reason": None,
    }
    items = _read_records()
    items.append(record)
    _write_records(items)
    return {
        "session_id": session_id,
        "access_token": secret,
        "audience": audience_token,
        "issued_at": record["issued_at"],
        "expires_at": record["expires_at"],
        "ttl_seconds": ttl,
        "capability_profile": profile_token,
        "capabilities": effective_capabilities,
        "subnet_id": record["subnet_id"],
        "zone": record["zone"],
        "target_id": target_token,
        "target_ids": [target_token],
        "last_used_at": None,
        "use_count": 0,
        "status": "active",
    }


def validate_mcp_session_lease(token: str, *, tool_id: str | None = None) -> dict[str, Any] | None:
    token_hash = _hash_token(str(token or "").strip())
    if not token_hash:
        return None
    now = datetime.now(timezone.utc)
    items = _read_records()
    matched: dict[str, Any] | None = None
    changed = False
    for item in reversed(items):
        if str(item.get("status") or "").strip().lower() != "active":
            continue
        if str(item.get("token_hash") or "").strip() != token_hash:
            continue
        try:
            expires_at = datetime.fromisoformat(str(item.get("expires_at") or ""))
        except Exception:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            item["status"] = "expired"
            changed = True
            continue
        item["last_used_at"] = _iso_now()
        item["use_count"] = max(0, int(item.get("use_count") or 0)) + 1
        item["last_tool_id"] = str(tool_id or "").strip() or item.get("last_tool_id")
        matched = dict(item)
        changed = True
        break
    if changed:
        _write_records(items)
    return matched


def list_mcp_session_leases(
    *,
    limit: int = 100,
    status: str | None = None,
    audience: str | None = None,
    target_id: str | None = None,
    capability_profile: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    items = list(reversed(_read_records()))
    out: list[dict[str, Any]] = []
    status_filter = str(status or "").strip().lower() or None
    audience_filter = str(audience or "").strip() or None
    target_filter = str(target_id or "").strip() or None
    profile_filter = str(capability_profile or "").strip() or None
    now = datetime.now(timezone.utc)
    for item in items:
        record_status = str(item.get("status") or "").strip().lower() or "unknown"
        if status_filter and record_status != status_filter:
            continue
        if audience_filter and str(item.get("audience") or "").strip() != audience_filter:
            continue
        if target_filter and str(item.get("target_id") or "").strip() != target_filter:
            continue
        if profile_filter and str(item.get("capability_profile") or "").strip() != profile_filter:
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


def revoke_mcp_session_lease(
    session_id: str,
    *,
    actor: str,
    auth_method: str,
    reason: str | None = None,
) -> dict[str, Any]:
    token = str(session_id or "").strip()
    if not token:
        raise ValueError("session_id is required")
    items = _read_records()
    revoked: dict[str, Any] | None = None
    for item in items:
        if str(item.get("session_id") or "").strip() != token:
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


def mcp_session_registry_summary() -> dict[str, Any]:
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
        "registry_path": str(_sessions_path()),
        "issued_count": len(items),
        "active_count": active,
        "profiles": sorted(DEFAULT_CAPABILITY_PROFILES.keys()),
    }


__all__ = [
    "DEFAULT_CAPABILITY_PROFILES",
    "DEFAULT_SESSION_TTL_SECONDS",
    "MAX_SESSION_TTL_SECONDS",
    "get_mcp_session_lease_record",
    "issue_mcp_session_lease",
    "list_mcp_session_leases",
    "mcp_session_registry_summary",
    "revoke_mcp_session_lease",
    "validate_mcp_session_lease",
]
