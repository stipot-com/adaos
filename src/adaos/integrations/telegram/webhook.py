from __future__ import annotations

from typing import Mapping, Any


def validate_secret(header_value: str | None, expected: str | None) -> bool:
    if expected is None:
        return True
    return bool(header_value) and header_value == expected

