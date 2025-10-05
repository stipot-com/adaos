"""Pydantic-free schema helpers for the persistent root backend."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .enums import Scope


def _isoformat(dt: datetime) -> str:
    moment = dt.astimezone(timezone.utc)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class ResponseEnvelope:
    """Standard API envelope returned by the v1 root backend."""

    payload: Mapping[str, Any]
    event_id: str
    server_time_utc: datetime

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.payload)
        data.setdefault("server_time_utc", _isoformat(self.server_time_utc))
        data.setdefault("event_id", self.event_id)
        return data


@dataclass(slots=True)
class ErrorEnvelope:
    code: str
    message: str
    hint: str | None = None
    retry_after: int | None = None
    event_id: str | None = None
    server_time_utc: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.hint is not None:
            data["hint"] = self.hint
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        if self.event_id is not None:
            data["event_id"] = self.event_id
        if self.server_time_utc is not None:
            data["server_time_utc"] = _isoformat(self.server_time_utc)
        return data


@dataclass(slots=True)
class TokenResponse:
    device_id: str
    subnet_id: str
    scopes: Sequence[Scope]
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    channel_token: str
    channel_expires_at: datetime

    def as_payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "subnet_id": self.subnet_id,
            "scopes": [scope.value for scope in self.scopes],
            "access_token": self.access_token,
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_token": self.refresh_token,
            "refresh_expires_at": self.refresh_expires_at.isoformat(),
            "channel_token": self.channel_token,
            "channel_expires_at": self.channel_expires_at.isoformat(),
        }


@dataclass(slots=True)
class AuditExport:
    records: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def as_ndjson(self) -> str:
        return "\n".join(json.dumps(record, sort_keys=True) for record in self.records)

    def verify(self, secret: bytes) -> bool:
        for record in self.records:
            payload = dict(record)
            signature = payload.pop("signature", None)
            if signature is None:
                return False
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            digest = hmac.new(secret, serialized, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(digest, signature):
                return False
        return True

