from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from adaos.sdk.data.context import get_current_skill
from adaos.services.yjs.doc import async_get_ydoc, async_read_ydoc, get_ydoc
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.yjs.webspace import default_webspace_id


def _sdk_yjs_owner() -> str:
    current = get_current_skill()
    name = str(getattr(current, "name", "") or "").strip()
    if name:
        return f"skill:{name}"
    return "sdk:unknown"


@asynccontextmanager
async def webspace_ydoc(
    webspace_id: str | None = None,
    *,
    read_only: bool = False,
    load_mark_roots: list[str] | tuple[str, ...] | None = None,
) -> AsyncIterator[Any]:
    """
    SDK-facing YJS context for skill code.

    Skills should prefer this wrapper over direct service imports so YJS write
    traffic carries explicit SDK ownership metadata into load-mark telemetry.
    """
    target = str(webspace_id or "").strip() or default_webspace_id()
    async with ystore_write_metadata(
        owner=_sdk_yjs_owner(),
        channel="sdk.web.yjs",
    ):
        async with async_get_ydoc(
            target,
            read_only=read_only,
            load_mark_roots=load_mark_roots,
        ) as ydoc:
            yield ydoc


@asynccontextmanager
async def webspace_read_ydoc(
    webspace_id: str | None = None,
) -> AsyncIterator[Any]:
    target = str(webspace_id or "").strip() or default_webspace_id()
    async with async_read_ydoc(target) as ydoc:
        yield ydoc


@contextmanager
def webspace_ydoc_sync(
    webspace_id: str | None = None,
    *,
    read_only: bool = False,
    load_mark_roots: list[str] | tuple[str, ...] | None = None,
) -> Iterator[Any]:
    target = str(webspace_id or "").strip() or default_webspace_id()
    with get_ydoc(
        target,
        read_only=read_only,
        load_mark_roots=load_mark_roots,
    ) as ydoc:
        yield ydoc


__all__ = [
    "webspace_ydoc",
    "webspace_read_ydoc",
    "webspace_ydoc_sync",
]
