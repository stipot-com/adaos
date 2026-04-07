from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping


class CanonicalStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    WARNING = "warning"
    UNKNOWN = "unknown"


class ConnectivityStatus(str, Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class TrustStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    UNTRUSTED = "untrusted"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ResourcePressureStatus(str, Enum):
    NORMAL = "normal"
    OVERLOADED = "overloaded"
    THROTTLED = "throttled"
    UNKNOWN = "unknown"


class SyncStatus(str, Enum):
    SYNCED = "synced"
    OUTDATED = "outdated"
    DRIFTED = "drifted"
    UNKNOWN = "unknown"


class InstallationStatus(str, Enum):
    INSTALLED = "installed"
    ACTIVE = "active"
    BROKEN = "broken"
    PENDING_UPDATE = "pending_update"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class CanonicalActionDescriptor:
    id: str
    title: str
    requires_role: str | None = None
    risk: str | None = None
    affects: list[str] = field(default_factory=list)
    preconditions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CanonicalGovernance:
    tenant_id: str | None = None
    owner_id: str | None = None
    visibility: list[str] = field(default_factory=list)
    roles_allowed: list[str] = field(default_factory=list)
    shared_with: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CanonicalAudit:
    created_by: str | None = None
    updated_by: str | None = None
    last_seen: str | None = None
    last_changed: str | None = None


@dataclass(slots=True)
class CanonicalObject:
    id: str
    kind: str
    title: str
    summary: str | None = None
    status: CanonicalStatus = CanonicalStatus.UNKNOWN
    health: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, list[str]] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    versioning: dict[str, Any] = field(default_factory=dict)
    desired_state: dict[str, Any] = field(default_factory=dict)
    actual_state: dict[str, Any] = field(default_factory=dict)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    actions: list[CanonicalActionDescriptor] = field(default_factory=list)
    governance: CanonicalGovernance = field(default_factory=CanonicalGovernance)
    representations: dict[str, Any] = field(default_factory=dict)
    audit: CanonicalAudit = field(default_factory=CanonicalAudit)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class CanonicalProjection:
    id: str
    kind: str
    title: str
    subject: CanonicalObject
    summary: str | None = None
    objects: list[CanonicalObject] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    representations: dict[str, Any] = field(default_factory=dict)
    audit: CanonicalAudit = field(default_factory=CanonicalAudit)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


def _token(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "").strip().lower()


def normalize_operational_status(value: Any) -> CanonicalStatus:
    if value is True:
        return CanonicalStatus.ONLINE
    if value is False:
        return CanonicalStatus.OFFLINE

    token = _token(value)
    if token in {"online", "up", "ready", "healthy", "active", "ok", "running"}:
        return CanonicalStatus.ONLINE
    if token in {"offline", "down", "failed", "broken", "disconnected", "unreachable", "error"}:
        return CanonicalStatus.OFFLINE
    if token in {"degraded", "unstable", "limited", "partial"}:
        return CanonicalStatus.DEGRADED
    if token in {"warning", "warn", "pending", "pending_update", "draining", "throttled", "overloaded", "stale", "outdated", "drifted", "expired"}:
        return CanonicalStatus.WARNING
    return CanonicalStatus.UNKNOWN


def normalize_connectivity_status(value: Any) -> ConnectivityStatus:
    if value is True:
        return ConnectivityStatus.REACHABLE
    if value is False:
        return ConnectivityStatus.UNREACHABLE

    token = _token(value)
    if token in {"reachable", "connected", "online", "up", "ready", "ws", "hub", "open"}:
        return ConnectivityStatus.REACHABLE
    if token in {"unreachable", "disconnected", "offline", "down", "failed", "none", "closed", "closing"}:
        return ConnectivityStatus.UNREACHABLE
    return ConnectivityStatus.UNKNOWN


def normalize_trust_status(value: Any) -> TrustStatus:
    token = _token(value)
    if token in {"authenticated", "trusted", "verified", "valid"}:
        return TrustStatus.AUTHENTICATED
    if token in {"untrusted", "invalid", "rejected"}:
        return TrustStatus.UNTRUSTED
    if token in {"expired", "stale"}:
        return TrustStatus.EXPIRED
    return TrustStatus.UNKNOWN


def normalize_resource_pressure(value: Any) -> ResourcePressureStatus:
    token = _token(value)
    if token in {"normal", "ready", "healthy", "ok"}:
        return ResourcePressureStatus.NORMAL
    if token in {"overloaded", "hot", "high", "critical"}:
        return ResourcePressureStatus.OVERLOADED
    if token in {"throttled", "limited", "rate_limited"}:
        return ResourcePressureStatus.THROTTLED
    return ResourcePressureStatus.UNKNOWN


def normalize_sync_status(value: Any) -> SyncStatus:
    token = _token(value)
    if token in {"synced", "ready", "in_sync", "current", "consistent"}:
        return SyncStatus.SYNCED
    if token in {"outdated", "stale", "lagging", "out_of_date"}:
        return SyncStatus.OUTDATED
    if token in {"drift", "drifted", "version_mismatch", "mismatch"}:
        return SyncStatus.DRIFTED
    return SyncStatus.UNKNOWN


def normalize_installation_status(value: Any) -> InstallationStatus:
    token = _token(value)
    if token in {"active", "running", "enabled"}:
        return InstallationStatus.ACTIVE
    if token in {"installed", "present", "available"}:
        return InstallationStatus.INSTALLED
    if token in {"broken", "failed", "error", "crashed"}:
        return InstallationStatus.BROKEN
    if token in {"pending", "pending_update", "update_available"}:
        return InstallationStatus.PENDING_UPDATE
    return InstallationStatus.UNKNOWN


def compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, Mapping):
            nested = compact_mapping(item)
            if nested:
                out[str(key)] = nested
            continue
        if isinstance(item, (list, tuple, set)):
            items = [sub for sub in (_jsonify(sub_item) for sub_item in item) if sub is not None]
            if items:
                out[str(key)] = items
            continue
        out[str(key)] = _jsonify(item)
    return out


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        payload: dict[str, Any] = {}
        for item in fields(value):
            raw = getattr(value, item.name)
            if raw is None:
                continue
            if isinstance(raw, Mapping):
                nested = compact_mapping(raw)
                if nested:
                    payload[item.name] = nested
                continue
            if isinstance(raw, (list, tuple, set)):
                items = [sub for sub in (_jsonify(sub_item) for sub_item in raw) if sub is not None]
                if items:
                    payload[item.name] = items
                continue
            payload[item.name] = _jsonify(raw)
        return payload
    if isinstance(value, Mapping):
        return compact_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [item for item in (_jsonify(sub_item) for sub_item in value) if item is not None]
    return value


__all__ = [
    "CanonicalActionDescriptor",
    "CanonicalAudit",
    "CanonicalGovernance",
    "CanonicalObject",
    "CanonicalProjection",
    "CanonicalStatus",
    "ConnectivityStatus",
    "InstallationStatus",
    "ResourcePressureStatus",
    "SyncStatus",
    "TrustStatus",
    "compact_mapping",
    "normalize_connectivity_status",
    "normalize_installation_status",
    "normalize_operational_status",
    "normalize_resource_pressure",
    "normalize_sync_status",
    "normalize_trust_status",
]
