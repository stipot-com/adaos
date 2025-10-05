"""Enumerations describing roles, scopes and consent states for the root service."""
from __future__ import annotations

from enum import Enum

__all__ = [
    "DeviceRole",
    "Scope",
    "ConsentType",
    "ConsentStatus",
]


class _StrEnum(str, Enum):
    """Simple ``str``-backed enum compatible with Python 3.10."""

    def __str__(self) -> str:  # pragma: no cover - convenience for logging only
        return str(self.value)


class DeviceRole(_StrEnum):
    OWNER = "OWNER"
    HUB = "HUB"
    MEMBER = "MEMBER"
    BROWSER_IO = "BROWSER_IO"
    SERVICE = "SERVICE"


class Scope(_StrEnum):
    MANAGE_MEMBERS = "manage_members"
    MANAGE_IO_DEVICES = "manage_io_devices"
    EMIT_EVENT = "emit_event"
    SUBSCRIBE_EVENT = "subscribe_event"
    READ_LOGS = "read_logs"
    TRANSFER_OWNERSHIP = "transfer_ownership"


class ConsentType(_StrEnum):
    MEMBER = "member"
    DEVICE = "device"


class ConsentStatus(_StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
