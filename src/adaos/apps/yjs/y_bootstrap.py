from __future__ import annotations

import asyncio

import y_py as Y
from ypy_websocket.ystore import SQLiteYStore

from .seed import SEED
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.manager import ScenarioManager


def _scenario_manager() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


async def ensure_webspace_seeded_from_scenario(
    ystore: SQLiteYStore, webspace_id: str, default_scenario_id: str = "web_desktop"
) -> None:
    """
    If the YDoc has no ui.application yet, try to seed it from a scenario
    package (.adaos/workspace/scenarios/<id>/scenario.json). If not found or
    invalid, fall back to the static SEED.

    Writes (when seeding from scenario):
      - ui.scenarios.<id>.application
      - registry.scenarios.<id>
      - data.scenarios.<id>.catalog
      - ui.current_scenario (if missing)
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

    # If ui.application already exists, assume webspace is seeded.
    if ui_map.get("application") is not None or len(ui_map) or len(data_map):
        return

    # 1) Try projecting scenario declaration via ScenarioManager so it also
    # emits scenarios.synced for downstream listeners.
    try:
        mgr = _scenario_manager()
        await mgr.sync_to_yjs_async(default_scenario_id, webspace_id)
        return
    except Exception:
        pass

    # 2) Fallback: SEED as in Stage A1.
    with ydoc.begin_transaction() as txn:
        ui = ydoc.get_map("ui")
        data = ydoc.get_map("data")

        ui.set(txn, "application", SEED["ui"]["application"])
        data.set(txn, "catalog", SEED["data"]["catalog"])
        data.set(txn, "installed", SEED["data"]["installed"])
        data.set(txn, "weather", SEED["data"]["weather"])

    try:
        await ystore.encode_state_as_update(ydoc)
    except Exception:
        pass


async def bootstrap_seed_if_empty(ystore: SQLiteYStore) -> None:
    """
    Backwards-compatible wrapper: seed the default webspace using the default scenario
    (web_desktop) or SEED if scenario content is not available.
    """
    await ensure_webspace_seeded_from_scenario(ystore, webspace_id="default")
