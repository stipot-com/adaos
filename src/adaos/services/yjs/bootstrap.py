from __future__ import annotations

import asyncio
import logging

import y_py as Y

from adaos.services.yjs.seed import SEED
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.yjs.store import AdaosMemoryYStore, get_ystore_for_webspace

_log = logging.getLogger("adaos.yjs.bootstrap")


def _scenario_manager() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


async def ensure_webspace_seeded_from_scenario(
    ystore: AdaosMemoryYStore,
    webspace_id: str,
    default_scenario_id: str = "web_desktop",
    *,
    space: str = "workspace",
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
    except BaseException as exc:  # catch PanicException and similar
        _log.warning(
            "apply_updates failed for webspace=%s (treating as empty, exc=%r, type=%s)",
            webspace_id,
            exc,
            type(exc).__name__,
            exc_info=True,
        )

    ui_map = ydoc.get_map("ui")
    data_map = ydoc.get_map("data")

    def _is_seeded_state(app: object, catalog: object) -> bool:
        if not isinstance(app, dict) or not app:
            return False
        modals = app.get("modals")
        if not isinstance(modals, dict) or not modals:
            return False
        # Desktop relies on these catalogs; if they are missing the UI becomes
        # "empty" after reload even though ui.application is a non-empty dict.
        if "apps_catalog" not in modals or "widgets_catalog" not in modals:
            return False
        if not isinstance(catalog, dict):
            return False
        apps = catalog.get("apps")
        widgets = catalog.get("widgets")
        if not isinstance(apps, list) or not isinstance(widgets, list):
            return False
        return True

    application = ui_map.get("application")
    if _is_seeded_state(application, data_map.get("catalog")):
        _log.debug(
            "webspace %s already seeded (ui keys=%s, data keys=%s)",
            webspace_id,
            list(ui_map.keys()),
            list(data_map.keys()),
        )
        return

    try:
        mgr = _scenario_manager()
        _log.info("seeding webspace %s from scenario %s (space=%s)", webspace_id, default_scenario_id, space)
        await mgr.sync_to_yjs_async(default_scenario_id, webspace_id, space=space)
        return
    except Exception as exc:
        _log.warning(
            "scenario-based seed failed for webspace=%s scenario=%s: %s",
            webspace_id,
            default_scenario_id,
            exc,
            exc_info=True,
        )

    if webspace_id != default_webspace_id():
        return

    with ydoc.begin_transaction() as txn:
        ui = ydoc.get_map("ui")
        data = ydoc.get_map("data")

        ui.set(txn, "application", SEED["ui"]["application"])
        data.set(txn, "catalog", SEED["data"]["catalog"])
        data.set(txn, "installed", SEED["data"]["installed"])
        try:
            weather = (SEED.get("data") or {}).get("weather")
            if weather is not None:
                data.set(txn, "weather", weather)
        except Exception:
            pass

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
