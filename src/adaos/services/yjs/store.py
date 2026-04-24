from __future__ import annotations

import logging
import os
import time
import contextlib
import contextvars
import asyncio
import inspect
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
_WRITE_META: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("adaos_ystore_write_meta", default=None)
_GLOBAL_WRITE_LISTENERS: list[Callable[[str, bytes], Any]] = []


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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


@contextlib.asynccontextmanager
async def ystore_write_metadata(
    *,
    root_names: list[str] | tuple[str, ...] | None = None,
    source: str | None = None,
    owner: str | None = None,
    channel: str | None = None,
):
    payload = dict(_WRITE_META.get() or {})
    names = [str(name or "").strip() for name in (root_names or ()) if str(name or "").strip()]
    if names:
        payload["root_names"] = names
    if source is not None:
        payload["source"] = str(source or "").strip() or None
    if owner is not None:
        payload["owner"] = str(owner or "").strip() or None
    if channel is not None:
        payload["channel"] = str(channel or "").strip() or None
    token = _WRITE_META.set(payload)
    try:
        yield
    finally:
        try:
            _WRITE_META.reset(token)
        except Exception:
            pass


@contextlib.contextmanager
def ystore_write_metadata_sync(
    *,
    root_names: list[str] | tuple[str, ...] | None = None,
    source: str | None = None,
    owner: str | None = None,
    channel: str | None = None,
):
    payload = dict(_WRITE_META.get() or {})
    names = [str(name or "").strip() for name in (root_names or ()) if str(name or "").strip()]
    if names:
        payload["root_names"] = names
    if source is not None:
        payload["source"] = str(source or "").strip() or None
    if owner is not None:
        payload["owner"] = str(owner or "").strip() or None
    if channel is not None:
        payload["channel"] = str(channel or "").strip() or None
    token = _WRITE_META.set(payload)
    try:
        yield
    finally:
        try:
            _WRITE_META.reset(token)
        except Exception:
            pass


def _listener_accepts_meta(cb: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(cb)
    except Exception:
        return False
    params = list(sig.parameters.values())
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params):
        return True
    positional = [
        param
        for param in params
        if param.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return len(positional) >= 3


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
    meta = dict(_WRITE_META.get() or {})
    for cb in list(_GLOBAL_WRITE_LISTENERS):
        try:
            if meta and _listener_accepts_meta(cb):
                res = cb(webspace_id, update, meta)
            else:
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


def _encode_snapshot_update(updates: List[Tuple[bytes, bytes, float]]) -> bytes:
    """
    Heavy snapshot encoding performed in a worker thread.
    """
    if not updates:
        return b""
    if len(updates) == 1:
        return bytes(updates[0][0] or b"")

    ydoc = Y.YDoc()
    for update, _meta, _ts in updates:
        Y.apply_update(ydoc, update)  # type: ignore[arg-type]
    return Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]


def _encode_snapshot_artifacts(updates: List[Tuple[bytes, bytes, float]]) -> tuple[bytes, bytes]:
    """
    Encode a compacted snapshot together with its state vector in one pass.
    """
    if not updates:
        return b"", b""
    ydoc = Y.YDoc()
    for update, _meta, _ts in updates:
        Y.apply_update(ydoc, update)  # type: ignore[arg-type]
    return (
        Y.encode_state_as_update(ydoc),  # type: ignore[arg-type]
        Y.encode_state_vector(ydoc),  # type: ignore[arg-type]
    )


def _decode_state_vector_from_snapshot(snapshot: bytes) -> bytes:
    """
    Recover a state vector from one compacted snapshot update.
    """
    if not snapshot:
        return b""
    ydoc = Y.YDoc()
    Y.apply_update(ydoc, snapshot)  # type: ignore[arg-type]
    return Y.encode_state_vector(ydoc)  # type: ignore[arg-type]


