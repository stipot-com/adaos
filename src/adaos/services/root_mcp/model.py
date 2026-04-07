from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping


class RootMcpSurface(str, Enum):
    DEVELOPMENT = "development"
    OPERATIONS = "operations"


class RootMcpAvailability(str, Enum):
    ENABLED = "enabled"
    PLACEHOLDER = "placeholder"


@dataclass(slots=True)
class RootMcpToolContract:
    id: str
    title: str
    surface: RootMcpSurface
    summary: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    required_capability: str | None = None
    stability: str = "experimental"
    side_effects: str = "none"
    availability: RootMcpAvailability = RootMcpAvailability.ENABLED
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class RootMcpManagedTarget:
    target_id: str
    title: str
    kind: str
    environment: str
    status: str = "unknown"
    zone: str | None = None
    subnet_id: str | None = None
    transport: dict[str, Any] = field(default_factory=dict)
    operational_surface: dict[str, Any] = field(default_factory=dict)
    access: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class RootMcpError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class RootMcpResponseEnvelope:
    request_id: str
    trace_id: str
    tool_id: str
    surface: RootMcpSurface
    ok: bool
    status: str
    dry_run: bool = False
    result: Any | None = None
    error: RootMcpError | None = None
    audit_event_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class RootMcpAuditEvent:
    event_id: str
    request_id: str
    trace_id: str
    tool_id: str
    surface: RootMcpSurface
    actor: str
    auth_method: str
    capability: str | None = None
    target_id: str | None = None
    policy_decision: str = "allow"
    execution_adapter: str | None = None
    dry_run: bool = False
    status: str = "ok"
    started_at: str | None = None
    finished_at: str | None = None
    result_summary: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    redactions: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonify(self)
        return payload if isinstance(payload, dict) else {}


ROOT_MCP_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "details": {"type": "object"},
    },
    "required": ["code", "message"],
    "additionalProperties": True,
}


ROOT_MCP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string"},
        "trace_id": {"type": "string"},
        "tool_id": {"type": "string"},
        "surface": {"type": "string", "enum": [item.value for item in RootMcpSurface]},
        "ok": {"type": "boolean"},
        "status": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "result": {"type": ["object", "array", "string", "number", "boolean", "null"]},
        "error": ROOT_MCP_ERROR_SCHEMA,
        "audit_event_id": {"type": "string"},
        "meta": {"type": "object"},
    },
    "required": ["request_id", "trace_id", "tool_id", "surface", "ok", "status", "dry_run"],
    "additionalProperties": True,
}


def schema_object(
    *,
    properties: Mapping[str, Any] | None = None,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required or []),
        "additionalProperties": bool(additional_properties),
    }


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, Mapping):
            nested = _compact_mapping(item)
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
                nested = _compact_mapping(raw)
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
        return _compact_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [item for item in (_jsonify(sub_item) for sub_item in value) if item is not None]
    return value


__all__ = [
    "ROOT_MCP_ERROR_SCHEMA",
    "ROOT_MCP_RESPONSE_SCHEMA",
    "RootMcpAuditEvent",
    "RootMcpAvailability",
    "RootMcpError",
    "RootMcpManagedTarget",
    "RootMcpResponseEnvelope",
    "RootMcpSurface",
    "RootMcpToolContract",
    "schema_object",
]
