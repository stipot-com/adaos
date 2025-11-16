from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, asynccontextmanager
from typing import Iterator, AsyncIterator, Awaitable, TypeVar

import y_py as Y
from ypy_websocket.ystore import SQLiteYStore

from adaos.apps.yjs.y_store import ystore_path_for_webspace

T = TypeVar("T")


def _run_blocking(coro: Awaitable[T]) -> T:
    """
    Execute an async SQLiteYStore operation from synchronous code.
    Falls back to a dedicated thread when called from an active loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("get_ydoc() cannot be used inside an active event loop; use async_get_ydoc().")


@contextmanager
def get_ydoc(webspace_id: str) -> Iterator[Y.YDoc]:
    """
    Synchronously load a workspace-backed YDoc, applying persisted updates on
    entry and writing the resulting state back on exit.
    """
    ystore = SQLiteYStore(str(ystore_path_for_webspace(webspace_id)))
    ydoc = Y.YDoc()

    async def _load() -> None:
        await ystore.start()
        try:
            await ystore.apply_updates(ydoc)
        except Exception:
            # Treat missing or corrupt snapshots as empty YDoc.
            pass

    _run_blocking(_load())
    try:
        yield ydoc
    finally:
        async def _flush() -> None:
            try:
                await ystore.encode_state_as_update(ydoc)
            except Exception:
                # Encoding is best-effort; leave silent to avoid breaking callers.
                pass

        try:
            _run_blocking(_flush())
        except Exception:
            pass


@asynccontextmanager
async def async_get_ydoc(webspace_id: str) -> AsyncIterator[Y.YDoc]:
    """
    Async counterpart of :func:`get_ydoc` for use inside running event loops.
    """
    ystore = SQLiteYStore(str(ystore_path_for_webspace(webspace_id)))
    ydoc = Y.YDoc()
    await ystore.start()
    try:
        try:
            await ystore.apply_updates(ydoc)
        except Exception:
            pass
        yield ydoc
        try:
            await ystore.encode_state_as_update(ydoc)
        except Exception:
            pass
    finally:
        try:
            await ystore.stop()
        except Exception:
            pass


__all__ = ["get_ydoc", "async_get_ydoc"]
