"""Foundational domain primitives for the AdaOS root authorization service."""
from .ids import (
    DeviceId,
    EventId,
    NodeId,
    SubnetId,
    TimestampedId,
    TraceId,
    generate_device_id,
    generate_event_id,
    generate_node_id,
    generate_subnet_id,
    generate_trace_id,
    uuid7,
)
from .enums import ConsentStatus, ConsentType, DeviceRole, Scope
from .models import AuditEvent, ConsentRequest, DeviceRecord, NodeRecord, SubnetRecord
from .root_backend import ClientContext, RootAuthorityBackend, RootBackendError

__all__ = [
    "DeviceId",
    "EventId",
    "NodeId",
    "SubnetId",
    "TimestampedId",
    "TraceId",
    "generate_device_id",
    "generate_event_id",
    "generate_node_id",
    "generate_subnet_id",
    "generate_trace_id",
    "uuid7",
    "ConsentStatus",
    "ConsentType",
    "DeviceRole",
    "Scope",
    "AuditEvent",
    "ConsentRequest",
    "DeviceRecord",
    "NodeRecord",
    "SubnetRecord",
    "ClientContext",
    "RootAuthorityBackend",
    "RootBackendError",
]
