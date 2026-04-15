from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

import y_py as Y

from adaos.services.yjs.seed import SEED
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.yjs.store import AdaosMemoryYStore, get_ystore_for_webspace

_log = logging.getLogger("adaos.yjs.bootstrap")


def _scenario_manager() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clone_json_like(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return value


def _seed_registry_payload() -> dict[str, Any]:
    application = _coerce_dict(_coerce_dict(SEED.get("ui") or {}).get("application") or {})
    return _coerce_dict(application.get("registry") or {})


def _seed_catalog_payload() -> dict[str, Any]:
    return _coerce_dict(_coerce_dict(SEED.get("data") or {}).get("catalog") or {})


def _seed_application_payload() -> dict[str, Any]:
    return _coerce_dict(_coerce_dict(SEED.get("ui") or {}).get("application") or {})


def _resolve_requested_scenario(ui_map: Any, default_scenario_id: str) -> str:
    current = str(ui_map.get("current_scenario") or "").strip()
    if current:
        return current
    requested = str(default_scenario_id or "").strip()
    return requested or "web_desktop"


def _has_projected_scenario_seed(ui_map: Any, data_map: Any, scenario_id: str) -> bool:
    if not str(scenario_id or "").strip():
        return False
    ui_scenarios = _coerce_dict(ui_map.get("scenarios") or {})
    scenario_ui = _coerce_dict(ui_scenarios.get(scenario_id) or {})
    application = _coerce_dict(scenario_ui.get("application") or {})
    if not application:
        return False
    data_scenarios = _coerce_dict(data_map.get("scenarios") or {})
    scenario_data = _coerce_dict(data_scenarios.get(scenario_id) or {})
    catalog = _coerce_dict(scenario_data.get("catalog") or {})
    return bool(catalog or "catalog" in scenario_data)


def _emit_bootstrap_rebuild_nudge(webspace_id: str, scenario_id: str) -> None:
    ctx = get_ctx()
    emit(
        ctx.bus,
        "scenarios.synced",
        {
            "scenario_id": str(scenario_id or "").strip() or "web_desktop",
            "webspace_id": str(webspace_id or "").strip() or default_webspace_id(),
        },
        "yjs.bootstrap",
    )


def _project_seed_payload_to_compat_branches(ydoc: Y.YDoc, *, scenario_id: str) -> None:
    application = _clone_json_like(_seed_application_payload())
    registry_payload = _clone_json_like(_seed_registry_payload())
    catalog_payload = _clone_json_like(_seed_catalog_payload())

    ui_map = ydoc.get_map("ui")
    registry_map = ydoc.get_map("registry")
    data_map = ydoc.get_map("data")

    with ydoc.begin_transaction() as txn:
        ui_scenarios = _coerce_dict(ui_map.get("scenarios") or {})
        scenario_ui = _coerce_dict(ui_scenarios.get(scenario_id) or {})
        scenario_ui["application"] = application
        updated_ui = dict(ui_scenarios)
        updated_ui[scenario_id] = scenario_ui
        ui_map.set(txn, "scenarios", updated_ui)
        ui_map.set(txn, "current_scenario", scenario_id)

        registry_scenarios = _coerce_dict(registry_map.get("scenarios") or {})
        updated_registry = dict(registry_scenarios)
        updated_registry[scenario_id] = registry_payload
        registry_map.set(txn, "scenarios", updated_registry)

        data_scenarios = _coerce_dict(data_map.get("scenarios") or {})
        updated_data = dict(data_scenarios)
        scenario_data = _coerce_dict(updated_data.get(scenario_id) or {})
        scenario_data["catalog"] = catalog_payload
        updated_data[scenario_id] = scenario_data
        data_map.set(txn, "scenarios", updated_data)


async def ensure_webspace_seeded_from_scenario(
    ystore: AdaosMemoryYStore,
    webspace_id: str,
    default_scenario_id: str = "web_desktop",
    *,
    space: str = "workspace",
    emit_event: bool = True,
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
    requested_scenario_id = _resolve_requested_scenario(ui_map, default_scenario_id)
    if _is_seeded_state(application, data_map.get("catalog")):
        _log.debug(
            "webspace %s already seeded (ui keys=%s, data keys=%s)",
            webspace_id,
            list(ui_map.keys()),
            list(data_map.keys()),
        )
        return

    if _has_projected_scenario_seed(ui_map, data_map, requested_scenario_id):
        _log.info(
            "webspace %s has projected scenario seed for %s; nudging semantic rebuild",
            webspace_id,
            requested_scenario_id,
        )
        if emit_event:
            _emit_bootstrap_rebuild_nudge(webspace_id, requested_scenario_id)
        return

    try:
        mgr = _scenario_manager()
        _log.info("seeding webspace %s from scenario %s (space=%s)", webspace_id, requested_scenario_id, space)
        await mgr.sync_to_yjs_async(
            requested_scenario_id,
            webspace_id,
            space=space,
            emit_event=emit_event,
        )
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

    fallback_scenario_id = str(default_scenario_id or "").strip() or "web_desktop"
    _project_seed_payload_to_compat_branches(ydoc, scenario_id=fallback_scenario_id)

    try:
        await ystore.encode_state_as_update(ydoc)
        if emit_event:
            _emit_bootstrap_rebuild_nudge(webspace_id, fallback_scenario_id)
        _log.info(
            "webspace %s seeded via compatibility fallback for scenario %s (ui keys=%s, data keys=%s)",
            webspace_id,
            fallback_scenario_id,
            list(ui_map.keys()),
            list(data_map.keys()),
        )
    except Exception as exc:
        _log.warning("encode_state_as_update failed for webspace=%s: %s", webspace_id, exc, exc_info=True)


async def bootstrap_seed_if_empty(ystore: AdaosMemoryYStore) -> None:
    await ensure_webspace_seeded_from_scenario(get_ystore_for_webspace("default"), webspace_id="default")
