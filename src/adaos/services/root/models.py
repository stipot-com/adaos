"""Dataclasses capturing the storage schema for the root authorization service."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .enums import ConsentStatus, ConsentType, DeviceRole, Scope
from .ids import DeviceId, EventId, NodeId, SubnetId, TraceId

__all__ = [
    "SubnetRecord",
    "NodeRecord",
    "DeviceRecord",
    "ConsentRequest",
    "AuditEvent",
]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(slots=True)
class SubnetRecord:
    id: SubnetId
    owner_id: DeviceId
    created_at: datetime = field(default_factory=_utcnow)
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class NodeRecord:
    id: NodeId
    role: DeviceRole
    subnet_id: SubnetId
    pub_fingerprint: str
    status: str = "inactive"
    last_seen_at: datetime | None = None


@dataclass(slots=True)
class DeviceRecord:
    id: DeviceId
    role: DeviceRole
    subnet_id: SubnetId
    node_id: NodeId | None = None
    aliases: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    scopes: list[Scope] = field(default_factory=list)
    jwk_thumbprint: str | None = None
    public_key_pem: str | None = None
    revoked: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def grant_scopes(self, scopes: Iterable[Scope]) -> None:
        unique = set(self.scopes)
        for scope in scopes:
            unique.add(scope)
        self.scopes = sorted(unique, key=lambda s: s.value)
        self.updated_at = _utcnow()

    def revoke_scope(self, scope: Scope) -> None:
        if scope in self.scopes:
            self.scopes.remove(scope)
            self.updated_at = _utcnow()

    def add_alias(self, alias: str) -> None:
        if alias not in self.aliases:
            self.aliases.append(alias)
            self.updated_at = _utcnow()

    def rotate_key(self, *, jwk_thumbprint: str, public_key_pem: str | None = None) -> None:
        self.jwk_thumbprint = jwk_thumbprint
        self.public_key_pem = public_key_pem
        self.updated_at = _utcnow()


@dataclass(slots=True)
class ConsentRequest:
    id: str
    consent_type: ConsentType
    requester_id: DeviceId
    subnet_id: SubnetId
    scopes_requested: list[Scope]
    status: ConsentStatus = ConsentStatus.PENDING
    ttl: timedelta = field(default_factory=lambda: timedelta(minutes=15))
    created_at: datetime = field(default_factory=_utcnow)

    def is_expired(self, at: datetime | None = None) -> bool:
        if at is None:
            at = _utcnow()
        return at > self.expires_at

    @property
    def expires_at(self) -> datetime:
        return self.created_at + self.ttl


@dataclass(slots=True)
class AuditEvent:
    id: EventId
    trace_id: TraceId
    subnet_id: SubnetId
    actor_id: DeviceId | None
    subject_id: DeviceId | NodeId | SubnetId | None
    action: str
    acl: Sequence[Scope]
    ttl: timedelta
    payload: dict[str, object]
    timestamp: datetime = field(default_factory=_utcnow)

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.id,
            "trace_id": self.trace_id,
            "subnet_id": self.subnet_id,
            "actor_id": self.actor_id,
            "subject_id": self.subject_id,
            "action": self.action,
            "acl": [scope.value for scope in self.acl],
            "ttl": int(self.ttl.total_seconds()),
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }
