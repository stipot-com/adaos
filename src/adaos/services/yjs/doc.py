from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Iterator, Awaitable, TypeVar

import y_py as Y
from ypy_websocket.ystore import SQLiteYStore

from adaos.apps.yjs.y_store import ystore_path_for_workspace

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

    result: dict[str, T] = {}
    error: list[BaseException] = []

    def _target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - best effort guard
            error.append(exc)

    thread = threading.Thread(target=_target, name="adaos-ystore-op", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")  # type: ignore[return-value]


@contextmanager
def get_ydoc(workspace_id: str) -> Iterator[Y.YDoc]:
    """
    Synchronously load a workspace-backed YDoc, applying persisted updates on
    entry and writing the resulting state back on exit.
    """
    ystore = SQLiteYStore(str(ystore_path_for_workspace(workspace_id)))
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


__all__ = ["get_ydoc"]
