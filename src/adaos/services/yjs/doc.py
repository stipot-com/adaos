from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager, asynccontextmanager
from typing import Iterator, AsyncIterator, Awaitable, Optional, TypeVar, Callable, Any

import y_py as Y

from adaos.services.yjs.store import get_ystore_for_webspace, ystore_write_metadata

T = TypeVar("T")
_log = logging.getLogger("adaos.yjs.doc")


def _record_doc_timing(timings: dict[str, float] | None, key: str, started_at: float, *, prefix: str = "") -> float:
    value = round((time.perf_counter() - started_at) * 1000.0, 3)
    if timings is not None:
        token = f"{prefix}{str(key or '').strip()}" if prefix else str(key or "").strip()
        if token:
            timings[token] = value
    return value


def _set_doc_timing(timings: dict[str, float] | None, key: str, value: float, *, prefix: str = "") -> float:
    if timings is not None:
        token = f"{prefix}{str(key or '').strip()}" if prefix else str(key or "").strip()
        if token:
            timings[token] = round(float(value), 3)
    return round(float(value), 3)


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


def _live_room_pipeline_ready(room: Any) -> bool:
    """
    Return True when backend mutations on this room will be broadcast and persisted.

    Live-room fast paths are only safe while the room task group is running.
    Otherwise we could mutate an in-memory doc and accidentally skip the normal
    YStore writeback path.
    """
    if room is None or getattr(room, "ydoc", None) is None:
        return False
    if getattr(room, "_task_group", None) is None:
        return False
    if getattr(room, "ystore", None) is None:
        return False
    started = getattr(room, "started", None)
    if started is None:
        return True
    is_set = getattr(started, "is_set", None)
    if not callable(is_set):
        return True
    try:
        return bool(is_set())
    except Exception:
        return False


def _can_access_live_room_directly(room: Any) -> bool:
    """
    Return True when the caller already runs on the room owner thread/loop.

    Direct room reuse is intentionally conservative: we only touch the live
    YDoc in-place when we know we are already executing in the same runtime
    context that owns the room. Other callers fall back to the isolated
    store-backed YDoc session.
    """
    if not _live_room_pipeline_ready(room):
        return False
    owner_thread = getattr(room, "_thread_id", None)
    current_thread = threading.get_ident()
    if owner_thread is not None and owner_thread != current_thread:
        return False
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    room_loop = getattr(room, "_loop", None)
    if room_loop is not None and current_loop is not None and room_loop is not current_loop:
        return False
    return True


def try_read_live_map_value(webspace_id: str, map_name: str, key: str) -> tuple[bool, Any]:
    """
    Best-effort fast path for reading a value from the in-memory live room.

    The helper only reads directly when the current thread already owns the
    room, so it stays non-blocking and safe for hot-path diagnostics.
    """
    room = _resolve_live_room(webspace_id)
    if not _can_access_live_room_directly(room):
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


def _state_changed(
    ydoc: Y.YDoc,
    before: bytes | None,
    timings: dict[str, float] | None,
    *,
    prefix: str = "",
) -> bool:
    if before is None:
        return True
    stage_started = time.perf_counter()
    try:
        after = Y.encode_state_vector(ydoc)
        _record_doc_timing(timings, "encode_state_vector_after", stage_started, prefix=prefix)
        return after != before
    except Exception:
        _record_doc_timing(timings, "encode_state_vector_after", stage_started, prefix=prefix)
        return True