def _persist_snapshot(path: Path, snapshot: bytes) -> int:
    """
    Heavy snapshot writing performed in a worker thread.
    """
    if not snapshot:
        try:
            path.unlink()
        except FileNotFoundError:
            return 0
        except Exception as exc:
            _log.warning("failed to remove stale YStore snapshot %s: %s", path, exc, exc_info=True)
        return 0
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
        self.max_replay_bytes = _env_int("ADAOS_YSTORE_MAX_REPLAY_BYTES", 512 * 1024, minimum=0)
        self.auto_backup_after_compact = _env_flag("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", True)
        self.auto_backup_cooldown_sec = _env_float("ADAOS_YSTORE_AUTOBACKUP_COOLDOWN_SEC", 30.0, minimum=0.0)
        self.auto_backup_debounce_sec = _env_float("ADAOS_YSTORE_AUTOBACKUP_DEBOUNCE_SEC", 0.5, minimum=0.0)
        self._lock: Lock = Lock()
        self._updates: List[Tuple[bytes, bytes, float]] = []
        self._base_snapshot_present = False
        self._loaded_from_disk = False
        self._started: Event | None = None
        self._starting: bool = False
        self._task_group = None
        self._running: bool = False
        self._write_total = 0
        self._compact_total = 0
        self._backup_total = 0
        self._backup_fast_path_total = 0
        self._backup_skipped_total = 0
        self._auto_backup_total = 0
        self._diff_write_total = 0
        self._snapshot_write_total = 0
        self._write_skipped_total = 0
        self._apply_total = 0
        self._applied_update_total = 0
        self._applied_update_bytes = 0
        self._last_write_at = 0.0
        self._last_compact_at = 0.0
        self._last_compact_reason = ""
        self._last_backup_at = 0.0
        self._last_auto_backup_at = 0.0
        self._last_auto_backup_reason = ""
        self._last_backup_mode = ""
        self._last_apply_at = 0.0
        self._last_loaded_from_disk_at = 0.0
        self._last_update_bytes = 0
        self._last_snapshot_bytes = 0
        self._last_apply_update_total = 0
        self._last_apply_bytes = 0
        self._auto_backup_inflight = False
        self._generation = 0
        self._persisted_generation = -1
        self._persisted_snapshot_bytes = 0
        self._base_state_vector: bytes | None = None
        self._state_vector_fast_path_total = 0
        self._state_vector_compute_total = 0
        self._state_vector_cache_miss_total = 0
        self._last_apply_mode = ""

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

    def _clear_runtime_state_locked(self) -> tuple[int, int]:
        released_entries = len(self._updates)
        released_bytes = sum(len(update) for update, _meta, _ts in self._updates)
        self._updates.clear()
        self._base_snapshot_present = False
        self._loaded_from_disk = False
        self._running = False
        self._auto_backup_inflight = False
        self._generation = 0
        self._persisted_generation = -1
        self._persisted_snapshot_bytes = 0
        self._base_state_vector = None
        self._last_apply_update_total = 0
        self._last_apply_bytes = 0
        self._last_apply_mode = ""
        self._last_loaded_from_disk_at = 0.0
        return released_entries, released_bytes

    async def evict_runtime_state(self) -> dict[str, int]:
        async with self._lock:
            released_entries, released_bytes = self._clear_runtime_state_locked()
        return {
            "released_update_entries": int(released_entries),
            "released_update_bytes": int(released_bytes),
        }

    async def write(self, data: bytes) -> None:  # type: ignore[override]
        """
        Append an update to the in-memory log, with optional TTL-based squashing.
        """
        await self.write_update(data)

    async def write_update(
        self,
        data: bytes,
        *,
        update_kind: str = "raw",
        notify: bool = True,
        state_vector: bytes | None = None,
    ) -> bool:
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
        auto_backup_reason: str | None = None
        async with self._lock:
            was_empty = not self._updates
            self._write_total += 1
            if update_kind == "diff":
                self._diff_write_total += 1
            elif update_kind == "snapshot":
                self._snapshot_write_total += 1
                if not self._updates:
                    self._base_snapshot_present = True
                    self._base_state_vector = bytes(state_vector or b"") or None
            self._last_write_at = now
            self._last_update_bytes = len(payload)
            if update_kind != "snapshot" or not was_empty:
                self._base_state_vector = None
            if self.document_ttl is not None and self._updates:
                last_ts = self._updates[-1][2]
                if now - last_ts > self.document_ttl:
                    # Squash stale history into a snapshot and continue with a
                    # fresh append-only window.
                    self._compact_updates_locked(now=now, keep_tail=0, reason="document_ttl")

            self._updates.append((payload, metadata, now))
            compact_reason = self._replay_compaction_reason_locked()
            if compact_reason:
                self._compact_updates_locked(now=now, keep_tail=self.replay_window, reason=compact_reason)
                if (
                    self.auto_backup_after_compact
                    and not self._auto_backup_inflight
                    and (self._last_auto_backup_at <= 0.0 or now - self._last_auto_backup_at >= self.auto_backup_cooldown_sec)
                ):
                    self._auto_backup_inflight = True
                    auto_backup_reason = compact_reason
            self._generation += 1
        if notify:
            try:
                _notify_write_listeners(self.path, payload)
            except Exception:
                pass
        if auto_backup_reason:
            self._schedule_auto_backup(reason=auto_backup_reason)
        return True

    async def encode_state_as_update(self, ydoc: Y.YDoc) -> None:  # type: ignore[override]
        update = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]
        state_vector = Y.encode_state_vector(ydoc)  # type: ignore[arg-type]
        await self.write_update(update, update_kind="snapshot", state_vector=state_vector)

    async def apply_updates(self, ydoc: Y.YDoc) -> None:  # type: ignore[override]
        await self._load_from_disk_if_needed()
        async with self._lock:
            if not self._updates:
                raise YDocNotFound
            updates = list(self._updates)
            apply_mode = "base_snapshot" if len(self._updates) == 1 and self._base_snapshot_present else "replay_log"

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
                self._last_apply_mode = apply_mode

    async def current_state_vector(self) -> bytes | None:
        """
        Return the full-document state vector when the store is already
        compacted to a single base snapshot.

        This lets detached YDoc sessions skip one extra encode pass on entry.
        """
        await self._load_from_disk_if_needed()
        snapshot = b""
        async with self._lock:
            if len(self._updates) != 1 or not self._base_snapshot_present:
                self._state_vector_cache_miss_total += 1
                return None
            if self._base_state_vector is not None:
                self._state_vector_fast_path_total += 1
                return bytes(self._base_state_vector)
            snapshot = bytes(self._updates[0][0] or b"")

        try:
            state_vector = await anyio.to_thread.run_sync(_decode_state_vector_from_snapshot, snapshot)
        except Exception:
            async with self._lock:
                self._state_vector_cache_miss_total += 1
            return None

        async with self._lock:
            if len(self._updates) == 1 and self._base_snapshot_present:
                self._base_state_vector = bytes(state_vector or b"") or None
                self._state_vector_compute_total += 1
                self._state_vector_fast_path_total += 1
                return bytes(self._base_state_vector or b"") or None
            self._state_vector_cache_miss_total += 1
        return None

    def _replay_window_bytes_locked(self, updates: List[Tuple[bytes, bytes, float]] | None = None) -> int:
        snapshot = list(updates if updates is not None else self._updates)
        if not snapshot:
            return 0
        start_idx = 1 if self._base_snapshot_present and len(snapshot) > 0 else 0
        return sum(len(update) for update, _meta, _ts in snapshot[start_idx:])

    def _replay_compaction_reason_locked(self) -> str | None:
        total = len(self._updates)
        if total <= 1:
            return None
        if total > self.max_updates:
            return "entry_limit"
        if self.max_replay_bytes > 0 and self._replay_window_bytes_locked() > self.max_replay_bytes:
            return "byte_limit"
        return None

    def _base_snapshot_bytes_locked(self, updates: List[Tuple[bytes, bytes, float]] | None = None) -> bytes:
        snapshot = list(updates if updates is not None else self._updates)
        if not snapshot or not self._base_snapshot_present:
            return b""
        return bytes(snapshot[0][0] or b"")

    def _compact_updates_locked(
        self,
        *,
        now: float,
        keep_tail: int | None = None,
        reason: str | None = None,
    ) -> None:
        updates = list(self._updates)
        if not updates:
            return
        total = len(updates)
        tail_count = self.replay_window if keep_tail is None else int(keep_tail)
        tail_count = max(0, min(tail_count, max(0, total - 1)))
        tail_byte_limit = int(self.max_replay_bytes) if self.max_replay_bytes > 0 else 0
        keep_from = total
        kept_total = 0
        kept_bytes = 0
        while keep_from > 0 and kept_total < tail_count:
            candidate_index = keep_from - 1
            if candidate_index <= 0:
                break
            candidate_update = updates[candidate_index][0]
            candidate_size = len(candidate_update)
            if tail_byte_limit > 0 and kept_total > 0 and kept_bytes + candidate_size > tail_byte_limit:
                break
            keep_from = candidate_index
            kept_total += 1
            kept_bytes += candidate_size
        prefix_count = max(1, keep_from)
        prefix = updates[:prefix_count]
        tail = updates[prefix_count:]
        snapshot_state_vector: bytes | None = None
        if prefix_count == 1 and self._base_snapshot_present:
            snapshot = self._base_snapshot_bytes_locked(prefix)
            snapshot_state_vector = bytes(self._base_state_vector or b"") or None
        else:
            ydoc = Y.YDoc()
            for update, _meta, _ts in prefix:
                Y.apply_update(ydoc, update)  # type: ignore[arg-type]
            snapshot = Y.encode_state_as_update(ydoc)  # type: ignore[arg-type]
            snapshot_state_vector = Y.encode_state_vector(ydoc)  # type: ignore[arg-type]
        metadata = prefix[-1][1] if prefix else b""
        self._updates = [(snapshot, metadata, now), *tail]
        self._base_snapshot_present = True
        self._base_state_vector = snapshot_state_vector if not tail else None
        self._compact_total += 1
        self._last_compact_at = now
        self._last_compact_reason = str(reason or "manual").strip() or "manual"
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

        try:
            state_vector = await anyio.to_thread.run_sync(_decode_state_vector_from_snapshot, data)
        except Exception:
            state_vector = b""

        metadata = await self.get_metadata()
        now = time.time()
        async with self._lock:
            if not self._updates:
                self._updates.append((data, metadata, now))
                self._base_snapshot_present = True
                self._base_state_vector = bytes(state_vector or b"") or None
                if self._base_state_vector is not None:
                    self._state_vector_compute_total += 1
                self._last_loaded_from_disk_at = now
                self._last_snapshot_bytes = len(data)
                self._persisted_generation = int(self._generation)
                self._persisted_snapshot_bytes = len(data)
        self._loaded_from_disk = True

    def _schedule_auto_backup(self, *, reason: str) -> bool:
        async def _runner() -> None:
            try:
                if self.auto_backup_debounce_sec > 0:
                    await asyncio.sleep(self.auto_backup_debounce_sec)
                await self.backup_to_disk(compact_runtime=True, backup_kind=f"auto_after_compact:{reason}")
            except Exception as exc:
                _log.warning(
                    "auto YStore backup failed for webspace=%s reason=%s: %s",
                    self.path,
                    reason,
                    exc,
                    exc_info=True,
                )
            finally:
                async with self._lock:
                    self._auto_backup_inflight = False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(_runner())
        return True

    async def request_runtime_compaction(self, *, reason: str = "manual") -> bool:
        token = str(reason or "").strip().lower().replace(" ", "_") or "manual"
        async with self._lock:
            has_replay_tail = len(self._updates) > 1 or self._replay_window_bytes_locked() > 0
            if not has_replay_tail or self._auto_backup_inflight:
                return False
            self._auto_backup_inflight = True
        if self._schedule_auto_backup(reason=f"idle_{token}"):
            return True
        async with self._lock:
            self._auto_backup_inflight = False
        return False

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

    async def backup_to_disk(
        self,
        *,
        compact_runtime: bool = True,
        backup_kind: str = "manual",
    ) -> None:
        """
        Persist the current YDoc state as a single update snapshot.
        """
        async with self._lock:
            updates = list(self._updates)
            generation = int(self._generation)
            metadata = updates[-1][1] if updates else b""
            cached_state_vector = bytes(self._base_state_vector or b"") or None
        path = ystore_path_for_webspace(self.path)
        snapshot_exists = path.exists()
        snapshot = b""
        snapshot_state_vector: bytes | None = None
        backup_mode = "empty"
        used_fast_path = False
        if updates:
            if len(updates) == 1 and bool(self._base_snapshot_present):
                snapshot = self._base_snapshot_bytes_locked(updates)
                snapshot_state_vector = cached_state_vector
                backup_mode = "runtime_base_snapshot"
                used_fast_path = True
                if snapshot and snapshot_state_vector is None:
                    snapshot_state_vector = await anyio.to_thread.run_sync(_decode_state_vector_from_snapshot, snapshot)
            else:
                snapshot, snapshot_state_vector = await anyio.to_thread.run_sync(_encode_snapshot_artifacts, updates)
                backup_mode = "encoded_runtime_log"

        skip_write = bool(not snapshot and not snapshot_exists)
        written_bytes = 0
        if not skip_write:
            async with self._lock:
                persisted_generation = int(self._persisted_generation)
                persisted_snapshot_bytes = int(self._persisted_snapshot_bytes)
            up_to_date = bool(
                snapshot
                and snapshot_exists
                and generation >= 0
                and generation == persisted_generation
                and len(snapshot) == persisted_snapshot_bytes
            )
            if up_to_date:
                skip_write = True
            else:
                written_bytes = await anyio.to_thread.run_sync(_persist_snapshot, path, snapshot)
        now = time.time()
        async with self._lock:
            self._backup_total += 1
            if used_fast_path:
                self._backup_fast_path_total += 1
            if skip_write:
                self._backup_skipped_total += 1
            self._last_backup_mode = f"{backup_mode}:skipped" if skip_write else backup_mode
            self._last_backup_at = now
            if written_bytes:
                self._last_snapshot_bytes = int(written_bytes)
            if backup_kind.startswith("auto_after_compact:"):
                self._auto_backup_total += 1
                self._last_auto_backup_at = now
                self._last_auto_backup_reason = str(backup_kind.partition(":")[2] or "").strip()
            compacted_runtime = False
            if (
                compact_runtime
                and (written_bytes or skip_write)
                and snapshot
                and self._generation == generation
                and (
                    len(self._updates) != 1
                    or not self._base_snapshot_present
                    or bytes(self._updates[0][0] or b"") != bytes(snapshot)
                )
            ):
                self._updates = [(bytes(snapshot), metadata, now)]
                self._base_snapshot_present = True
                self._base_state_vector = bytes(snapshot_state_vector or b"") or None
                self._compact_total += 1
                self._last_compact_at = now
                self._last_compact_reason = "backup_compaction"
                self._generation += 1
                compacted_runtime = True
            if compacted_runtime:
                self._persisted_generation = int(self._generation)
                self._persisted_snapshot_bytes = len(snapshot)
            elif skip_write:
                self._persisted_generation = generation
                self._persisted_snapshot_bytes = len(snapshot)
            elif written_bytes and self._generation == generation:
                self._persisted_generation = generation
                self._persisted_snapshot_bytes = int(written_bytes)
            if (
                snapshot
                and snapshot_state_vector
                and self._generation == generation
                and len(self._updates) == 1
                and self._base_snapshot_present
                and bytes(self._updates[0][0] or b"") == bytes(snapshot)
            ):
                self._base_state_vector = bytes(snapshot_state_vector)
            if backup_kind.startswith("auto_after_compact:") and not compacted_runtime and self._last_auto_backup_reason:
                # Keep the last auto-backup reason observable even when concurrent
                # writes made runtime-side collapse unsafe for this round.
                self._last_auto_backup_reason = self._last_auto_backup_reason

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
        base_snapshot_present = bool(updates) and bool(self._base_snapshot_present)
        replay_window_entries = max(0, update_log_entries - (1 if base_snapshot_present else 0))
        replay_window_bytes = self._replay_window_bytes_locked(updates)
        runtime_compaction_eligible = bool(update_log_entries > 1 or replay_window_bytes > 0)
        persisted_up_to_date = bool(
            (update_log_entries <= 0 and not snapshot_exists)
            or (snapshot_exists and int(self._persisted_generation) == int(self._generation))
        )
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
            "base_snapshot_present": bool(base_snapshot_present),
            "replay_window_entries": replay_window_entries,
            "replay_window_limit": int(self.replay_window),
            "replay_window_bytes": int(replay_window_bytes),
            "replay_window_byte_limit": int(self.max_replay_bytes),
            "runtime_compaction_eligible": runtime_compaction_eligible,
            "max_update_log_entries": int(self.max_updates),
            "loaded_from_disk": bool(self._loaded_from_disk),
            "running": bool(self._running),
            "write_total": int(self._write_total),
            "compact_total": int(self._compact_total),
            "backup_total": int(self._backup_total),
            "backup_fast_path_total": int(self._backup_fast_path_total),
            "backup_skipped_total": int(self._backup_skipped_total),
            "auto_backup_total": int(self._auto_backup_total),
            "diff_write_total": int(self._diff_write_total),
            "snapshot_write_total": int(self._snapshot_write_total),
            "write_skipped_total": int(self._write_skipped_total),
            "apply_total": int(self._apply_total),
            "applied_update_total": int(self._applied_update_total),
            "applied_update_bytes": int(self._applied_update_bytes),
            "auto_backup_after_compact": bool(self.auto_backup_after_compact),
            "auto_backup_cooldown_sec": float(self.auto_backup_cooldown_sec),
            "auto_backup_debounce_sec": float(self.auto_backup_debounce_sec),
            "auto_backup_inflight": bool(self._auto_backup_inflight),
            "snapshot_file_exists": bool(snapshot_exists),
            "snapshot_file_size": int(snapshot_size),
            "persisted_generation": int(self._persisted_generation) if self._persisted_generation >= 0 else None,
            "persisted_snapshot_bytes": int(self._persisted_snapshot_bytes),
            "persisted_up_to_date": persisted_up_to_date,
            "cached_state_vector_bytes": len(self._base_state_vector or b""),
            "state_vector_fast_path_total": int(self._state_vector_fast_path_total),
            "state_vector_compute_total": int(self._state_vector_compute_total),
            "state_vector_cache_miss_total": int(self._state_vector_cache_miss_total),
            "last_update_bytes": int(self._last_update_bytes),
            "last_snapshot_bytes": int(self._last_snapshot_bytes),
            "last_backup_mode": self._last_backup_mode or None,
            "last_apply_update_total": int(self._last_apply_update_total),
            "last_apply_bytes": int(self._last_apply_bytes),
            "last_apply_mode": self._last_apply_mode or None,
            "last_write_at": self._last_write_at or None,
            "last_write_ago_s": round(max(0.0, now - self._last_write_at), 3) if self._last_write_at else None,
            "last_compact_at": self._last_compact_at or None,
            "last_compact_reason": self._last_compact_reason or None,
            "last_compact_ago_s": round(max(0.0, now - self._last_compact_at), 3) if self._last_compact_at else None,
            "last_backup_at": self._last_backup_at or None,
            "last_backup_ago_s": round(max(0.0, now - self._last_backup_at), 3) if self._last_backup_at else None,
            "last_auto_backup_at": self._last_auto_backup_at or None,
            "last_auto_backup_reason": self._last_auto_backup_reason or None,
            "last_auto_backup_ago_s": round(max(0.0, now - self._last_auto_backup_at), 3)
            if self._last_auto_backup_at
            else None,
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
            store.stop()
        except Exception:
            pass
        try:
            store._clear_runtime_state_locked()  # type: ignore[attr-defined]
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
            store.stop()
        except Exception:
            pass
        try:
            store._clear_runtime_state_locked()  # type: ignore[attr-defined]
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


