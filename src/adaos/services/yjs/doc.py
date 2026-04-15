from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from typing import Iterator, AsyncIterator, Awaitable, Optional, TypeVar, Callable, Any

import y_py as Y

from adaos.services.yjs.store import get_ystore_for_webspace

T = TypeVar("T")
_log = logging.getLogger("adaos.yjs.doc")


def _record_doc_timing(timings: dict[str, float] | None, key: str, started_at: float, *, prefix: str = "") -> float:
    value = round((time.perf_counter() - started_at) * 1000.0, 3)
    if timings is not None:
        token = f"{prefix}{str(key or '').strip()}" if prefix else str(key or "").strip()
        if token:
            timings[token] = value
    return value


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
        from adaos.services.yjs.gateway import y_server  # pylint: disable=import-outside-toplevel
    except Exception:
        return None
    return y_server.rooms.get(webspace_id)


def try_read_live_map_value(webspace_id: str, map_name: str, key: str) -> tuple[bool, Any]:
    """
    Best-effort fast path for reading a value from the in-memory live room.

    The helper only reads directly when the current thread already owns the
    room, so it stays non-blocking and safe for hot-path diagnostics.
    """
    room = _resolve_live_room(webspace_id)
    if not room:
        return False, None
    owner_thread = getattr(room, "_thread_id", None)
    current = threading.get_ident()
    if owner_thread is not None and owner_thread != current:
        return False, None
    try:
        y_map = room.ydoc.get_map(str(map_name or ""))
        return True, y_map.get(str(key or ""))
    except Exception:
        return True, None


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
def get_ydoc(
    webspace_id: str,
    *,
    read_only: bool = False,
    timings: dict[str, float] | None = None,
    timing_prefix: str = "",
) -> Iterator[Y.YDoc]:
    """
    Synchronously load a webspace-backed YDoc, applying persisted updates on
    entry and writing the resulting state back on exit.
    """
    _log.debug("get_ydoc enter webspace=%s", webspace_id)
    session_started = time.perf_counter()
    ystore = get_ystore_for_webspace(webspace_id)
    ydoc = Y.YDoc()

    async def _load() -> bytes | None:
        stage_started = time.perf_counter()
        await ystore.start()
        _record_doc_timing(timings, "ystore_start", stage_started, prefix=timing_prefix)
        try:
            stage_started = time.perf_counter()
            await ystore.apply_updates(ydoc)
            _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
        except BaseException:
            # Treat corrupted updates as "no state"; start from empty doc.
            _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
            pass
        if read_only:
            return None
        try:
            stage_started = time.perf_counter()
            before = Y.encode_state_vector(ydoc)
            _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
            return before
        except Exception:
            _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
            return None

    before = _run_blocking(_load())
    try:
        yield ydoc
    finally:
        async def _flush() -> bytes | None:
            update: bytes | None = None
            if not read_only:
                try:
                    stage_started = time.perf_counter()
                    await ystore.encode_state_as_update(ydoc)
                    _record_doc_timing(timings, "ystore_encode_state_as_update", stage_started, prefix=timing_prefix)
                except Exception:
                    _record_doc_timing(timings, "ystore_encode_state_as_update", stage_started, prefix=timing_prefix)
                    pass
                stage_started = time.perf_counter()
                update = _encode_diff(ydoc, before)
                _record_doc_timing(timings, "encode_diff", stage_started, prefix=timing_prefix)
                stage_started = time.perf_counter()
                _schedule_room_update(webspace_id, update)
                _record_doc_timing(timings, "room_update", stage_started, prefix=timing_prefix)
            return update

        try:
            _run_blocking(_flush())
        except Exception as exc:
            _log.warning("get_ydoc flush failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
        finally:
            try:
                stage_started = time.perf_counter()
                _run_blocking(ystore.stop())
                _record_doc_timing(timings, "ystore_stop", stage_started, prefix=timing_prefix)
            except Exception:
                _record_doc_timing(timings, "ystore_stop", stage_started, prefix=timing_prefix)
            _record_doc_timing(timings, "total", session_started, prefix=timing_prefix)


@asynccontextmanager
async def async_get_ydoc(
    webspace_id: str,
    *,
    read_only: bool = False,
    timings: dict[str, float] | None = None,
    timing_prefix: str = "",
) -> AsyncIterator[Y.YDoc]:
    """
    Async counterpart of :func:`get_ydoc` for use inside running event loops.
    """
    # Debug log omitted to reduce noise in dev logs.
    session_started = time.perf_counter()
    ystore = get_ystore_for_webspace(webspace_id)
    ydoc = Y.YDoc()
    stage_started = time.perf_counter()
    await ystore.start()
    _record_doc_timing(timings, "ystore_start", stage_started, prefix=timing_prefix)
    try:
        try:
            stage_started = time.perf_counter()
            await ystore.apply_updates(ydoc)
            _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
        except BaseException:
            # Treat corrupted updates as "no state"; start from empty doc.
            _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
            pass
        before = None
        if not read_only:
            try:
                stage_started = time.perf_counter()
                before = Y.encode_state_vector(ydoc)
                _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
            except Exception:
                _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
                before = None
        yield ydoc
        if not read_only:
            try:
                stage_started = time.perf_counter()
                await ystore.encode_state_as_update(ydoc)
                _record_doc_timing(timings, "ystore_encode_state_as_update", stage_started, prefix=timing_prefix)
            except Exception as exc:
                _record_doc_timing(timings, "ystore_encode_state_as_update", stage_started, prefix=timing_prefix)
                _log.warning("async_get_ydoc encode_state_as_update failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
            stage_started = time.perf_counter()
            update = _encode_diff(ydoc, before)
            _record_doc_timing(timings, "encode_diff", stage_started, prefix=timing_prefix)
            stage_started = time.perf_counter()
            _schedule_room_update(webspace_id, update)
            _record_doc_timing(timings, "room_update", stage_started, prefix=timing_prefix)
    finally:
        try:
            stage_started = time.perf_counter()
            await ystore.stop()
            _record_doc_timing(timings, "ystore_stop", stage_started, prefix=timing_prefix)
        except Exception:
            _record_doc_timing(timings, "ystore_stop", stage_started, prefix=timing_prefix)
        _record_doc_timing(timings, "total", session_started, prefix=timing_prefix)


@asynccontextmanager
async def async_read_ydoc(
    webspace_id: str,
    *,
    timings: dict[str, float] | None = None,
    timing_prefix: str = "",
) -> AsyncIterator[Y.YDoc]:
    async with async_get_ydoc(
        webspace_id,
        read_only=True,
        timings=timings,
        timing_prefix=timing_prefix,
    ) as ydoc:
        yield ydoc


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


def apply_update_to_live_room(webspace_id: str, update: bytes) -> bool:
    """
    Apply a raw Yjs update to the active in-process room (if any).
    Returns False if the webspace is not currently hosted in-process.
    """
    if not update:
        return False
    room = _resolve_live_room(webspace_id)
    if not room:
        return False

    def _apply() -> None:
        try:
            Y.apply_update(room.ydoc, update)
        except Exception:
            pass

    return _run_on_room_thread(room, _apply)


__all__ = [
    "get_ydoc",
    "async_get_ydoc",
    "async_read_ydoc",
    "try_read_live_map_value",
    "mutate_live_room",
    "apply_update_to_live_room",
]
