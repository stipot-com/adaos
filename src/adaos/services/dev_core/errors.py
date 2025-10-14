"""Developer workflow error classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .types import Issue


class DevError(RuntimeError):
    """Base error for developer workflows."""


class EDeprecated(DevError):
    """Raised when a legacy entrypoint is used."""


class ETemplateNotFound(DevError):
    """Raised when a requested template cannot be located."""

    def __init__(self, template: str, *, candidates: Optional[List[str]] = None) -> None:
        self.template = template
        self.candidates = candidates or []
        message = f"Template '{template}' not found"
        if self.candidates:
            message += ": available: " + ", ".join(self.candidates[:20])
        super().__init__(message)


class EDevNotFound(DevError):
    """Raised when a requested dev artifact does not exist."""


@dataclass(slots=True)
class EValidationFailed(DevError):
    """Raised when validation errors should abort an operation."""

    issues: List[Issue]

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        if not self.issues:
            return "validation failed"
        first = self.issues[0]
        location = f" at {first.path}" if first.path else ""
        return f"validation failed{location}: {first.message}"


class EVersionConflict(DevError):
    """Raised when version bump/publish conflicts occur."""


class EAuthRequired(DevError):
    """Raised when root authentication is required."""


class ENetwork(DevError):
    """Raised for network/transport issues."""
