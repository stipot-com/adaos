from __future__ import annotations

from typing import Optional


def bump_version(current: Optional[str], index: int) -> str:
    """
    Best-effort semantic-ish version bump.

    - Accepts any string, extracts digits from up to 3 dot-separated components.
    - `index`: 0=major, 1=minor, 2=patch (clamped).
    - Resets less-significant components to 0.
    """

    parts = [0, 0, 0]
    if current:
        raw_parts = str(current).split(".")
        for idx in range(min(len(raw_parts), 3)):
            token = raw_parts[idx]
            digits = "".join(ch for ch in token if ch.isdigit())
            if digits:
                try:
                    parts[idx] = int(digits)
                except ValueError:
                    parts[idx] = 0
    index = max(0, min(int(index), 2))
    parts[index] += 1
    for reset in range(index + 1, 3):
        parts[reset] = 0
    return ".".join(str(value) for value in parts)


__all__ = ["bump_version"]

