from __future__ import annotations

import y_py as Y
from ypy_websocket.ystore import SQLiteYStore

from .seed import SEED


async def bootstrap_seed_if_empty(ystore: SQLiteYStore) -> None:
    """
    If the given Y store is effectively empty, bootstrap it with the
    initial SEED document (ui + data).
    """
    # Ensure the underlying SQLite DB is initialised so read/write work.
    try:
        await ystore.start()
    except Exception:
        # If start fails, leave early; the room can still operate in-memory.
        return

    ydoc = Y.YDoc()
    try:
        await ystore.apply_updates(ydoc)
    except Exception:
        # Treat any read error as "no state yet".
        pass

    ui_map = ydoc.get_map("ui")
    data_map = ydoc.get_map("data")
    if len(ui_map) or len(data_map):
        # Already seeded with some state.
        return

    with ydoc.begin_transaction() as txn:
        ui = ydoc.get_map("ui")
        data = ydoc.get_map("data")

        ui.set(txn, "application", SEED["ui"]["application"])
        data.set(txn, "catalog", SEED["data"]["catalog"])
        data.set(txn, "installed", SEED["data"]["installed"])
        data.set(txn, "weather", SEED["data"]["weather"])

    # Persist a full snapshot into the store.
    await ystore.encode_state_as_update(ydoc)
