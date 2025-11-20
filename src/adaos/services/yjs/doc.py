from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager, asynccontextmanager
from typing import Iterator, AsyncIterator, Awaitable, Optional, TypeVar, Callable, Any

import y_py as Y

from adaos.apps.yjs.y_store import get_ystore_for_webspace

T = TypeVar("T")
_log = logging.getLogger("adaos.yjs.doc")


def _run_blocking(coro: Awaitable[T]) -> T:
    """
    Execute an async SQLiteYStore operation from synchronous code.
    Falls back to asyncio.run when no loop is active.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("get_ydoc() cannot be used inside an active event loop; use async_get_ydoc().")


def _resolve_live_room(webspace_id: str):
    """
    Try to resolve an active YRoom for the given webspace id, if the Y websocket
    server is running in-process. Import is lazy to avoid circular deps.
    """
    try:
        from adaos.apps.yjs.y_gateway import y_server  # pylint: disable=import-outside-toplevel
    except Exception:
        return None
    return y_server.rooms.get(webspace_id)


def _schedule_room_update(webspace_id: str, update: Optional[bytes]) -> None:
    """
    Apply the given Yjs update to the active room (if any) so connected clients
    receive the change immediately. Falls back silently if no room is active.
    """
    if not update:
        return
    room = _resolve_live_room(webspace_id)
    if not room:
        return

    def _apply() -> None:
        try:
            Y.apply_update(room.ydoc, update)
        except Exception:
            pass

    _run_on_room_thread(room, _apply)


def _run_on_room_thread(room, fn: Callable[[], None]) -> bool:
    owner_thread = getattr(room, "_thread_id", None)
    loop = getattr(room, "_loop", None)
    current = threading.get_ident()

    if owner_thread is not None and owner_thread == current:
        fn()
        return True

    if loop and loop.is_running():
        try:
            loop.call_soon_threadsafe(fn)
            return True
        except RuntimeError:
            return False

    if owner_thread is None:
        fn()
        return True

    return False


def _encode_diff(ydoc: Y.YDoc, before: bytes | None) -> bytes | None:
    try:
        if before is not None:
            return Y.encode_state_as_update(ydoc, before)
        return Y.encode_state_as_update(ydoc)
    except Exception:
        return None


@contextmanager
def get_ydoc(webspace_id: str) -> Iterator[Y.YDoc]:
    """
    Synchronously load a webspace-backed YDoc, applying persisted updates on
    entry and writing the resulting state back on exit.
    """
    _log.debug("get_ydoc enter webspace=%s", webspace_id)
    ystore = get_ystore_for_webspace(webspace_id)
    ydoc = Y.YDoc()

    async def _load() -> bytes | None:
        await ystore.start()
        try:
            await ystore.apply_updates(ydoc)
        except Exception:
            pass
        try:
            return Y.encode_state_vector(ydoc)
        except Exception:
            return None

    before = _run_blocking(_load())
    try:
        yield ydoc
    finally:
        async def _flush() -> bytes | None:
            try:
                await ystore.encode_state_as_update(ydoc)
            except Exception:
                pass
            finally:
                try:
                    await ystore.stop()
                except Exception:
                    pass
            return _encode_diff(ydoc, before)

        try:
            update = _run_blocking(_flush())
        except Exception as exc:
            _log.warning("get_ydoc flush failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
            update = None
        _schedule_room_update(webspace_id, update)


@asynccontextmanager
async def async_get_ydoc(webspace_id: str) -> AsyncIterator[Y.YDoc]:
    """
    Async counterpart of :func:`get_ydoc` for use inside running event loops.
    """
    _log.debug("async_get_ydoc enter webspace=%s", webspace_id)
    ystore = get_ystore_for_webspace(webspace_id)
    ydoc = Y.YDoc()
    await ystore.start()
    try:
        try:
            await ystore.apply_updates(ydoc)
        except Exception:
            pass
        try:
            before = Y.encode_state_vector(ydoc)
        except Exception:
            before = None
        yield ydoc
        try:
            await ystore.encode_state_as_update(ydoc)
        except Exception as exc:
            _log.warning("async_get_ydoc encode_state_as_update failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
        update = _encode_diff(ydoc, before)
        _schedule_room_update(webspace_id, update)
    finally:
        try:
            await ystore.stop()
        except Exception:
            pass


def mutate_live_room(webspace_id: str, mutator: Callable[[Y.YDoc, Any], None]) -> bool:
    """
    Attempt to mutate the active YDoc directly so connected clients receive the change.
    Returns False if the webspace is not currently hosted in-process.
    """
    room = _resolve_live_room(webspace_id)
    if not room:
        return False

    def _apply() -> None:
        try:
            with room.ydoc.begin_transaction() as txn:
                mutator(room.ydoc, txn)
        except Exception:
            pass

    return _run_on_room_thread(room, _apply)


__all__ = ["get_ydoc", "async_get_ydoc", "mutate_live_room"]
