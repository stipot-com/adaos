from __future__ import annotations

import asyncio
import logging

import y_py as Y

from .seed import SEED
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.manager import ScenarioManager
from .y_store import AdaosMemoryYStore, get_ystore_for_webspace

_log = logging.getLogger("adaos.yjs.bootstrap")


def _scenario_manager() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


async def ensure_webspace_seeded_from_scenario(
    ystore: AdaosMemoryYStore, webspace_id: str, default_scenario_id: str = "web_desktop"
) -> None:
    """
    If the YDoc has no ui.application yet, try to seed it from a scenario
    package (.adaos/workspace/scenarios/<id>/scenario.json). If not found or
    invalid, fall back to the static SEED.
    """
    _log.debug("ensure_webspace_seeded_from_scenario start webspace=%s scenario=%s", webspace_id, default_scenario_id)

    try:
        await ystore.start()
    except Exception as exc:
        _log.warning("ystore.start() failed for webspace=%s: %s", webspace_id, exc, exc_info=True)
        return

    ydoc = Y.YDoc()
    try:
        await ystore.apply_updates(ydoc)
    except Exception as exc:
        _log.warning("apply_updates failed for webspace=%s (treating as empty): %s", webspace_id, exc, exc_info=True)

    ui_map = ydoc.get_map("ui")
    data_map = ydoc.get_map("data")

    if ui_map.get("application") is not None or len(ui_map) or len(data_map):
        _log.debug(
            "webspace %s already seeded (ui keys=%s, data keys=%s)",
            webspace_id,
            list(ui_map.keys()),
            list(data_map.keys()),
        )
        return

    try:
        mgr = _scenario_manager()
        _log.info("seeding webspace %s from scenario %s", webspace_id, default_scenario_id)
        await mgr.sync_to_yjs_async(default_scenario_id, webspace_id)
        return
    except Exception as exc:
        _log.warning(
            "scenario-based seed failed for webspace=%s scenario=%s, falling back to SEED: %s",
            webspace_id,
            default_scenario_id,
            exc,
            exc_info=True,
        )

    with ydoc.begin_transaction() as txn:
        ui = ydoc.get_map("ui")
        data = ydoc.get_map("data")

        ui.set(txn, "application", SEED["ui"]["application"])
        data.set(txn, "catalog", SEED["data"]["catalog"])
        data.set(txn, "installed", SEED["data"]["installed"])
        data.set(txn, "weather", SEED["data"]["weather"])

    try:
        await ystore.encode_state_as_update(ydoc)
        _log.info(
            "webspace %s seeded via SEED (ui keys=%s, data keys=%s)",
            webspace_id,
            list(ui_map.keys()),
            list(data_map.keys()),
        )
    except Exception as exc:
        _log.warning("encode_state_as_update failed for webspace=%s: %s", webspace_id, exc, exc_info=True)


async def bootstrap_seed_if_empty(ystore: AdaosMemoryYStore) -> None:
    await ensure_webspace_seeded_from_scenario(get_ystore_for_webspace("default"), webspace_id="default")
