from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Iterator, TypeGuard


def is_mapping_like(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return True
    items = getattr(value, "items", None)
    return callable(items)

def is_iterable_like(value: Any) -> TypeGuard[Iterable[Any]]:
    if value is None:
        return False
    if isinstance(value, (str, bytes, bytearray)):
        return False
    if is_mapping_like(value):
        return False
    return isinstance(value, Iterable)


def coerce_dict(value: Any) -> dict[str, Any]:
    """
    Best-effort conversion to a plain dict.

    YJS map values (y_py) are not guaranteed to implement `collections.abc.Mapping`,
    but they typically expose `.items()`. Treating them as non-mapping silently
    drops state during scenario switches and NLU Teacher persistence.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return {str(k): v for k, v in items()}
        except Exception:
            return {}
    return {}


def iter_mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    """
    Yield mapping items from `value`.

    - If `value` is a mapping-like object -> yield it.
    - If `value` is an iterable of mapping-like objects -> yield each.
    """
    if value is None:
        return
    if is_mapping_like(value):
        coerced = coerce_dict(value)
        if coerced:
            yield coerced
        return
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Iterable):
        return
    for item in value:
        if is_mapping_like(item):
            coerced = coerce_dict(item)
            if coerced:
                yield coerced


def iter_scalars(value: Any, *, scalar_types: tuple[type, ...] = (str, int)) -> Iterator[Any]:
    """
    Yield scalar items from an iterable (e.g. YArray).

    Intended for lists of ids like `installed.apps` / `installed.widgets`.
    """
    if not is_iterable_like(value):
        return
    for item in value:
        if isinstance(item, scalar_types):
            yield item
