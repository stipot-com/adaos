from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
import y_py as Y

from adaos.services.yjs import doc as ydoc_module
from adaos.services.yjs import store as ystore_module
from adaos.services.yjs.doc import async_get_ydoc, async_read_ydoc, get_ydoc
from adaos.services.yjs.store import get_ystore_for_webspace, reset_ystore_for_webspace

pytestmark = pytest.mark.anyio


def _webspace_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def test_async_get_ydoc_uses_diff_writeback() -> None:
    webspace_id = _webspace_id("diff-writeback")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("ui").set(txn, "current_scenario", "infrascope")

        snapshot = store.runtime_snapshot()
        assert snapshot["diff_write_total"] == 1
        assert snapshot["snapshot_write_total"] == 0
        assert snapshot["update_log_entries"] == 1
        assert snapshot["update_log_bytes"] > 0

        async with async_get_ydoc(webspace_id, read_only=True) as ydoc:
            assert ydoc.get_map("ui").get("current_scenario") == "infrascope"

        snapshot = store.runtime_snapshot()
        assert snapshot["apply_total"] >= 1
        assert snapshot["applied_update_total"] >= 1
        assert snapshot["applied_update_bytes"] >= snapshot["update_log_bytes"]
        assert snapshot["running"] is False
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_read_ydoc_collects_timings_without_name_error() -> None:
    webspace_id = _webspace_id("read-timings")
    timings: dict[str, float] = {}
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("ui").set(txn, "current_scenario", "web_desktop")

        async with async_read_ydoc(webspace_id, timings=timings, timing_prefix="probe.") as ydoc:
            assert ydoc.get_map("ui").get("current_scenario") == "web_desktop"

        assert "probe.ystore_start" in timings
        assert "probe.ystore_apply_updates" in timings
        assert "probe.ystore_stop" in timings
        assert "probe.total" in timings
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_get_ydoc_skips_noop_flush() -> None:
    webspace_id = _webspace_id("noop-flush")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("data").set(txn, "flag", True)

        first = store.runtime_snapshot()
        assert first["diff_write_total"] == 1
        assert first["update_log_entries"] == 1

        async with async_get_ydoc(webspace_id):
            pass

        second = store.runtime_snapshot()
        assert second["diff_write_total"] == 1
        assert second["update_log_entries"] == 1
        assert second["write_skipped_total"] == 0
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_get_ydoc_byte_budget_compacts_replay_window(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_MAX_UPDATES", "128")
    monkeypatch.setenv("ADAOS_YSTORE_REPLAY_WINDOW", "32")
    monkeypatch.setenv("ADAOS_YSTORE_MAX_REPLAY_BYTES", "700")
    webspace_id = _webspace_id("byte-budget")
    store = get_ystore_for_webspace(webspace_id)
    value = "x" * 256
    try:
        for idx in range(6):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"key_{idx}", value)

        snapshot = store.runtime_snapshot()
        assert snapshot["compact_total"] >= 1
        assert snapshot["last_compact_reason"] == "byte_limit"
        assert snapshot["base_snapshot_present"] is True
        assert snapshot["update_log_entries"] < 6
        assert snapshot["replay_window_byte_limit"] == 700

        async with async_read_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            for idx in range(6):
                assert data_map.get(f"key_{idx}") == value
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_runtime_snapshot_reports_replay_byte_budget(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_MAX_REPLAY_BYTES", "2048")
    webspace_id = _webspace_id("runtime-byte-budget")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("ui").set(txn, "current_scenario", "infrascope")

        snapshot = store.runtime_snapshot()
        assert snapshot["replay_window_byte_limit"] == 2048
        assert snapshot["replay_window_bytes"] >= 0
        assert "base_snapshot_present" in snapshot
        assert "last_compact_reason" in snapshot
        assert "auto_backup_total" in snapshot
        assert "last_auto_backup_reason" in snapshot
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_backup_to_disk_compacts_runtime_log(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", "0")
    webspace_id = _webspace_id("backup-compact")
    store = get_ystore_for_webspace(webspace_id)
    try:
        for idx in range(3):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"item_{idx}", idx)

        before = store.runtime_snapshot()
        assert before["update_log_entries"] == 3

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")

        after = store.runtime_snapshot()
        assert after["snapshot_file_exists"] is True
        assert after["backup_total"] >= 1
        assert after["update_log_entries"] == 1
        assert after["base_snapshot_present"] is True
        assert after["last_compact_reason"] == "backup_compaction"

        async with async_read_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            for idx in range(3):
                assert data_map.get(f"item_{idx}") == idx
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_backup_to_disk_reuses_runtime_base_snapshot_without_reencode(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", "0")
    webspace_id = _webspace_id("backup-fast-path")
    store = get_ystore_for_webspace(webspace_id)
    try:
        for idx in range(3):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"item_{idx}", idx)

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")
        path = ystore_module.ystore_path_for_webspace(webspace_id)
        assert path.exists() is True
        path.unlink()

        monkeypatch.setattr(
            ystore_module,
            "_encode_snapshot_artifacts",
            lambda updates: (_ for _ in ()).throw(AssertionError("encode should not run for base snapshot fast-path")),
        )

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")

        snapshot = store.runtime_snapshot()
        assert snapshot["snapshot_file_exists"] is True
        assert snapshot["backup_fast_path_total"] >= 1
        assert snapshot["last_backup_mode"] == "runtime_base_snapshot"
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_backup_to_disk_skips_when_persisted_generation_is_current(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", "0")
    webspace_id = _webspace_id("backup-skip")
    store = get_ystore_for_webspace(webspace_id)
    try:
        for idx in range(3):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"item_{idx}", idx)

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")

        monkeypatch.setattr(
            ystore_module,
            "_encode_snapshot_artifacts",
            lambda updates: (_ for _ in ()).throw(AssertionError("encode should not run when persisted generation is current")),
        )
        monkeypatch.setattr(
            ystore_module,
            "_persist_snapshot",
            lambda path, snapshot: (_ for _ in ()).throw(AssertionError("persist should not run when snapshot is already current")),
        )

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")

        snapshot = store.runtime_snapshot()
        assert snapshot["backup_skipped_total"] >= 1
        assert snapshot["persisted_up_to_date"] is True
        assert snapshot["last_backup_mode"] == "runtime_base_snapshot:skipped"
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_auto_backup_after_pressure_compaction(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_MAX_UPDATES", "128")
    monkeypatch.setenv("ADAOS_YSTORE_REPLAY_WINDOW", "32")
    monkeypatch.setenv("ADAOS_YSTORE_MAX_REPLAY_BYTES", "700")
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", "1")
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_COOLDOWN_SEC", "0")
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_DEBOUNCE_SEC", "0.05")
    webspace_id = _webspace_id("auto-backup")
    store = get_ystore_for_webspace(webspace_id)
    value = "y" * 256
    try:
        for idx in range(6):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"key_{idx}", value)

        for _ in range(20):
            snapshot = store.runtime_snapshot()
            if int(snapshot.get("auto_backup_total") or 0) >= 1:
                break
            await asyncio.sleep(0.05)

        snapshot = store.runtime_snapshot()
        assert snapshot["auto_backup_total"] >= 1
        assert snapshot["last_auto_backup_reason"] == "byte_limit"
        assert snapshot["snapshot_file_exists"] is True
        assert snapshot["base_snapshot_present"] is True
        assert snapshot["update_log_entries"] == 1
        assert snapshot["last_compact_reason"] == "backup_compaction"

        async with async_read_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            for idx in range(6):
                assert data_map.get(f"key_{idx}") == value
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_request_runtime_compaction_collapses_runtime_log(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT", "1")
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_COOLDOWN_SEC", "0")
    monkeypatch.setenv("ADAOS_YSTORE_AUTOBACKUP_DEBOUNCE_SEC", "0.05")
    webspace_id = _webspace_id("idle-compaction")
    store = get_ystore_for_webspace(webspace_id)
    try:
        for idx in range(3):
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    ydoc.get_map("data").set(txn, f"item_{idx}", idx)

        before = store.runtime_snapshot()
        assert before["runtime_compaction_eligible"] is True
        assert before["update_log_entries"] == 3

        scheduled = await store.request_runtime_compaction(reason="room_reset")
        assert scheduled is True

        for _ in range(20):
            snapshot = store.runtime_snapshot()
            if int(snapshot.get("auto_backup_total") or 0) >= 1 and int(snapshot.get("update_log_entries") or 0) == 1:
                break
            await asyncio.sleep(0.05)

        after = store.runtime_snapshot()
        assert after["auto_backup_total"] >= 1
        assert after["last_auto_backup_reason"] == "idle_room_reset"
        assert after["update_log_entries"] == 1
        assert after["base_snapshot_present"] is True
        assert after["runtime_compaction_eligible"] is False
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_evict_ystore_for_webspace_releases_runtime_memory_and_preserves_snapshot() -> None:
    webspace_id = _webspace_id("evict-store")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("data").set(txn, "flag", "persisted")

        before = store.runtime_snapshot()
        assert before["update_log_entries"] == 1

        result = await ystore_module.evict_ystore_for_webspace(
            webspace_id,
            persist_snapshot=True,
            compact_runtime=True,
            backup_kind="test_evict",
        )

        assert result["ystore_found"] is True
        assert result["persisted"] is True
        assert result["released_update_entries"] >= 1
        assert webspace_id not in ystore_module._YSTORE_CACHE

        after = store.runtime_snapshot()
        assert after["update_log_entries"] == 0
        assert after["running"] is False

        restored = get_ystore_for_webspace(webspace_id)
        assert restored is not store
        assert restored.runtime_snapshot()["update_log_entries"] == 0

        async with async_read_ydoc(webspace_id) as ydoc:
            assert ydoc.get_map("data").get("flag") == "persisted"
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_get_ydoc_reuses_cached_state_vector_for_compacted_store(monkeypatch) -> None:
    webspace_id = _webspace_id("cached-state-vector")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("data").set(txn, "flag", True)

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")
        store_snapshot = store.runtime_snapshot()
        assert store_snapshot["update_log_entries"] == 1
        assert store_snapshot["cached_state_vector_bytes"] > 0

        encode_calls = 0
        original_encode_state_vector = ydoc_module.Y.encode_state_vector

        def _counting_encode_state_vector(ydoc):
            nonlocal encode_calls
            encode_calls += 1
            return original_encode_state_vector(ydoc)

        monkeypatch.setattr(ydoc_module.Y, "encode_state_vector", _counting_encode_state_vector)

        async with async_get_ydoc(webspace_id):
            pass

        assert encode_calls == 1
        snapshot = store.runtime_snapshot()
        assert snapshot["state_vector_fast_path_total"] >= 1
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_get_ydoc_builds_state_vector_cache_after_restore(monkeypatch) -> None:
    webspace_id = _webspace_id("restored-state-vector")
    store = get_ystore_for_webspace(webspace_id)
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("data").set(txn, "flag", "restored")

        await store.backup_to_disk(compact_runtime=True, backup_kind="manual")

        ystore_module._YSTORE_CACHE.pop(webspace_id, None)
        restored = get_ystore_for_webspace(webspace_id)
        assert restored.runtime_snapshot()["cached_state_vector_bytes"] == 0

        encode_calls = 0
        original_encode_state_vector = ydoc_module.Y.encode_state_vector

        def _counting_encode_state_vector(ydoc):
            nonlocal encode_calls
            encode_calls += 1
            return original_encode_state_vector(ydoc)

        monkeypatch.setattr(ydoc_module.Y, "encode_state_vector", _counting_encode_state_vector)

        async with async_get_ydoc(webspace_id):
            pass

        assert encode_calls == 2
        snapshot = restored.runtime_snapshot()
        assert snapshot["state_vector_compute_total"] >= 1
        assert snapshot["state_vector_fast_path_total"] >= 1
        assert snapshot["cached_state_vector_bytes"] > 0
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_ystore_encode_state_as_update_keeps_snapshot_path_for_seed_flows() -> None:
    webspace_id = _webspace_id("snapshot-write")
    store = get_ystore_for_webspace(webspace_id)
    ydoc = Y.YDoc()
    with ydoc.begin_transaction() as txn:
        ydoc.get_map("data").set(txn, "seeded", True)

    try:
        await store.start()
        await store.encode_state_as_update(ydoc)

        snapshot = store.runtime_snapshot()
        assert snapshot["snapshot_write_total"] == 1
        assert snapshot["diff_write_total"] == 0
        assert snapshot["update_log_entries"] == 1
        assert snapshot["update_log_bytes"] > 0
    finally:
        store.stop()
        reset_ystore_for_webspace(webspace_id)


def test_get_ydoc_stops_store_after_context() -> None:
    webspace_id = _webspace_id("sync-stop")
    store = get_ystore_for_webspace(webspace_id)
    try:
        with get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                ydoc.get_map("data").set(txn, "sync_flag", True)
            assert store.runtime_snapshot()["running"] is True

        snapshot = store.runtime_snapshot()
        assert snapshot["running"] is False
        assert snapshot["diff_write_total"] == 1
        assert snapshot["update_log_entries"] == 1
    finally:
        reset_ystore_for_webspace(webspace_id)


async def test_async_get_ydoc_prefers_live_room_when_requested(monkeypatch) -> None:
    class _FakeStore:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0
            self.apply_updates_calls = 0
            self.write_updates: list[bytes] = []

        async def start(self) -> None:
            self.start_calls += 1

        def stop(self) -> None:
            self.stop_calls += 1

        async def apply_updates(self, ydoc) -> None:  # noqa: ARG002
            self.apply_updates_calls += 1

        async def current_state_vector(self) -> bytes | None:
            return None

        async def write_update(self, update: bytes, *, update_kind: str = "raw") -> bool:  # noqa: ARG002
            self.write_updates.append(bytes(update or b""))
            return True

    room_doc = Y.YDoc()
    fake_store = _FakeStore()
    room = type(
        "Room",
        (),
        {
            "ydoc": room_doc,
            "ystore": fake_store,
            "_task_group": object(),
            "_thread_id": threading.get_ident(),
            "_loop": asyncio.get_running_loop(),
        },
    )()

    monkeypatch.setattr(ydoc_module, "get_ystore_for_webspace", lambda _webspace_id: fake_store)
    monkeypatch.setattr(ydoc_module, "_resolve_live_room", lambda _webspace_id: room)

    async with async_get_ydoc("live-room-fast-path", prefer_live_room=True) as ydoc:
        assert ydoc is room_doc
        with ydoc.begin_transaction() as txn:
            ydoc.get_map("ui").set(txn, "current_scenario", "infrascope")

    assert fake_store.start_calls == 0
    assert fake_store.stop_calls == 0
    assert fake_store.apply_updates_calls == 0
    assert len(fake_store.write_updates) == 0
    assert room_doc.get_map("ui").get("current_scenario") == "infrascope"


async def test_async_read_ydoc_prefers_live_room_without_store_replay(monkeypatch) -> None:
    class _FakeStore:
        async def start(self) -> None:
            raise AssertionError("ystore.start should not run on async_read_ydoc live-room path")

        def stop(self) -> None:
            raise AssertionError("ystore.stop should not run on async_read_ydoc live-room path")

        async def apply_updates(self, ydoc) -> None:  # noqa: ARG002
            raise AssertionError("ystore.apply_updates should not run on async_read_ydoc live-room path")

    room_doc = Y.YDoc()
    with room_doc.begin_transaction() as txn:
        room_doc.get_map("data").set(txn, "flag", True)
    room = type(
        "Room",
        (),
        {
            "ydoc": room_doc,
            "ystore": object(),
            "_task_group": object(),
            "_thread_id": threading.get_ident(),
            "_loop": asyncio.get_running_loop(),
        },
    )()

    monkeypatch.setattr(ydoc_module, "get_ystore_for_webspace", lambda _webspace_id: _FakeStore())
    monkeypatch.setattr(ydoc_module, "_resolve_live_room", lambda _webspace_id: room)

    async with async_read_ydoc("live-room-read-only") as ydoc:
        assert ydoc is room_doc
        assert ydoc.get_map("data").get("flag") is True
