from __future__ import annotations

from uuid import uuid4

import pytest
import y_py as Y

from adaos.services.yjs.doc import async_get_ydoc, get_ydoc
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
