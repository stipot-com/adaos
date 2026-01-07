from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

_io_meta: ContextVar[dict[str, Any] | None] = ContextVar("adaos_io_meta", default=None)


def get_current_meta() -> dict[str, Any]:
    """
    Return a copy of the current IO `_meta` context.

    This is used to make skills "routeless": a tool can emit `io.out.*` events
    without explicitly passing `_meta`; the runtime attaches it automatically.
    """
    meta = _io_meta.get()
    if not isinstance(meta, dict):
        return {}
    return dict(meta)


@contextmanager
def io_meta(meta: Mapping[str, Any] | None) -> Iterator[None]:
    """
    Temporarily set IO `_meta` for the current execution context.
    """
    token = _io_meta.set(dict(meta) if isinstance(meta, Mapping) else None)
    try:
        yield
    finally:
        _io_meta.reset(token)

