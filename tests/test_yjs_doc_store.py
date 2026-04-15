from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
import y_py as Y

from adaos.services.yjs import doc as ydoc_module
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
    assert len(fake_store.write_updates) == 1
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
            "_thread_id": threading.get_ident(),
            "_loop": asyncio.get_running_loop(),
        },
    )()

    monkeypatch.setattr(ydoc_module, "get_ystore_for_webspace", lambda _webspace_id: _FakeStore())
    monkeypatch.setattr(ydoc_module, "_resolve_live_room", lambda _webspace_id: room)

    async with async_read_ydoc("live-room-read-only") as ydoc:
        assert ydoc is room_doc
        assert ydoc.get_map("data").get("flag") is True
