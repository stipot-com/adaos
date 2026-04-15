from __future__ import annotations

import logging
import os
import time
import contextlib
import contextvars
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Tuple

import anyio
import y_py as Y
from anyio import Event, Lock, TASK_STATUS_IGNORED
from anyio.abc import TaskStatus
from ypy_websocket.ystore import BaseYStore, YDocNotFound

from adaos.services.agent_context import get_ctx
from adaos.sdk.core.decorators import subscribe

_log = logging.getLogger("adaos.yjs.ystore")

_SUPPRESS_NOTIFY: contextvars.ContextVar[bool] = contextvars.ContextVar("adaos_ystore_suppress_notify", default=False)
_GLOBAL_WRITE_LISTENERS: list[Callable[[str, bytes], Any]] = []


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def add_ystore_write_listener(cb: Callable[[str, bytes], Any]) -> Callable[[], None]:
    """
    Register a global listener called on every YStore write:
      cb(webspace_id: str, update: bytes) -> Any

    Returns a function that removes the listener.
    """
    _GLOBAL_WRITE_LISTENERS.append(cb)

    def _remove() -> None:
        try:
            _GLOBAL_WRITE_LISTENERS.remove(cb)
        except ValueError:
            return

    return _remove


@contextlib.asynccontextmanager
async def suppress_ystore_write_notifications():
    token = _SUPPRESS_NOTIFY.set(True)
    try:
        yield
    finally:
        try:
            _SUPPRESS_NOTIFY.reset(token)
        except Exception:
            pass


def _notify_write_listeners(webspace_id: str, update: bytes) -> None:
    if _SUPPRESS_NOTIFY.get():
        return
    if not _GLOBAL_WRITE_LISTENERS:
        return
    # Best-effort, never block the writer.
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except Exception:
        loop = None
    for cb in list(_GLOBAL_WRITE_LISTENERS):
        try:
            res = cb(webspace_id, update)
            if loop is not None:
                try:
                    import asyncio

                    if asyncio.iscoroutine(res):
                        loop.create_task(res)
                except Exception:
                    pass
        except Exception:
            continue


