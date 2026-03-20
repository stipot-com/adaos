"""Compatibility wrapper for skill-local memory backed by the skill env store."""

from __future__ import annotations

from typing import Any

from .skill_env import get_env, set_env

__all__ = ["get", "set"]


def get(key: str, default: Any | None = None) -> Any:
    return get_env(key, default)


def set(key: str, value: Any) -> None:
    set_env(key, value)
