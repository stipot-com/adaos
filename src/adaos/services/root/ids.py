"""Helper utilities for generating strongly-typed identifiers used by the
AdaOS root authorization service.

The MVP specification requires orderable identifiers (ULID/UUIDv7).  Python 3.10
does not provide native helpers for UUID version 7, so we implement a minimal
UUIDv7 generator according to draft-ietf-uuidrev-rfc4122bis.  The generator keeps
identifiers monotonic within millisecond resolution and therefore fits the
"orderable" requirement while remaining dependency-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import time
import uuid
from typing import NewType

__all__ = [
    "SubnetId",
    "NodeId",
    "DeviceId",
    "EventId",
    "TraceId",
    "generate_subnet_id",
    "generate_node_id",
    "generate_device_id",
    "generate_event_id",
    "generate_trace_id",
    "uuid7",
]

SubnetId = NewType("SubnetId", str)
NodeId = NewType("NodeId", str)
DeviceId = NewType("DeviceId", str)
EventId = NewType("EventId", str)
TraceId = NewType("TraceId", str)


_UUID7_MASK_48 = (1 << 48) - 1
_UUID7_VERSION_BITS = 0x7
_UUID7_VARIANT_BITS = 0b10


def uuid7(ts: float | None = None) -> uuid.UUID:
    """Return a UUID version 7 value.

    Args:
        ts: Optional timestamp (seconds). When omitted the current time is used.
            Supplying the timestamp is primarily intended for testing.

    The implementation follows the bit layout defined in
    draft-ietf-uuidrev-rfc4122bis section 5.2.  Millisecond precision is used for
    the 48-bit timestamp component and the remaining bits are filled with
    cryptographically secure random data.
    """

    if ts is None:
        ts = time.time()

    unix_ts_ms = int(ts * 1000)
    if unix_ts_ms < 0 or unix_ts_ms > _UUID7_MASK_48:
        raise ValueError("timestamp out of range for UUIDv7")

    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    value = (unix_ts_ms & _UUID7_MASK_48) << 80
    value |= _UUID7_VERSION_BITS << 76
    value |= rand_a << 64
    value |= _UUID7_VARIANT_BITS << 62
    value |= rand_b

    return uuid.UUID(int=value)


def _format_uuid(u: uuid.UUID) -> str:
    return str(u)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def generate_subnet_id() -> SubnetId:
    return SubnetId(_format_uuid(uuid7()))


def generate_node_id() -> NodeId:
    return NodeId(_format_uuid(uuid7()))


def generate_device_id() -> DeviceId:
    return DeviceId(_format_uuid(uuid7()))


def generate_event_id() -> EventId:
    return EventId(_format_uuid(uuid7()))


def generate_trace_id() -> TraceId:
    return TraceId(_format_uuid(uuid7()))


@dataclass(frozen=True, slots=True)
class TimestampedId:
    """Convenience container tying an identifier to its creation instant."""

    value: str
    created_at: str

    @classmethod
    def with_uuid7(cls) -> "TimestampedId":
        return cls(value=str(uuid7()), created_at=_now_iso())