async def evict_ystore_for_webspace(
    webspace_id: str,
    *,
    store: AdaosMemoryYStore | None = None,
    persist_snapshot: bool = True,
    compact_runtime: bool = True,
    backup_kind: str = "evict",
    delete_snapshot: bool = False,
) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    cached = _YSTORE_CACHE.pop(key, None)
    extra_target = cached if cached is not None and cached is not store else None
    target = store or cached
    if target is None:
        removed_snapshot = False
        if delete_snapshot:
            try:
                path = ystore_path_for_webspace(key)
                if path.exists():
                    path.unlink()
                    removed_snapshot = True
            except Exception:
                _log.warning("failed to remove YStore snapshot for webspace=%s", key, exc_info=True)
        return {
            "ok": True,
            "webspace_id": key,
            "ystore_found": False,
            "persisted": False,
            "snapshot_deleted": removed_snapshot,
            "released_update_entries": 0,
            "released_update_bytes": 0,
        }

    persisted = False
    backup_skipped = False
    backup_error: str | None = None
    if persist_snapshot:
        try:
            await target.backup_to_disk(
                compact_runtime=compact_runtime,
                backup_kind=backup_kind,
            )
            snapshot = target.runtime_snapshot()
            persisted = bool(snapshot.get("snapshot_file_exists"))
            backup_skipped = bool(snapshot.get("persisted_up_to_date"))
        except Exception as exc:
            backup_error = f"{type(exc).__name__}: {exc}"
            _log.warning("failed to persist YStore before eviction webspace=%s", key, exc_info=True)

    try:
        result = target.stop()
        if inspect.isawaitable(result):
            await result
    except Exception:
        _log.debug("failed to stop YStore before eviction webspace=%s", key, exc_info=True)

    released = {"released_update_entries": 0, "released_update_bytes": 0}
    try:
        released = await target.evict_runtime_state()
    except Exception:
        _log.warning("failed to clear YStore runtime state webspace=%s", key, exc_info=True)

    if extra_target is not None:
        try:
            result = extra_target.stop()
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.debug("failed to stop cached YStore during eviction webspace=%s", key, exc_info=True)
        try:
            await extra_target.evict_runtime_state()
        except Exception:
            _log.warning("failed to clear cached YStore runtime state webspace=%s", key, exc_info=True)

    removed_snapshot = False
    if delete_snapshot:
        try:
            path = ystore_path_for_webspace(key)
            if path.exists():
                path.unlink()
                removed_snapshot = True
        except Exception:
            _log.warning("failed to remove YStore snapshot for webspace=%s", key, exc_info=True)

    return {
        "ok": backup_error is None,
        "webspace_id": key,
        "ystore_found": True,
        "persisted": persisted,
        "backup_skipped": backup_skipped,
        "backup_error": backup_error,
        "snapshot_deleted": removed_snapshot,
        **released,
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