def _persist_snapshot(path: Path, updates: List[Tuple[bytes, bytes, float]]) -> int:
    """
    Heavy snapshot encoding/writing performed in a worker thread.
    """
    if not updates:
        try:
            path.unlink()
        except FileNotFoundError:
            return 0
        except Exception as exc:
            _log.warning("failed to remove stale YStore snapshot %s: %s", path, exc, exc_info=True)
        return 0

    ydoc = Y.YDoc()
    for update, _meta, _ts in updates:
        Y.apply_update(ydoc, update)  # type: ignore[arg-type]
    snapshot = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]

    tmp = Path(str(path) + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(snapshot)
        tmp.replace(path)
        _log.debug("YStore snapshot written for webspace=%s path=%s", path.name.removesuffix(".sqlite3"), path)
        return len(snapshot)
    except Exception as exc:
        _log.warning("failed to write YStore snapshot %s: %s", path, exc, exc_info=True)
        return 0


def ystores_root() -> Path:
    """
    Root directory for Yjs store snapshots, ensuring it exists.

    Even though the live store is in-memory, we keep periodic snapshots here
    so that webspaces can be restored across restarts.
    """
    ctx = get_ctx()
    root = ctx.paths.state_dir() / "ystores"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ystore_path_for_webspace(webspace_id: str) -> Path:
    """
    Map a webspace id to a filesystem path for its snapshot.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in webspace_id)
    # We keep the historical .sqlite3 suffix even though the file now contains
    # a single encoded YDoc snapshot, to avoid surprising existing tooling.
    return ystores_root() / f"{safe}.sqlite3"


def ystore_snapshot_exists(webspace_id: str) -> bool:
    try:
        return ystore_path_for_webspace(str(webspace_id or "")).exists()
    except Exception:
        return False


class AdaosMemoryYStore(BaseYStore):
    """
    In-memory YStore with optional periodic snapshots to disk.

    - All Y updates are kept in-memory in the current process.
    - `read()` replays the in-memory log or, on first access, a persisted
      snapshot from disk (if present).
    - `backup_to_disk()` compresses the current log into a single
      `Y.encode_state_as_update(ydoc)` blob and writes it atomically.
    - Hot-path callers may append incremental diff updates directly via
      `write_update()` instead of re-encoding the full document on every flush.
    """

    def __init__(self, path: str, *, document_ttl: float | None = None):
        # BaseYStore expects these attributes; its __init__ is abstract/no-op.
        self.path = path
        self.metadata_callback = None
        self.document_ttl = document_ttl
        self.max_updates = _env_int("ADAOS_YSTORE_MAX_UPDATES", 128, minimum=8)
        self.replay_window = min(
            self.max_updates - 1,
            _env_int("ADAOS_YSTORE_REPLAY_WINDOW", 32, minimum=0),
        )
        self._lock: Lock = Lock()
        self._updates: List[Tuple[bytes, bytes, float]] = []
        self._loaded_from_disk = False
        self._started: Event | None = None
        self._starting: bool = False
        self._task_group = None
        self._running: bool = False
        self._write_total = 0
        self._compact_total = 0
        self._backup_total = 0
        self._diff_write_total = 0
        self._snapshot_write_total = 0
        self._write_skipped_total = 0
        self._apply_total = 0
        self._applied_update_total = 0
        self._applied_update_bytes = 0
        self._last_write_at = 0.0
        self._last_compact_at = 0.0
        self._last_backup_at = 0.0
        self._last_apply_at = 0.0
        self._last_loaded_from_disk_at = 0.0
        self._last_update_bytes = 0
        self._last_snapshot_bytes = 0
        self._last_apply_update_total = 0
        self._last_apply_bytes = 0

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """
        For the in-memory store, start/stop are lightweight and idempotent.
        """
        if self._running:
            task_status.started()
            return
        self._running = True
        self.started.set()
        task_status.started()

    def stop(self) -> None:
        self._running = False

    async def write(self, data: bytes) -> None:  # type: ignore[override]
        """
        Append an update to the in-memory log, with optional TTL-based squashing.
        """
        await self.write_update(data)

    async def write_update(self, data: bytes, *, update_kind: str = "raw", notify: bool = True) -> bool:
        """
        Append one already-encoded Yjs update to the in-memory log.

        `update_kind` is diagnostic only and lets runtime snapshots distinguish
        full-state writes from incremental diff writes.
        """
        payload = bytes(data or b"")
        now = time.time()
        if not payload:
            async with self._lock:
                self._write_skipped_total += 1
            return False

        metadata = await self.get_metadata()
        async with self._lock:
            self._write_total += 1
            if update_kind == "diff":
                self._diff_write_total += 1
            elif update_kind == "snapshot":
                self._snapshot_write_total += 1
            self._last_write_at = now
            self._last_update_bytes = len(payload)
            if self.document_ttl is not None and self._updates:
                last_ts = self._updates[-1][2]
                if now - last_ts > self.document_ttl:
                    # Squash stale history into a snapshot and continue with a
                    # fresh append-only window.
                    self._compact_updates_locked(now=now, keep_tail=0)

            self._updates.append((payload, metadata, now))
            if len(self._updates) > self.max_updates:
                self._compact_updates_locked(now=now, keep_tail=self.replay_window)
        if notify:
            try:
                _notify_write_listeners(self.path, payload)
            except Exception:
                pass
        return True

    async def encode_state_as_update(self, ydoc: Y.YDoc) -> None:  # type: ignore[override]
        update = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]
        await self.write_update(update, update_kind="snapshot")

    async def apply_updates(self, ydoc: Y.YDoc) -> None:  # type: ignore[override]
        await self._load_from_disk_if_needed()
        async with self._lock:
            if not self._updates:
                raise YDocNotFound
            updates = list(self._updates)

        now = time.time()
        applied_total = 0
        applied_bytes = 0
        try:
            for update, _metadata, _ts in updates:
                Y.apply_update(ydoc, update)  # type: ignore[arg-type]
                applied_total += 1
                applied_bytes += len(update)
        finally:
            async with self._lock:
                self._apply_total += 1
                self._applied_update_total += applied_total
                self._applied_update_bytes += applied_bytes
                self._last_apply_at = now
                self._last_apply_update_total = applied_total
                self._last_apply_bytes = applied_bytes

    def _compact_updates_locked(self, *, now: float, keep_tail: int | None = None) -> None:
        updates = list(self._updates)
        if not updates:
            return
        total = len(updates)
        tail_count = self.replay_window if keep_tail is None else int(keep_tail)
        tail_count = max(0, min(tail_count, max(0, total - 1)))
        prefix_count = max(1, total - tail_count)
        prefix = updates[:prefix_count]
        tail = updates[prefix_count:]
        ydoc = Y.YDoc()
        for update, _meta, _ts in prefix:
            Y.apply_update(ydoc, update)  # type: ignore[arg-type]
        snapshot = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]
        metadata = prefix[-1][1] if prefix else b""
        self._updates = [(snapshot, metadata, now), *tail]
        self._compact_total += 1
        self._last_compact_at = now
        self._last_snapshot_bytes = len(snapshot)

    async def _load_from_disk_if_needed(self) -> None:
        if self._loaded_from_disk:
            return
        path = ystore_path_for_webspace(self.path)
        if not path.exists():
            self._loaded_from_disk = True
            return

        try:
            data = path.read_bytes()
        except Exception as exc:  # pragma: no cover - IO errors are logged only
            _log.warning("failed to read YStore snapshot %s: %s", path, exc, exc_info=True)
            self._loaded_from_disk = True
            return

        metadata = await self.get_metadata()
        now = time.time()
        async with self._lock:
            if not self._updates:
                self._updates.append((data, metadata, now))
                self._last_loaded_from_disk_at = now
                self._last_snapshot_bytes = len(data)
        self._loaded_from_disk = True

    async def read(self) -> AsyncIterator[tuple[bytes, bytes]]:  # type: ignore[override]
        """
        Async iterator over stored updates (update, metadata).
        """
        await self._load_from_disk_if_needed()
        async with self._lock:
            if not self._updates:
                raise YDocNotFound
            snapshot = list(self._updates)

        for update, metadata, _ts in snapshot:
            yield update, metadata

    async def backup_to_disk(self) -> None:
        """
        Persist the current YDoc state as a single update snapshot.
        """
        async with self._lock:
            updates = list(self._updates)

        path = ystore_path_for_webspace(self.path)
        written_bytes = await anyio.to_thread.run_sync(_persist_snapshot, path, updates)
        async with self._lock:
            self._backup_total += 1
            self._last_backup_at = time.time()
            if written_bytes:
                self._last_snapshot_bytes = int(written_bytes)

    def runtime_snapshot(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = time.time() if now_ts is None else float(now_ts)
        snapshot_path = ystore_path_for_webspace(self.path)
        snapshot_exists = snapshot_path.exists()
        try:
            snapshot_size = snapshot_path.stat().st_size if snapshot_exists else 0
        except Exception:
            snapshot_size = 0
        updates = list(self._updates)
        update_log_entries = len(updates)
        update_log_bytes = sum(len(update) for update, _meta, _ts in updates)
        base_snapshot_present = bool(updates) and bool(self._loaded_from_disk or self._compact_total > 0)
        replay_window_entries = max(0, update_log_entries - (1 if base_snapshot_present else 0))
        if update_log_entries <= 0:
            log_mode = "empty"
        elif base_snapshot_present:
            log_mode = "snapshot_plus_diff"
        else:
            log_mode = "append_only"
        return {
            "webspace_id": self.path,
            "log_mode": log_mode,
            "update_log_entries": update_log_entries,
            "update_log_bytes": int(update_log_bytes),
            "replay_window_entries": replay_window_entries,
            "replay_window_limit": int(self.replay_window),
            "max_update_log_entries": int(self.max_updates),
            "loaded_from_disk": bool(self._loaded_from_disk),
            "running": bool(self._running),
            "write_total": int(self._write_total),
            "compact_total": int(self._compact_total),
            "backup_total": int(self._backup_total),
            "diff_write_total": int(self._diff_write_total),
            "snapshot_write_total": int(self._snapshot_write_total),
            "write_skipped_total": int(self._write_skipped_total),
            "apply_total": int(self._apply_total),
            "applied_update_total": int(self._applied_update_total),
            "applied_update_bytes": int(self._applied_update_bytes),
            "snapshot_file_exists": bool(snapshot_exists),
            "snapshot_file_size": int(snapshot_size),
            "last_update_bytes": int(self._last_update_bytes),
            "last_snapshot_bytes": int(self._last_snapshot_bytes),
            "last_apply_update_total": int(self._last_apply_update_total),
            "last_apply_bytes": int(self._last_apply_bytes),
            "last_write_at": self._last_write_at or None,
            "last_write_ago_s": round(max(0.0, now - self._last_write_at), 3) if self._last_write_at else None,
            "last_compact_at": self._last_compact_at or None,
            "last_compact_ago_s": round(max(0.0, now - self._last_compact_at), 3) if self._last_compact_at else None,
            "last_backup_at": self._last_backup_at or None,
            "last_backup_ago_s": round(max(0.0, now - self._last_backup_at), 3) if self._last_backup_at else None,
            "last_apply_at": self._last_apply_at or None,
            "last_apply_ago_s": round(max(0.0, now - self._last_apply_at), 3) if self._last_apply_at else None,
            "last_loaded_from_disk_at": self._last_loaded_from_disk_at or None,
            "last_loaded_from_disk_ago_s": round(max(0.0, now - self._last_loaded_from_disk_at), 3)
            if self._last_loaded_from_disk_at
            else None,
        }


_YSTORE_CACHE: Dict[str, AdaosMemoryYStore] = {}


def get_ystore_for_webspace(webspace_id: str) -> AdaosMemoryYStore:
    """
        Return a cached in-memory YStore for the given webspace.

        All callers (web_desktop_skill, async_get_ydoc, y_gateway) share the same
        instance to avoid \"YStore already running\" races.
    """
    store = _YSTORE_CACHE.get(webspace_id)
    if store is None:
        store = AdaosMemoryYStore(webspace_id)
        _YSTORE_CACHE[webspace_id] = store
    return store


def ystore_runtime_snapshot(*, webspace_id: str | None = None, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    if webspace_id:
        store = get_ystore_for_webspace(str(webspace_id))
        return {
            "webspace_id": str(webspace_id),
            "webspace_total": 1,
            "webspaces": {
                str(webspace_id): store.runtime_snapshot(now_ts=now),
            },
        }

    webspaces: dict[str, Any] = {}
    active_total = 0
    for ws_id, store in sorted(_YSTORE_CACHE.items()):
        item = store.runtime_snapshot(now_ts=now)
        webspaces[str(ws_id)] = item
        if int(item.get("update_log_entries") or 0) > 0 or bool(item.get("snapshot_file_exists")):
            active_total += 1
    return {
        "webspace_total": len(webspaces),
        "active_webspace_total": active_total,
        "webspaces": webspaces,
    }


def reset_ystore_for_webspace(webspace_id: str) -> None:
    """
    Drop any in-memory Y updates for the given webspace so that future access
    starts from a clean YDoc. Used when corrupted updates cause Y.apply_update
    panics for a webspace that is being deleted or re-seeded.
    """
    store = _YSTORE_CACHE.pop(webspace_id, None)
    if store is not None:
        try:
            store._updates.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        path = ystore_path_for_webspace(webspace_id)
        if path.exists():
            path.unlink()
    except Exception:
        _log.warning("failed to remove YStore snapshot for webspace=%s", webspace_id, exc_info=True)


async def restore_ystore_for_webspace(webspace_id: str) -> dict[str, Any]:
    """
    Recreate the in-memory YStore for a webspace from its last persisted
    snapshot, without reseeding from scenario sources.
    """
    key = str(webspace_id or "").strip() or "default"
    path = ystore_path_for_webspace(key)
    snapshot_exists = path.exists()
    if not snapshot_exists:
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": key,
            "error": "snapshot_missing",
            "snapshot_path": str(path),
        }

    store = _YSTORE_CACHE.pop(key, None)
    if store is not None:
        try:
            store._updates.clear()  # type: ignore[attr-defined]
        except Exception:
            pass

    restored = AdaosMemoryYStore(key)
    _YSTORE_CACHE[key] = restored
    try:
        await restored._load_from_disk_if_needed()  # type: ignore[attr-defined]
    except Exception as exc:
        _log.warning("failed to restore YStore snapshot for webspace=%s: %s", key, exc, exc_info=True)
        return {
            "ok": False,
            "accepted": False,
            "webspace_id": key,
            "error": f"restore_failed:{type(exc).__name__}",
            "snapshot_path": str(path),
        }

    return {
        "ok": True,
        "accepted": True,
        "webspace_id": key,
        "snapshot_path": str(path),
        "runtime": restored.runtime_snapshot(),
    }


@subscribe("sys.ystore.backup")
async def _on_ystore_backup(payload: dict) -> None:
    """
    System handler: persist in-memory YStore snapshot for a webspace.

    This is triggered by the scheduler via `sys.ystore.backup` events.
    """
    if not isinstance(payload, dict):
        return
    webspace_id = str(payload.get("webspace_id") or payload.get("workspace_id") or "default")
    try:
        store = get_ystore_for_webspace(webspace_id)
        await store.backup_to_disk()
    except Exception as exc:  # pragma: no cover - defensive logging
        _log.warning("YStore backup failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
