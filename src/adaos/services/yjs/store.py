from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import AsyncIterator, Dict, List, Tuple

import anyio
import y_py as Y
from anyio import Event, Lock, TASK_STATUS_IGNORED
from anyio.abc import TaskStatus
from ypy_websocket.ystore import BaseYStore, YDocNotFound

from adaos.services.agent_context import get_ctx
from adaos.sdk.core.decorators import subscribe

_log = logging.getLogger("adaos.yjs.ystore")


def _persist_snapshot(path: Path, updates: List[Tuple[bytes, bytes, float]]) -> None:
    """
    Heavy snapshot encoding/writing performed in a worker thread.
    """
    if not updates:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            _log.warning("failed to remove stale YStore snapshot %s: %s", path, exc, exc_info=True)
        return

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
    except Exception as exc:
        _log.warning("failed to write YStore snapshot %s: %s", path, exc, exc_info=True)


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


class AdaosMemoryYStore(BaseYStore):
    """
    In-memory YStore with optional periodic snapshots to disk.

    - All Y updates are kept in-memory in the current process.
    - `read()` replays the in-memory log or, on first access, a persisted
      snapshot from disk (if present).
    - `backup_to_disk()` compresses the current log into a single
      `Y.encode_state_as_update(ydoc)` blob and writes it atomically.
    """

    def __init__(self, path: str, *, document_ttl: float | None = None):
        # BaseYStore expects these attributes; its __init__ is abstract/no-op.
        self.path = path
        self.metadata_callback = None
        self.document_ttl = document_ttl
        self._lock: Lock = Lock()
        self._updates: List[Tuple[bytes, bytes, float]] = []
        self._loaded_from_disk = False
        self._started: Event | None = None
        self._starting: bool = False
        self._task_group = None
        self._running: bool = False

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
        metadata = await self.get_metadata()
        now = time.time()
        async with self._lock:
            if self.document_ttl is not None and self._updates:
                last_ts = self._updates[-1][2]
                if now - last_ts > self.document_ttl:
                    # Squash history into a single snapshot.
                    ydoc = Y.YDoc()
                    for update, _meta, _ts in self._updates:
                        Y.apply_update(ydoc, update)  # type: ignore[arg-type]
                    self._updates.clear()
                    squashed = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]
                    self._updates.append((squashed, metadata, now))
                    return

            self._updates.append((data, metadata, now))

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
        await anyio.to_thread.run_sync(_persist_snapshot, path, updates)


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