@contextmanager
def get_ydoc(
    webspace_id: str,
    *,
    read_only: bool = False,
    timings: dict[str, float] | None = None,
    timing_prefix: str = "",
    load_mark_roots: list[str] | tuple[str, ...] | None = None,
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
        stage_started = time.perf_counter()
        before = await ystore.current_state_vector()
        if before is not None:
            _set_doc_timing(timings, "encode_state_vector", 0.0, prefix=timing_prefix)
            return before
        try:
            before = Y.encode_state_vector(ydoc)
            _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
            return before
        except Exception:
            _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
            return None

    before = _run_blocking(_load())
    tracked_load_mark_roots = [str(name or "").strip() for name in (load_mark_roots or ()) if str(name or "").strip()]
    try:
        yield ydoc
    finally:
        async def _flush() -> bytes | None:
            update: bytes | None = None
            if not read_only:
                if _state_changed(ydoc, before, timings, prefix=timing_prefix):
                    stage_started = time.perf_counter()
                    update = _encode_diff(ydoc, before)
                    _record_doc_timing(timings, "encode_diff", stage_started, prefix=timing_prefix)
                    try:
                        stage_started = time.perf_counter()
                        async with ystore_write_metadata(root_names=tracked_load_mark_roots, source="get_ydoc"):
                            if update:
                                await ystore.write_update(update, update_kind="diff")
                            else:
                                await ystore.write_update(b"", update_kind="diff")
                        _record_doc_timing(timings, "ystore_write_update", stage_started, prefix=timing_prefix)
                    except Exception:
                        _record_doc_timing(timings, "ystore_write_update", stage_started, prefix=timing_prefix)
                        pass
                    stage_started = time.perf_counter()
                    _schedule_room_update(webspace_id, update)
                    _record_doc_timing(timings, "room_update", stage_started, prefix=timing_prefix)
                else:
                    _set_doc_timing(timings, "encode_diff", 0.0, prefix=timing_prefix)
                    _set_doc_timing(timings, "ystore_write_update", 0.0, prefix=timing_prefix)
                    _set_doc_timing(timings, "room_update", 0.0, prefix=timing_prefix)
            return update

        try:
            _run_blocking(_flush())
        except Exception as exc:
            _log.warning("get_ydoc flush failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
        finally:
            stage_started = time.perf_counter()
            try:
                ystore.stop()
            except Exception:
                pass
            _record_doc_timing(timings, "ystore_stop", stage_started, prefix=timing_prefix)
            _record_doc_timing(timings, "total", session_started, prefix=timing_prefix)


@asynccontextmanager
async def async_get_ydoc(
    webspace_id: str,
    *,
    read_only: bool = False,
    prefer_live_room: bool = False,
    timings: dict[str, float] | None = None,
    timing_prefix: str = "",
    load_mark_roots: list[str] | tuple[str, ...] | None = None,
) -> AsyncIterator[Y.YDoc]:
    """
    Async counterpart of :func:`get_ydoc` for use inside running event loops.
    """
    # Debug log omitted to reduce noise in dev logs.
    session_started = time.perf_counter()
    ystore = get_ystore_for_webspace(webspace_id)
    room = _resolve_live_room(webspace_id) if prefer_live_room else None
    use_live_room = _can_access_live_room_directly(room)
    ydoc = room.ydoc if use_live_room else Y.YDoc()
    if use_live_room:
        _set_doc_timing(timings, "ystore_start", 0.0, prefix=timing_prefix)
        _set_doc_timing(timings, "ystore_apply_updates", 0.0, prefix=timing_prefix)
    else:
        stage_started = time.perf_counter()
        await ystore.start()
        _record_doc_timing(timings, "ystore_start", stage_started, prefix=timing_prefix)
    try:
        if not use_live_room:
            try:
                stage_started = time.perf_counter()
                await ystore.apply_updates(ydoc)
                _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
            except BaseException:
                # Treat corrupted updates as "no state"; start from empty doc.
                _record_doc_timing(timings, "ystore_apply_updates", stage_started, prefix=timing_prefix)
                pass
        before = None
        tracked_load_mark_roots = [str(name or "").strip() for name in (load_mark_roots or ()) if str(name or "").strip()]
        if not read_only:
            stage_started = time.perf_counter()
            before = await ystore.current_state_vector()
            if before is not None:
                _set_doc_timing(timings, "encode_state_vector", 0.0, prefix=timing_prefix)
            else:
                try:
                    before = Y.encode_state_vector(ydoc)
                    _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
                except Exception:
                    _record_doc_timing(timings, "encode_state_vector", stage_started, prefix=timing_prefix)
                    before = None
        yield ydoc
        if not read_only:
            if _state_changed(ydoc, before, timings, prefix=timing_prefix):
                if use_live_room:
                    # Active YRoom instances already fan backend mutations into
                    # websocket broadcast and YStore persistence.
                    _set_doc_timing(timings, "encode_diff", 0.0, prefix=timing_prefix)
                    _set_doc_timing(timings, "ystore_write_update", 0.0, prefix=timing_prefix)
                    _set_doc_timing(timings, "room_update", 0.0, prefix=timing_prefix)
                else:
                    stage_started = time.perf_counter()
                    update = _encode_diff(ydoc, before)
                    _record_doc_timing(timings, "encode_diff", stage_started, prefix=timing_prefix)
                    try:
                        stage_started = time.perf_counter()
                        async with ystore_write_metadata(root_names=tracked_load_mark_roots, source="async_get_ydoc"):
                            if update:
                                await ystore.write_update(update, update_kind="diff")
                            else:
                                await ystore.write_update(b"", update_kind="diff")
                        _record_doc_timing(timings, "ystore_write_update", stage_started, prefix=timing_prefix)
                    except Exception as exc:
                        _record_doc_timing(timings, "ystore_write_update", stage_started, prefix=timing_prefix)
                        _log.warning("async_get_ydoc write_update failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
                    stage_started = time.perf_counter()
                    _schedule_room_update(webspace_id, update)
                    _record_doc_timing(timings, "room_update", stage_started, prefix=timing_prefix)
            else:
                _set_doc_timing(timings, "encode_diff", 0.0, prefix=timing_prefix)
                _set_doc_timing(timings, "ystore_write_update", 0.0, prefix=timing_prefix)
                _set_doc_timing(timings, "room_update", 0.0, prefix=timing_prefix)
    finally:
        if use_live_room:
            _set_doc_timing(timings, "ystore_stop", 0.0, prefix=timing_prefix)
        else:
            stage_started = time.perf_counter()
            try:
                ystore.stop()
            except Exception:
                pass
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
        prefer_live_room=True,
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
    if not _live_room_pipeline_ready(room):
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
    if not _live_room_pipeline_ready(room):
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
