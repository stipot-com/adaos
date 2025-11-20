"""SDK facade for interacting with the secrets service."""

from __future__ import annotations

from typing import Optional

from adaos.sdk.core._cap import require_cap


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return a secret by name or the provided default when missing."""

    ctx = require_cap("secrets.read")
    return ctx.secrets.get(name, default=default)


def set(name: str, value: str) -> None:
    """Store or update a secret value for the active skill."""

    ctx = require_cap("secrets.write")
    ctx.secrets.put(name, value)


def delete(name: str) -> None:
    """Remove a stored secret value for the active skill."""

    ctx = require_cap("secrets.write")
    ctx.secrets.delete(name)


# Backwards-compatible aliases for older skills.
read = get
write = set


__all__ = ["get", "set", "delete", "read", "write"]
