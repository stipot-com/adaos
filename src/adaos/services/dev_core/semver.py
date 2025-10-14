"""Simple semantic version helpers."""

from __future__ import annotations

from typing import Tuple

from .types import Bump


def _parse(version: str | None) -> Tuple[int, int, int]:
    if not version:
        return (0, 1, 0)
    parts = version.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return (0, 1, 0)
    return (max(0, major), max(0, minor), max(0, patch))


def bump(version: str | None, kind: Bump) -> str:
    major, minor, patch = _parse(version)
    if kind == "major":
        major += 1
        minor = 0
        patch = 0
    elif kind == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"
