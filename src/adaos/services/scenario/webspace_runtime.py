from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from collections import OrderedDict
from collections.abc import Iterable
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time

import y_py as Y

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.capacity import get_local_capacity
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspaces import index as workspace_index
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.seed import SEED
from adaos.services.eventbus import emit
from adaos.sdk.core.decorators import subscribe
from .workflow_runtime import ScenarioWorkflowRuntime

_log = logging.getLogger("adaos.scenario.webspace_runtime")
_WS_ID_RE = re.compile(r"[^a-zA-Z0-9-_]+")
_SCENARIO_SWITCH_REBUILD_TASKS: dict[str, asyncio.Task[Any]] = {}
_WEBSPACE_REBUILD_STATUS: dict[str, Dict[str, Any]] = {}
_WEBUI_DECL_CACHE: dict[str, tuple[tuple[str, int, int], Dict[str, Any]]] = {}
_RESOLVED_WEBSPACE_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_RESOLVED_WEBSPACE_CACHE_LIMIT = 64
_WEBUI_LOAD_PHASES = frozenset({"eager", "visible", "interaction", "deferred"})
_WEBUI_LOAD_FOCUS = frozenset({"primary", "supporting", "off_focus", "background"})
_WEBUI_READINESS_STATES = frozenset({"pending_structure", "first_paint", "interactive", "hydrating", "ready", "degraded"})
_DEFERRED_OFF_FOCUS_LOAD = {
    "structure": "interaction",
    "data": "deferred",
    "focus": "off_focus",
    "offFocusReadyState": "hydrating",
}


@dataclass(slots=True)
class WebUIRegistryEntry:
    """
    Effective UI model snapshot for a single webspace after merging:

      - scenario-projected catalog/registry,
      - skill contributions from webui.json,
      - auto-installed items and current desktop overlay state.
    """

    scenario_id: str
    apps: List[Dict[str, Any]] = field(default_factory=list)
    widgets: List[Dict[str, Any]] = field(default_factory=list)
    registry_modals: List[str] = field(default_factory=list)
    registry_widgets: List[str] = field(default_factory=list)
    installed: Dict[str, List[str]] = field(default_factory=lambda: {"apps": [], "widgets": []})


@dataclass(slots=True)
class WebspaceInfo:
    """
    Lightweight snapshot of a webspace entry used by higher-level services
    and SDK helpers. ``is_dev`` is derived from the display name and can be
    used to filter workspace vs dev spaces.
    """

    id: str
    title: str
    created_at: int
    kind: str = "workspace"
    home_scenario: str = "web_desktop"
    source_mode: str = "workspace"
    is_dev: bool = False


@dataclass(slots=True)
class WebspaceOperationalState:
    """
    Lightweight operational view of a webspace that combines persistent
    manifest metadata with the current live scenario selection from Yjs.

    ``stored_home_scenario`` preserves whether the manifest explicitly stores
    a home scenario. This matters for legacy spaces, where reload/reset should
    still be able to fall back to ``ui.current_scenario`` instead of forcing
    ``web_desktop`` semantics too early.
    """

    webspace_id: str
    title: str
    kind: str
    source_mode: str
    is_dev: bool
    stored_home_scenario: str | None
    effective_home_scenario: str
    current_scenario: str | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "webspace_id": self.webspace_id,
            "title": self.title,
            "kind": self.kind,
            "source_mode": self.source_mode,
            "is_dev": self.is_dev,
            "stored_home_scenario": self.stored_home_scenario,
            "home_scenario": self.effective_home_scenario,
            "current_scenario": self.current_scenario,
            "current_matches_home": bool(self.current_scenario) and self.current_scenario == self.effective_home_scenario,
        }


@dataclass(slots=True)
class WebspaceResolverInputs:
    """
    Explicit resolver inputs for the current light-weight Phase 3 contract.

    `overlay_snapshot` is sourced from persistent webspace metadata and
    represents canonical desktop customization state for the current MVP
    Phase 5 boundary.
    """

    webspace_id: str
    scenario_id: str
    source_mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    scenario_application: Dict[str, Any] = field(default_factory=dict)
    scenario_catalog: Dict[str, Any] = field(default_factory=dict)
    scenario_registry: Dict[str, Any] = field(default_factory=dict)
    overlay_snapshot: Dict[str, Any] = field(default_factory=dict)
    live_state: Dict[str, Any] = field(default_factory=dict)
    skill_decls: List[Dict[str, Any]] = field(default_factory=list)
    desktop_scenarios: List[Tuple[str, str]] = field(default_factory=list)
    scenario_source: str = "legacy_yjs"
    legacy_scenario_fallback: bool = False


@dataclass(slots=True)
class WebspaceResolverOutputs:
    """
    Materialized effective UI state computed from resolver inputs.

    These values are still written to the existing Yjs compatibility paths,
    but the merge result itself is now an explicit architectural layer.
    """

    webspace_id: str
    scenario_id: str
    source_mode: str
    application: Dict[str, Any] = field(default_factory=dict)
    catalog: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: {"apps": [], "widgets": []})
    registry: Dict[str, List[str]] = field(default_factory=lambda: {"modals": [], "widgets": []})
    installed: Dict[str, List[str]] = field(default_factory=lambda: {"apps": [], "widgets": []})
    desktop: Dict[str, Any] = field(default_factory=dict)
    routing: Dict[str, Any] = field(default_factory=dict)
    skill_decls: List[Dict[str, Any]] = field(default_factory=list)

    def to_registry_entry(self) -> "WebUIRegistryEntry":
        return WebUIRegistryEntry(
            scenario_id=self.scenario_id,
            apps=[dict(it) for it in (self.catalog.get("apps") or []) if isinstance(it, Mapping)],
            widgets=[dict(it) for it in (self.catalog.get("widgets") or []) if isinstance(it, Mapping)],
            registry_modals=list(self.registry.get("modals") or []),
            registry_widgets=list(self.registry.get("widgets") or []),
            installed={
                "apps": list(self.installed.get("apps") or []),
                "widgets": list(self.installed.get("widgets") or []),
            },
        )


def _mark_entry(entry: Dict[str, Any], *, source: str, dev: bool) -> Dict[str, Any]:
    """
    Attach provenance / dev flag to a catalog entry without overwriting its
    semantic "source" (which may already contain a YDoc path like "y:data/...").
    """
    data = dict(entry)
    # Always keep provenance separate from semantic `source` paths used by
    # widget renderers (e.g. metric tiles reading y:data/...).
    data["origin"] = source
    data["dev"] = dev
    return data


def _merge_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        merged.append(item)
    return merged


def _merge_registry_lists(base: List[str], extras: List[List[str]]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for value in base:
        token = str(value)
        if token and token not in seen:
            seen.add(token)
            merged.append(token)
    for contrib in extras:
        for token in contrib:
            token = str(token)
            if token and token not in seen:
                seen.add(token)
                merged.append(token)
    return merged


def _filter_installed(installed: Dict[str, List[str]], apps: List[Dict[str, Any]], widgets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    app_ids = {str(item.get("id")) for item in apps if item.get("id")}
    widget_ids = {str(item.get("id")) for item in widgets if item.get("id")}
    current_apps = [a for a in (installed.get("apps") or []) if a in app_ids]
    current_widgets = [w for w in (installed.get("widgets") or []) if w in widget_ids]
    return {"apps": current_apps, "widgets": current_widgets}

def _dedupe_str_list(values: Any) -> List[str]:
    # YJS may return YArray-like values which are iterable but not `list`.
    if isinstance(values, (str, bytes, bytearray)) or isinstance(values, Mapping):
        return []
    if not isinstance(values, Iterable):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        token = v.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """
    Best-effort conversion of YJS map-like values to a plain dict.

    y_py map objects are not guaranteed to implement `collections.abc.Mapping`
    but they often expose `.items()`. Using `isinstance(..., Mapping)` only
    can silently drop persisted state (e.g. installed apps) during scenario
    switches.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:
            return {}
    return {}


def _normalize_webui_load_hint(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, str] = {}
    structure = str(value.get("structure") or "").strip()
    if structure in _WEBUI_LOAD_PHASES:
        out["structure"] = structure
    data = str(value.get("data") or "").strip()
    if data in _WEBUI_LOAD_PHASES:
        out["data"] = data
    focus = str(value.get("focus") or "").strip()
    if focus in _WEBUI_LOAD_FOCUS:
        out["focus"] = focus
    off_focus_ready = str(value.get("offFocusReadyState") or "").strip()
    if off_focus_ready in _WEBUI_READINESS_STATES:
        out["offFocusReadyState"] = off_focus_ready
    return out


def _apply_webui_load_hint(node: Any) -> Dict[str, Any]:
    item = _coerce_dict(node)
    if not item:
        return {}
    load = _normalize_webui_load_hint(item.get("load"))
    if load:
        item["load"] = load
    else:
        item.pop("load", None)
    return item


def _normalize_webui_widget_config(node: Any) -> Dict[str, Any]:
    return _apply_webui_load_hint(node)


def _normalize_webui_page_schema(node: Any) -> Dict[str, Any]:
    page = _apply_webui_load_hint(node)
    if not page:
        return {}
    widgets = page.get("widgets")
    if isinstance(widgets, list):
        page["widgets"] = [_normalize_webui_widget_config(widget) for widget in widgets if isinstance(widget, Mapping)]
    return page


def _normalize_webui_modal_def(node: Any) -> Dict[str, Any]:
    modal = _apply_webui_load_hint(node)
    if not modal:
        return {}
    schema = modal.get("schema")
    if isinstance(schema, Mapping):
        modal["schema"] = _normalize_webui_page_schema(schema)
    return modal


def _clone_json_like(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if value is None:
            return None
        if isinstance(value, dict):
            return {str(k): _clone_json_like(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clone_json_like(v) for v in value]
        if isinstance(value, tuple):
            return [_clone_json_like(v) for v in value]
        if isinstance(value, Mapping):
            return {str(k): _clone_json_like(v) for k, v in value.items()}
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(k): _clone_json_like(v) for k, v in items()}
            except Exception:
                return value
        return value


def _fingerprint_json_like(value: Any) -> str:
    try:
        normalized = json.dumps(
            _clone_json_like(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except Exception:
        normalized = repr(value)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _resolver_cache_keys(inputs: WebspaceResolverInputs) -> Dict[str, str]:
    scenario_snapshot = {
        "scenario_id": inputs.scenario_id,
        "source_mode": inputs.source_mode,
        "scenario_source": inputs.scenario_source,
        "legacy_scenario_fallback": inputs.legacy_scenario_fallback,
        "scenario_application": inputs.scenario_application,
        "scenario_catalog": inputs.scenario_catalog,
        "scenario_registry": inputs.scenario_registry,
    }
    return {
        "scenario": _fingerprint_json_like(scenario_snapshot),
        "skills": _fingerprint_json_like(inputs.skill_decls),
        "overlay": _fingerprint_json_like(inputs.overlay_snapshot),
        "live": _fingerprint_json_like(inputs.live_state),
        "desktop_scenarios": _fingerprint_json_like(inputs.desktop_scenarios),
    }


def _resolver_input_fingerprint(inputs: WebspaceResolverInputs, *, cache_keys: Mapping[str, Any]) -> str:
    snapshot = {
        "webspace_id": inputs.webspace_id,
        "scenario_id": inputs.scenario_id,
        "source_mode": inputs.source_mode,
        "scenario_source": inputs.scenario_source,
        "legacy_scenario_fallback": inputs.legacy_scenario_fallback,
        "metadata": inputs.metadata,
        "cache_keys": dict(cache_keys),
    }
    return _fingerprint_json_like(snapshot)


def _resolved_outputs_to_cache_payload(resolved: WebspaceResolverOutputs) -> Dict[str, Any]:
    return {
        "webspace_id": str(resolved.webspace_id or ""),
        "scenario_id": str(resolved.scenario_id or ""),
        "source_mode": str(resolved.source_mode or ""),
        "application": _clone_json_like(resolved.application),
        "catalog": _clone_json_like(resolved.catalog),
        "registry": _clone_json_like(resolved.registry),
        "installed": _clone_json_like(resolved.installed),
        "desktop": _clone_json_like(resolved.desktop),
        "routing": _clone_json_like(resolved.routing),
        "skill_decls": _clone_json_like(resolved.skill_decls),
    }


def _resolved_outputs_from_cache_payload(payload: Mapping[str, Any]) -> WebspaceResolverOutputs:
    return WebspaceResolverOutputs(
        webspace_id=str(payload.get("webspace_id") or ""),
        scenario_id=str(payload.get("scenario_id") or ""),
        source_mode=str(payload.get("source_mode") or ""),
        application=_coerce_dict(payload.get("application") or {}),
        catalog=_coerce_dict(payload.get("catalog") or {}),
        registry=_coerce_dict(payload.get("registry") or {}),
        installed=_coerce_dict(payload.get("installed") or {}),
        desktop=_coerce_dict(payload.get("desktop") or {}),
        routing=_coerce_dict(payload.get("routing") or {}),
        skill_decls=[
            dict(item)
            for item in (payload.get("skill_decls") or [])
            if isinstance(item, Mapping)
        ],
    )


def _get_cached_resolved_outputs(fingerprint: str) -> WebspaceResolverOutputs | None:
    token = str(fingerprint or "").strip()
    if not token:
        return None
    cached = _RESOLVED_WEBSPACE_CACHE.get(token)
    if not isinstance(cached, Mapping):
        return None
    _RESOLVED_WEBSPACE_CACHE.move_to_end(token)
    return _resolved_outputs_from_cache_payload(cached)


def _remember_resolved_outputs(fingerprint: str, resolved: WebspaceResolverOutputs) -> None:
    token = str(fingerprint or "").strip()
    if not token:
        return
    _RESOLVED_WEBSPACE_CACHE[token] = _resolved_outputs_to_cache_payload(resolved)
    _RESOLVED_WEBSPACE_CACHE.move_to_end(token)
    while len(_RESOLVED_WEBSPACE_CACHE) > _RESOLVED_WEBSPACE_CACHE_LIMIT:
        _RESOLVED_WEBSPACE_CACHE.popitem(last=False)


def _set_map_value_if_changed(y_map: Any, txn: Any, key: str, value: Any) -> bool:
    next_value = _clone_json_like(value)
    try:
        current = y_map.get(key)
    except Exception:
        current = None
    if _clone_json_like(current) == next_value:
        return False
    y_map.set(txn, key, next_value)
    return True


def _merge_installed_with_auto(installed: Dict[str, Any], *, auto_apps: set[str], auto_widgets: set[str]) -> Dict[str, List[str]]:
    """
    Merge existing installed apps/widgets with auto-installed ids while
    preserving user choices across scenario switches.

    Important: we do NOT drop ids that are not present in the current catalog,
    because switching scenarios would otherwise lose installed apps/widgets
    that become available again when returning to the previous scenario.
    """
    apps = _dedupe_str_list(installed.get("apps"))
    widgets = _dedupe_str_list(installed.get("widgets"))

    for app_id in sorted(auto_apps):
        if app_id not in apps:
            apps.append(app_id)
    for widget_id in sorted(auto_widgets):
        if widget_id not in widgets:
            widgets.append(widget_id)

    return {"apps": apps, "widgets": widgets}


def _normalize_overlay_widget_entries(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        item["id"] = item_id
        if item.get("type") is not None:
            item["type"] = str(item.get("type"))
        out.append(item)
    return out


def _built_in_scenario_content(scenario_id: str) -> Dict[str, Any]:
    if str(scenario_id or "").strip() != "web_desktop":
        return {}
    try:
        app = json.loads(json.dumps(((SEED.get("ui") or {}).get("application") or {})))
        data = json.loads(json.dumps((SEED.get("data") or {})))
    except Exception:
        return {}
    catalog = data.get("catalog") if isinstance(data, dict) else {}
    if not isinstance(catalog, dict):
        catalog = {}
    return {
        "id": "web_desktop",
        "ui": {"application": app if isinstance(app, dict) else {}},
        "registry": {},
        "catalog": catalog,
        "data": data if isinstance(data, dict) else {},
    }


def _load_scenario_switch_content(scenario_id: str, *, space: str) -> Dict[str, Any]:
    content = scenarios_loader.read_content(scenario_id, space=space)
    if isinstance(content, dict) and content:
        return content
    fallback = _built_in_scenario_content(scenario_id)
    if fallback:
        _log.info("desktop.scenario.set: using built-in fallback content for scenario=%s", scenario_id)
        return fallback
    return {}


def _scenario_exists_for_switch(scenario_id: str, *, space: str) -> bool:
    if _built_in_scenario_content(scenario_id):
        return True
    try:
        return bool(scenarios_loader.scenario_exists(scenario_id, space=space))
    except Exception:
        return False


def _scenario_loader_space(source_mode: str) -> str:
    return "dev" if str(source_mode or "").strip().lower() == "dev" else "workspace"


def _pointer_first_scenario_switch_enabled() -> bool:
    raw = os.getenv("ADAOS_WEBSPACE_POINTER_SCENARIO_SWITCH")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _extract_scenario_sections_from_content(content: Mapping[str, Any] | None) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    payload = _coerce_dict(content or {})
    ui_section = _coerce_dict(_coerce_dict(payload.get("ui") or {}).get("application") or {})
    registry_section = _coerce_dict(payload.get("registry") or {})
    catalog_section = _coerce_dict(payload.get("catalog") or {})
    return ui_section, catalog_section, registry_section


def _read_legacy_materialized_scenario_sections(
    ui_map: Any,
    data_map: Any,
    registry_map: Any,
    scenario_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    scenarios_ui = _coerce_dict(ui_map.get("scenarios") or {})
    scenario_ui_entry = _coerce_dict(scenarios_ui.get(scenario_id) or {})
    scenario_app_ui = _coerce_dict(scenario_ui_entry.get("application") or {})

    scenarios_data = _coerce_dict(data_map.get("scenarios") or {})
    scenario_entry = _coerce_dict(scenarios_data.get(scenario_id) or {})
    base_catalog = _coerce_dict(scenario_entry.get("catalog") or {})

    scenario_registry_map = _coerce_dict(registry_map.get("scenarios") or {})
    registry_entry = _coerce_dict(scenario_registry_map.get(scenario_id) or {})
    return scenario_app_ui, base_catalog, registry_entry


def _resolve_scenario_sections_in_doc(
    ydoc: Any,
    *,
    webspace_id: str,
    scenario_id: str,
    source_mode: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str, bool]:
    ui_map = ydoc.get_map("ui")
    data_map = ydoc.get_map("data")
    registry_map = ydoc.get_map("registry")

    loader_space = _scenario_loader_space(source_mode)
    content = _load_scenario_switch_content(scenario_id, space=loader_space)
    if isinstance(content, Mapping) and content:
        ui_section, catalog_section, registry_section = _extract_scenario_sections_from_content(content)
        return ui_section, catalog_section, registry_section, f"loader:{loader_space}", False

    scenario_app_ui, base_catalog, registry_entry = _read_legacy_materialized_scenario_sections(
        ui_map,
        data_map,
        registry_map,
        scenario_id,
    )
    if scenario_app_ui or base_catalog or registry_entry:
        _log.info(
            "resolver using legacy materialized scenario payload webspace=%s scenario=%s source_mode=%s",
            webspace_id,
            scenario_id,
            source_mode,
        )
        return scenario_app_ui, base_catalog, registry_entry, "legacy_yjs", True

    _log.warning(
        "resolver found no canonical or legacy scenario payload webspace=%s scenario=%s source_mode=%s",
        webspace_id,
        scenario_id,
        source_mode,
    )
    return {}, {}, {}, "missing", True


def _materialize_scenario_switch_content_in_doc(
    ydoc: Any,
    *,
    scenario_id: str,
    content: Mapping[str, Any],
) -> None:
    ui_section, catalog_section, registry_section = _extract_scenario_sections_from_content(content)
    data_section = _coerce_dict(_coerce_dict(content).get("data") or {})

    ui_map = ydoc.get_map("ui")
    registry_map = ydoc.get_map("registry")
    data_map = ydoc.get_map("data")

    with ydoc.begin_transaction() as txn:
        scenarios_ui = _coerce_dict(ui_map.get("scenarios") or {})
        updated_ui = dict(scenarios_ui)
        updated_ui[scenario_id] = {"application": ui_section}
        _set_map_value_if_changed(ui_map, txn, "scenarios", updated_ui)
        _set_map_value_if_changed(ui_map, txn, "current_scenario", scenario_id)

        reg_scenarios = _coerce_dict(registry_map.get("scenarios") or {})
        reg_updated = dict(reg_scenarios)
        reg_updated[scenario_id] = registry_section
        _set_map_value_if_changed(registry_map, txn, "scenarios", reg_updated)

        data_scenarios = _coerce_dict(data_map.get("scenarios") or {})
        data_updated = dict(data_scenarios)
        entry_raw = data_updated.get(scenario_id) or {}
        entry = dict(entry_raw) if isinstance(entry_raw, Mapping) else {}
        entry["catalog"] = catalog_section
        if data_section:
            entry["data"] = data_section
        data_updated[scenario_id] = entry
        _set_map_value_if_changed(data_map, txn, "scenarios", data_updated)


def _scenario_supports_catalog_controls(
    scenario_id: str,
    scenario_application: Mapping[str, Any] | None,
) -> bool:
    scenario_token = str(scenario_id or "").strip()
    if scenario_token == "web_desktop":
        return True
    app = _coerce_dict(scenario_application or {})
    desktop = _coerce_dict(app.get("desktop") or {})
    page_schema = _coerce_dict(desktop.get("pageSchema") or {})
    widgets = page_schema.get("widgets") or []
    if isinstance(widgets, list):
        for raw in widgets:
            if not isinstance(raw, Mapping):
                continue
            widget_type = str(raw.get("type") or "").strip()
            if widget_type == "desktop.widgets":
                return True
            data_source = _coerce_dict(raw.get("dataSource") or {})
            if (
                widget_type == "collection.grid"
                and str(data_source.get("transform") or "").strip() == "desktop.icons"
            ):
                return True
    topbar = desktop.get("topbar") or []
    if isinstance(topbar, list):
        for raw in topbar:
            if not isinstance(raw, Mapping):
                continue
            action = _coerce_dict(raw.get("action") or {})
            modal_id = str(action.get("openModal") or action.get("modalId") or "").strip()
            if modal_id in {"apps_catalog", "widgets_catalog"}:
                return True
    return False


def _set_webspace_rebuild_status(webspace_id: str, **fields: Any) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
    current.update(fields)
    current["webspace_id"] = target
    current["updated_at"] = time.time()
    _WEBSPACE_REBUILD_STATUS[target] = current
    return dict(current)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _record_timing(timings: Dict[str, float], key: str, started_at: float) -> float:
    value = _elapsed_ms(started_at)
    timings[str(key or "").strip() or "unknown"] = value
    return value


def _copy_timing_map(value: Any) -> Dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out: Dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            out[key] = round(float(raw_value), 3)
        except Exception:
            continue
    return out or None


def _sum_timing_values(timings: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(timings, Mapping):
        return None
    total = 0.0
    seen = False
    for key in keys:
        try:
            value = timings.get(key)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except Exception:
            continue
    if not seen:
        return None
    return round(total, 3)


def _derive_phase_timings(
    *,
    switch_timings_ms: Mapping[str, Any] | None = None,
    rebuild_timings_ms: Mapping[str, Any] | None = None,
    switch_mode: str | None = None,
) -> Dict[str, float] | None:
    phase: Dict[str, float] = {}

    switch_total = None
    if isinstance(switch_timings_ms, Mapping):
        try:
            switch_total = float(switch_timings_ms.get("total")) if switch_timings_ms.get("total") is not None else None
        except Exception:
            switch_total = None
    rebuild_total = None
    if isinstance(rebuild_timings_ms, Mapping):
        try:
            rebuild_total = float(rebuild_timings_ms.get("total")) if rebuild_timings_ms.get("total") is not None else None
        except Exception:
            rebuild_total = None

    if switch_total is not None:
        phase["time_to_accept"] = round(switch_total, 3)

    pointer_update = _sum_timing_values(switch_timings_ms, "describe_state_before", "resolve_manifest_policy", "validate_scenario", "write_switch_pointer")
    if pointer_update is not None:
        phase["time_to_pointer_update"] = pointer_update

    if "time_to_first_structure" not in phase and switch_total is not None and rebuild_total is not None:
        full_ready = round(switch_total + rebuild_total, 3)
        phase["time_to_first_structure"] = full_ready
        phase["time_to_interactive_focus"] = full_ready

    if switch_total is not None and rebuild_total is not None:
        phase["time_to_full_hydration"] = round(switch_total + rebuild_total, 3)
    elif rebuild_total is not None:
        phase["time_to_full_hydration"] = round(rebuild_total, 3)

    return phase or None


def _finalize_timing_map(timings: Dict[str, float], *, started_at: float) -> Dict[str, float]:
    finalized = dict(timings)
    finalized["total"] = _elapsed_ms(started_at)
    return finalized


def _set_webspace_rebuild_status_if_current(webspace_id: str, request_id: str | None, **fields: Any) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    request_token = str(request_id or "").strip()
    if request_token:
        current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
        current_request = str(current.get("request_id") or "").strip()
        if current_request and current_request != request_token:
            return current
    if request_token and "request_id" not in fields:
        fields["request_id"] = request_token
    return _set_webspace_rebuild_status(target, **fields)


def describe_webspace_rebuild_state(webspace_id: str) -> dict[str, Any]:
    target = str(webspace_id or "").strip()
    current = dict(_WEBSPACE_REBUILD_STATUS.get(target) or {})
    if not current:
        return {
            "webspace_id": target,
            "status": "idle",
            "pending": False,
            "background": False,
            "updated_at": None,
        }
    return {
        "webspace_id": target,
        "status": str(current.get("status") or "idle"),
        "pending": bool(current.get("pending")),
        "background": bool(current.get("background")),
        "action": str(current.get("action") or "") or None,
        "request_id": str(current.get("request_id") or "") or None,
        "source_of_truth": str(current.get("source_of_truth") or "") or None,
        "scenario_id": str(current.get("scenario_id") or "") or None,
        "scenario_resolution": str(current.get("scenario_resolution") or "") or None,
        "switch_mode": str(current.get("switch_mode") or "") or None,
        "requested_at": current.get("requested_at"),
        "started_at": current.get("started_at"),
        "finished_at": current.get("finished_at"),
        "updated_at": current.get("updated_at"),
        "projection_refresh": dict(current.get("projection_refresh") or {})
        if isinstance(current.get("projection_refresh"), Mapping)
        else None,
        "registry_summary": dict(current.get("registry_summary") or {})
        if isinstance(current.get("registry_summary"), Mapping)
        else None,
        "resolver": dict(current.get("resolver") or {})
        if isinstance(current.get("resolver"), Mapping)
        else None,
        "apply_summary": dict(current.get("apply_summary") or {})
        if isinstance(current.get("apply_summary"), Mapping)
        else None,
        "timings_ms": _copy_timing_map(current.get("timings_ms")),
        "switch_timings_ms": _copy_timing_map(current.get("switch_timings_ms")),
        "semantic_rebuild_timings_ms": _copy_timing_map(current.get("semantic_rebuild_timings_ms")),
        "phase_timings_ms": _copy_timing_map(current.get("phase_timings_ms")),
        "error": str(current.get("error") or "") or None,
    }


class WebspaceScenarioRuntime:
    """
    Core runtime responsible for computing and applying the effective UI
    (application + catalog + registry + installed) for a given webspace.

    It reads:
      - ui.current_scenario,
      - scenario content from loader-backed canonical sources,
      - legacy Yjs scenario materialization as fallback only,
      - skill webui.json declarations (apps/widgets/registry/contributions),
      - persistent webspace desktop overlay,
    and writes:
      - ui.application,
      - data.catalog,
      - data.installed,
      - data.desktop,
      - registry.merged.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()
        # Cached snapshot of desktop scenarios discovered on disk.
        self._desktop_scenarios: Optional[List[Tuple[str, str]]] = None
        self._last_rebuild_timings_ms: Dict[str, float] | None = None
        self._last_resolver_debug: Dict[str, Any] | None = None
        self._last_apply_summary: Dict[str, Any] | None = None

    # --- scenario helpers -------------------------------------------------

    def _list_desktop_scenarios(self, space: str) -> List[Tuple[str, str]]:
        """
        Discover scenarios with ``type: desktop`` under the workspace
        scenarios directory. Returns a list of ``(scenario_id, title)``
        tuples. The ``web_desktop`` scenario itself is excluded so that it
        does not create a recursive launcher icon.

        ``space`` controls which manifest metadata is preferred:
          - ``workspace`` ¢?" use workspace manifests only,
          - ``dev``       ¢?" prefer dev manifests, fallback to workspace.
        """
        entries: List[Tuple[str, str]] = []
        try:
            root = self.ctx.paths.scenarios_dir()
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                scenario_id = child.name
                if scenario_id == "web_desktop":
                    continue
                if space == "dev":
                    manifest = scenarios_loader.read_manifest(scenario_id, space="dev")
                    if not isinstance(manifest, dict) or not manifest:
                        manifest = scenarios_loader.read_manifest(scenario_id, space="workspace")
                else:
                    manifest = scenarios_loader.read_manifest(scenario_id, space="workspace")
                if not isinstance(manifest, dict) or not manifest:
                    continue
                if manifest.get("type") != "desktop":
                    continue
                title = str(manifest.get("title") or manifest.get("name") or scenario_id)
                entries.append((scenario_id, title))
        except Exception:
            _log.debug("failed to list desktop scenarios", exc_info=True)
        return entries

    # --- helpers ---------------------------------------------------------

    def _load_webui(self, skill_name: str, space: str) -> Dict[str, Any]:
        paths = self.ctx.paths
        base = paths.dev_skills_dir() if space == "dev" else paths.skills_dir()
        path = Path(base) / skill_name / "webui.json"
        if not path.exists():
            try:
                repo_root_attr = getattr(paths, "repo_root", None)
                repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
                if repo_root:
                    fallback = (
                        Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills" / skill_name / "webui.json"
                    )
                    if fallback.exists():
                        path = fallback
            except Exception:
                pass
        if not path.exists():
            _WEBUI_DECL_CACHE.pop(str(path), None)
            _log.debug("webui.json missing for %s (%s)", skill_name, space)
            return {}
        cache_key = str(path.resolve())
        try:
            stat = path.stat()
            stamp = (cache_key, int(stat.st_mtime_ns), int(stat.st_size))
        except Exception:
            stamp = None
        if stamp is not None:
            cached = _WEBUI_DECL_CACHE.get(cache_key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            # Accept UTF-8 with BOM produced by some Windows/PowerShell editors.
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _log.warning("failed to read webui.json for %s: %s", skill_name, exc)
            if stamp is not None:
                _WEBUI_DECL_CACHE[cache_key] = (stamp, {})
            return {}
        if not isinstance(raw, dict):
            _log.warning("webui.json must be an object for %s", skill_name)
            if stamp is not None:
                _WEBUI_DECL_CACHE[cache_key] = (stamp, {})
            return {}

        catalog = raw.get("catalog") or {}
        apps = raw.get("apps") or catalog.get("apps") or []
        widgets = raw.get("widgets") or catalog.get("widgets") or []
        registry = raw.get("registry") or {}
        reg_modals_raw = registry.get("modals") or {}
        reg_widgets_raw = registry.get("widgets") or {}
        ydoc_defaults = raw.get("ydoc_defaults") or {}
        raw_contrib = raw.get("contributions") or []
        contributions = [c for c in raw_contrib if isinstance(c, dict)]

        payload = {
            "skill": skill_name,
            "space": space,
            "apps": [_apply_webui_load_hint(it) for it in apps if isinstance(it, dict)],
            "widgets": [_apply_webui_load_hint(it) for it in widgets if isinstance(it, dict)],
            "registry": {
                "modals": (
                    {str(k): _normalize_webui_modal_def(v) for k, v in reg_modals_raw.items()}
                    if isinstance(reg_modals_raw, dict)
                    else [str(x) for x in reg_modals_raw if isinstance(x, (str, int))]
                ),
                "widgets": (
                    {str(k): _apply_webui_load_hint(v) for k, v in reg_widgets_raw.items()}
                    if isinstance(reg_widgets_raw, dict)
                    else [str(x) for x in reg_widgets_raw if isinstance(x, (str, int))]
                ),
            },
            "ydoc_defaults": ydoc_defaults if isinstance(ydoc_defaults, dict) else {},
            "contributions": contributions,
        }
        if stamp is not None:
            _WEBUI_DECL_CACHE[cache_key] = (stamp, payload)
        return payload

    def _collect_skill_decls(self, mode: str = "mixed") -> List[Dict[str, Any]]:
        try:
            cap = get_local_capacity()
            skills = cap.get("skills") or []
        except Exception:
            skills = []
        if not isinstance(skills, list):
            skills = []

        decls: List[Dict[str, Any]] = []
        for rec in skills:
            if not isinstance(rec, dict) or not rec.get("active", True):
                continue
            name = rec.get("name") or rec.get("id")
            if not name:
                continue
            skill_name = str(name)

            if mode == "workspace":
                # Workspace mode: always use default webui.json regardless of
                # dev flag so that skills remain visible even when a dev
                # variant exists.
                decl = self._load_webui(skill_name, "default")
                if decl:
                    decls.append(decl)
                continue

            if mode == "dev":
                # Dev mode: include all active skills but prefer dev webui.json
                # when present, falling back to workspace webui.json.
                decl = self._load_webui(skill_name, "dev")
                if not decl:
                    decl = self._load_webui(skill_name, "default")
                if decl:
                    decls.append(decl)
                continue

            # Mixed mode: include both dev and default variants as-is.
            space = "dev" if rec.get("dev") else "default"
            decl = self._load_webui(skill_name, space)
            if decl:
                decls.append(decl)

        # Always ensure desktop skill's own webui.json is loaded so that
        # base desktop modals remain available even if not listed in capacity.
        try:
            desktop_decl = self._load_webui("web_desktop_skill", "default")
        except Exception:
            desktop_decl = {}
        if isinstance(desktop_decl, dict) and desktop_decl:
            decls.append(desktop_decl)

        return decls

    def _apply_ydoc_defaults_in_txn(self, ydoc: Y.YDoc, txn: Any, decls: List[Dict[str, Any]]) -> None:  # type: ignore[override]
        spec: Dict[str, Any] = {}
        for decl in decls:
            raw = decl.get("ydoc_defaults") or {}
            if not isinstance(raw, dict):
                continue
            for path, default in raw.items():
                if not isinstance(path, str):
                    continue
                # Preserve first writer semantics for conflicting defaults.
                spec.setdefault(path, default)

        for path, default in spec.items():
            segments = [s for s in path.split("/") if s]
            if len(segments) != 2:
                continue
            root_name, key = segments
            root = ydoc.get_map(root_name)
            if root.get(key) is not None:
                continue
            try:
                value = json.loads(json.dumps(default))
            except Exception:
                value = default
            root.set(txn, key, value)

    def _collect_resolver_inputs_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebspaceResolverInputs:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")

        scenario_id = str(ui_map.get("current_scenario") or "web_desktop").strip() or "web_desktop"

        mode = "mixed"
        metadata: Dict[str, Any] = {}
        overlay_snapshot: Dict[str, Any] = {}
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                mode = row.effective_source_mode
                metadata = {
                    "title": row.title,
                    "kind": row.effective_kind,
                    "source_mode": row.effective_source_mode,
                    "home_scenario": row.effective_home_scenario,
                    "is_dev": row.is_dev,
                }
                if getattr(row, "has_ui_overlay", False):
                    overlay_snapshot = {
                        "installed": _coerce_dict(getattr(row, "installed_overlay", {}) or {}),
                        "pinnedWidgets": _normalize_overlay_widget_entries(
                            getattr(row, "pinned_widgets_overlay", []) or []
                        ),
                        "topbar": list(getattr(row, "topbar_overlay", []) or []),
                        "pageSchema": _coerce_dict(getattr(row, "page_schema_overlay", {}) or {}),
                        "source": "workspace_manifest_overlay",
                    }
        except Exception:
            mode = "mixed"
            metadata = {}

        scenario_app_ui, base_catalog, registry_entry, scenario_source, legacy_fallback = _resolve_scenario_sections_in_doc(
            ydoc,
            webspace_id=webspace_id,
            scenario_id=scenario_id,
            source_mode=mode,
        )
        if metadata:
            metadata = dict(metadata)
        metadata["scenario_source"] = scenario_source
        metadata["legacy_scenario_fallback"] = legacy_fallback

        return WebspaceResolverInputs(
            webspace_id=webspace_id,
            scenario_id=str(scenario_id),
            source_mode=mode,
            metadata=metadata,
            scenario_application=scenario_app_ui,
            scenario_catalog=base_catalog,
            scenario_registry=registry_entry,
            overlay_snapshot=overlay_snapshot,
            live_state={
                "desktop": _coerce_dict(data_map.get("desktop") or {}),
                "routing": _coerce_dict(data_map.get("routing") or {}),
            },
            skill_decls=self._collect_skill_decls(mode=mode),
            desktop_scenarios=self._list_desktop_scenarios(space=mode),
            scenario_source=scenario_source,
            legacy_scenario_fallback=legacy_fallback,
        )

    def resolve_webspace(self, inputs: WebspaceResolverInputs) -> WebspaceResolverOutputs:
        cache_keys = _resolver_cache_keys(inputs)
        resolver_fingerprint = _resolver_input_fingerprint(inputs, cache_keys=cache_keys)
        resolver_debug = {
            "source": str(inputs.scenario_source or ""),
            "legacy_fallback": bool(inputs.legacy_scenario_fallback),
            "cache_keys": dict(cache_keys),
            "input_fingerprint": resolver_fingerprint,
            "cache_hit": False,
        }
        cached = _get_cached_resolved_outputs(resolver_fingerprint)
        if cached is not None:
            resolver_debug["cache_hit"] = True
            self._last_resolver_debug = resolver_debug
            return cached

        scenario_id = str(inputs.scenario_id or "").strip() or "web_desktop"
        source_mode = str(inputs.source_mode or "").strip() or "mixed"
        scenario_application = _coerce_dict(inputs.scenario_application or {})
        scenario_desktop = _coerce_dict(scenario_application.get("desktop") or {})
        scenario_catalog = _coerce_dict(inputs.scenario_catalog or {})
        scenario_registry = _coerce_dict(inputs.scenario_registry or {})
        scenario_apps = [it for it in (scenario_catalog.get("apps") or []) if isinstance(it, Mapping)]
        scenario_widgets = [it for it in (scenario_catalog.get("widgets") or []) if isinstance(it, Mapping)]
        base_registry_modals = [str(x) for x in (scenario_registry.get("modals") or [])]
        base_registry_widgets = [str(x) for x in (scenario_registry.get("widgets") or [])]

        skill_decls = list(inputs.skill_decls or [])
        skill_apps: List[Dict[str, Any]] = []
        skill_widgets: List[Dict[str, Any]] = []
        skill_registry_modals: List[List[str]] = []
        skill_registry_widgets: List[List[str]] = []
        auto_widget_ids: set[str] = set()
        auto_app_ids: set[str] = set()

        for decl in skill_decls:
            skill_name = decl.get("skill") or ""
            space = decl.get("space") or "default"
            source = f"skill:{skill_name}"
            dev_flag = space == "dev"
            for app in decl.get("apps") or []:
                if isinstance(app, dict):
                    skill_apps.append(_mark_entry(app, source=source, dev=dev_flag))
            for widget in decl.get("widgets") or []:
                if isinstance(widget, dict):
                    skill_widgets.append(_mark_entry(widget, source=source, dev=dev_flag))
            reg = decl.get("registry") or {}
            mod_spec = reg.get("modals") or {}
            if isinstance(mod_spec, dict):
                skill_registry_modals.append([str(k) for k in mod_spec.keys()])
            else:
                skill_registry_modals.append([str(x) for x in mod_spec])
            wid_spec = reg.get("widgets") or {}
            if isinstance(wid_spec, dict):
                skill_registry_widgets.append([str(k) for k in wid_spec.keys()])
            else:
                skill_registry_widgets.append([str(x) for x in wid_spec])
            for contrib in decl.get("contributions") or []:
                if not isinstance(contrib, dict):
                    continue
                ep = str(contrib.get("extensionPoint") or "")
                ctype = str(contrib.get("type") or "")
                cid = str(contrib.get("id") or "")
                auto = bool(contrib.get("autoInstall"))
                if not cid or not auto:
                    continue
                if ep == "desktop.widgets" and ctype == "widget":
                    auto_widget_ids.add(cid)
                if ep == "desktop.apps" and ctype == "app":
                    auto_app_ids.add(cid)

        merged_apps = [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_apps]
        merged_widgets = [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_widgets]

        extra_apps: List[Dict[str, Any]] = []
        for sid, title in inputs.desktop_scenarios:
            if sid == scenario_id:
                continue
            app_id = f"scenario:{sid}"
            extra_apps.append(
                {
                    "id": app_id,
                    "title": title,
                    "icon": "apps-outline",
                    "scenario_id": sid,
                }
            )
            auto_app_ids.add(app_id)

        merged_apps = _merge_by_id(merged_apps + extra_apps + skill_apps)
        merged_widgets = _merge_by_id(merged_widgets + skill_widgets)
        supports_catalog_controls = _scenario_supports_catalog_controls(
            scenario_id,
            scenario_application,
        )
        default_modal_ids = ["scenario_switcher"]
        if supports_catalog_controls:
            default_modal_ids = ["apps_catalog", "widgets_catalog", *default_modal_ids]
        merged_registry = {
            "modals": _merge_registry_lists(
                base_registry_modals,
                skill_registry_modals + [default_modal_ids],
            ),
            "widgets": _merge_registry_lists(base_registry_widgets, skill_registry_widgets),
        }

        installed_current = _coerce_dict((inputs.overlay_snapshot or {}).get("installed") or {})
        overlay_has_pinned_widgets = "pinnedWidgets" in (inputs.overlay_snapshot or {})
        overlay_pinned_widgets = _normalize_overlay_widget_entries((inputs.overlay_snapshot or {}).get("pinnedWidgets"))
        scenario_pinned_widgets = _normalize_overlay_widget_entries(scenario_desktop.get("pinnedWidgets"))
        scenario_topbar = list(scenario_desktop.get("topbar") or []) if isinstance(scenario_desktop.get("topbar"), list) else []
        scenario_page_schema = _coerce_dict(scenario_desktop.get("pageSchema") or {})
        installed_with_auto = _merge_installed_with_auto(
            installed_current,
            auto_apps=auto_app_ids,
            auto_widgets=auto_widget_ids,
        )

        merged_modals_map: Dict[str, Any] = {}
        base_modals_map = _coerce_dict(scenario_application.get("modals") or {})
        for key, value in base_modals_map.items():
            merged_modals_map[str(key)] = value
        for decl in skill_decls:
            reg = decl.get("registry") or {}
            mod_spec = reg.get("modals") or {}
            if not isinstance(mod_spec, dict):
                continue
            for key, value in mod_spec.items():
                token = str(key)
                if token and token not in merged_modals_map:
                    merged_modals_map[token] = value

        if supports_catalog_controls and "apps_catalog" not in merged_modals_map:
            merged_modals_map["apps_catalog"] = {
                "title": "Available Apps",
                "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                "schema": {
                    "id": "apps_catalog",
                    "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "apps-list",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Apps",
                            "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                            "dataSource": {
                                "kind": "y",
                                "path": "data/catalog/apps",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "callHost",
                                    "target": "desktop.toggleInstall",
                                    "params": {
                                        "type": "app",
                                        "id": "$event.id",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }
        if supports_catalog_controls and "widgets_catalog" not in merged_modals_map:
            merged_modals_map["widgets_catalog"] = {
                "title": "Available Widgets",
                "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                "schema": {
                    "id": "widgets_catalog",
                    "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "widgets-list",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Widgets",
                            "load": dict(_DEFERRED_OFF_FOCUS_LOAD),
                            "dataSource": {
                                "kind": "y",
                                "path": "data/catalog/widgets",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "callHost",
                                    "target": "desktop.toggleInstall",
                                    "params": {
                                        "type": "widget",
                                        "id": "$event.id",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }

        app_with_modals: Dict[str, Any] = dict(scenario_application)
        if merged_modals_map:
            app_with_modals["modals"] = merged_modals_map
        desktop_config = _coerce_dict(app_with_modals.get("desktop") or {})
        desktop_config["topbar"] = scenario_topbar
        desktop_config["pageSchema"] = scenario_page_schema
        desktop_config["pinnedWidgets"] = (
            overlay_pinned_widgets if overlay_has_pinned_widgets else scenario_pinned_widgets
        )
        app_with_modals["desktop"] = desktop_config

        desktop_next = _coerce_dict((inputs.live_state or {}).get("desktop") or {})
        desktop_installed = _coerce_dict(desktop_next.get("installed") or {})
        desktop_installed["apps"] = list(installed_with_auto.get("apps") or [])
        desktop_installed["widgets"] = list(installed_with_auto.get("widgets") or [])
        desktop_next["installed"] = desktop_installed
        desktop_next["topbar"] = list(desktop_config.get("topbar") or [])
        desktop_next["pageSchema"] = _coerce_dict(desktop_config.get("pageSchema") or {})
        desktop_next["pinnedWidgets"] = list(desktop_config.get("pinnedWidgets") or [])

        routing_dict = _coerce_dict((inputs.live_state or {}).get("routing") or {})
        routes = routing_dict.get("routes")
        routing_dict = {**routing_dict, "routes": _coerce_dict(routes)}

        resolved = WebspaceResolverOutputs(
            webspace_id=inputs.webspace_id,
            scenario_id=scenario_id,
            source_mode=source_mode,
            application=app_with_modals,
            catalog={
                "apps": [dict(it) for it in merged_apps],
                "widgets": [dict(it) for it in merged_widgets],
            },
            registry={
                "modals": list(merged_registry.get("modals") or []),
                "widgets": list(merged_registry.get("widgets") or []),
            },
            installed={
                "apps": list(installed_with_auto.get("apps") or []),
                "widgets": list(installed_with_auto.get("widgets") or []),
            },
            desktop=desktop_next,
            routing=routing_dict,
            skill_decls=skill_decls,
        )
        _remember_resolved_outputs(resolver_fingerprint, resolved)
        self._last_resolver_debug = resolver_debug
        return resolved

    def _apply_resolved_state_in_doc(self, ydoc: Y.YDoc, webspace_id: str, resolved: WebspaceResolverOutputs) -> None:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")
        target_paths = (
            "ui.application",
            "data.catalog",
            "data.installed",
            "data.desktop",
            "data.routing",
            "registry.merged",
        )
        changed_paths: List[str] = []
        failed_paths: List[str] = []
        defaults_failed = False

        def _apply_branch(path: str, y_map: Any, key: str, value: Any, *, ignore_errors: bool = False) -> None:
            try:
                changed = _set_map_value_if_changed(y_map, txn, key, value)
            except Exception:
                if not ignore_errors:
                    raise
                failed_paths.append(path)
                return
            if changed:
                changed_paths.append(path)

        with ydoc.begin_transaction() as txn:
            try:
                self._apply_ydoc_defaults_in_txn(ydoc, txn, resolved.skill_decls)
            except Exception:
                defaults_failed = True
                _log.warning("failed to apply ydoc_defaults for webspace=%s", webspace_id, exc_info=True)

            _apply_branch("ui.application", ui_map, "application", resolved.application)
            _apply_branch("data.catalog", data_map, "catalog", resolved.catalog)
            _apply_branch("data.installed", data_map, "installed", resolved.installed)
            _apply_branch("data.desktop", data_map, "desktop", resolved.desktop, ignore_errors=True)
            _apply_branch("data.routing", data_map, "routing", resolved.routing, ignore_errors=True)
            _apply_branch("registry.merged", registry_map, "merged", resolved.registry)

        self._last_apply_summary = {
            "branch_count": len(target_paths),
            "changed_branches": len(changed_paths),
            "unchanged_branches": len(target_paths) - len(changed_paths) - len(failed_paths),
            "failed_branches": len(failed_paths),
            "changed_paths": list(changed_paths),
            "defaults_failed": defaults_failed,
        }
        if failed_paths:
            self._last_apply_summary["failed_paths"] = list(failed_paths)

    def _resolve_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebspaceResolverOutputs:
        return self.resolve_webspace(self._collect_resolver_inputs_in_doc(ydoc, webspace_id))

    def _rebuild_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebUIRegistryEntry:
        rebuild_started = time.perf_counter()
        timings: Dict[str, float] = {}
        self._last_resolver_debug = None
        self._last_apply_summary = None

        stage_started = time.perf_counter()
        inputs = self._collect_resolver_inputs_in_doc(ydoc, webspace_id)
        _record_timing(timings, "collect_inputs", stage_started)

        stage_started = time.perf_counter()
        resolved = self.resolve_webspace(inputs)
        _record_timing(timings, "resolve", stage_started)

        stage_started = time.perf_counter()
        self._apply_resolved_state_in_doc(ydoc, webspace_id, resolved)
        _record_timing(timings, "apply", stage_started)

        stage_started = time.perf_counter()
        entry = resolved.to_registry_entry()
        _record_timing(timings, "to_registry_entry", stage_started)
        self._last_rebuild_timings_ms = _finalize_timing_map(timings, started_at=rebuild_started)

        try:
            _log.debug(
                "rebuilt webspace=%s scenario=%s source=%s legacy_fallback=%s cache_hit=%s apply=%d/%d apps=%d widgets=%d timings_ms=%s",
                webspace_id,
                resolved.scenario_id,
                str(inputs.scenario_source or ""),
                bool(inputs.legacy_scenario_fallback),
                bool((self._last_resolver_debug or {}).get("cache_hit")),
                int((self._last_apply_summary or {}).get("changed_branches") or 0),
                int((self._last_apply_summary or {}).get("branch_count") or 0),
                len(entry.apps),
                len(entry.widgets),
                self._last_rebuild_timings_ms,
            )
        except Exception:
            pass

        return entry

    # --- public API ------------------------------------------------------

    def compute_registry_for_webspace(self, webspace_id: str) -> WebUIRegistryEntry:
        """
        Compute and apply the effective UI model for the given webspace.

        This is a synchronous helper that loads the YDoc via get_ydoc(),
        rebuilds ui.application/data.catalog/data.installed/registry.merged
        and returns the resulting registry snapshot.
        """
        with get_ydoc(webspace_id) as ydoc:
            return self._rebuild_in_doc(ydoc, webspace_id)

    async def rebuild_webspace_async(self, webspace_id: str) -> WebUIRegistryEntry:
        """
        Async counterpart of :meth:`compute_registry_for_webspace` for use
        inside running event loops.
        """
        async with async_get_ydoc(webspace_id) as ydoc:
            return self._rebuild_in_doc(ydoc, webspace_id)


# --- webspace helpers ---------------------------------------------------


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload") or {}
        if isinstance(data, dict):
            return data
    if isinstance(evt, dict):
        return evt
    return {}


def _webspace_id(payload: Dict[str, Any]) -> str:
    """
    Resolve target webspace id for an event payload.

    Explicit fields on the payload (webspace_id/workspace_id) take
    precedence over metadata injected by the transport (_meta).
    """
    if isinstance(payload, dict):
        direct = payload.get("webspace_id") or payload.get("workspace_id")
        if direct:
            return str(direct)
        meta = payload.get("_meta")
        if isinstance(meta, dict):
            token = meta.get("webspace_id") or meta.get("workspace_id")
            if token:
                return str(token)
    return default_webspace_id()


async def _resolve_projection_refresh_target(
    webspace_id: str,
    *,
    scenario_id: str | None = None,
) -> str | None:
    explicit = str(scenario_id or "").strip()
    if explicit:
        return explicit
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            current = str(ui_map.get("current_scenario") or "").strip()
            return current or None
    except Exception:
        _log.debug("failed to resolve projection refresh target for webspace=%s", webspace_id, exc_info=True)
        return None


def _resolve_projection_refresh_space(webspace_id: str) -> str:
    try:
        row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
        return "dev" if str(getattr(row, "effective_source_mode", "") or "").strip().lower() == "dev" else "workspace"
    except Exception:
        return "workspace"


async def _refresh_projection_rules_for_rebuild(
    ctx: AgentContext,
    webspace_id: str,
    *,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    target_scenario = await _resolve_projection_refresh_target(webspace_id, scenario_id=scenario_id)
    target_space = _resolve_projection_refresh_space(webspace_id)
    if not target_scenario:
        return {
            "attempted": False,
            "scenario_id": None,
            "space": target_space,
            "rules_loaded": 0,
            "source": "none",
        }
    try:
        rules_loaded = int(ctx.projections.load_from_scenario(target_scenario, space=target_space) or 0)
        return {
            "attempted": True,
            "scenario_id": target_scenario,
            "space": target_space,
            "rules_loaded": rules_loaded,
            "source": "scenario_manifest",
        }
    except Exception as exc:
        try:
            replace_scenario_entries = getattr(ctx.projections, "replace_scenario_entries", None)
            if callable(replace_scenario_entries):
                replace_scenario_entries([], scenario_id=target_scenario, space=target_space)
        except Exception:
            _log.debug("failed to clear stale scenario data_projections for scenario=%s", target_scenario, exc_info=True)
        _log.debug("failed to refresh data_projections for scenario=%s", target_scenario, exc_info=True)
        return {
            "attempted": True,
            "scenario_id": target_scenario,
            "space": target_space,
            "rules_loaded": 0,
            "source": "scenario_manifest",
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _slugify_webspace_id(raw: str | None) -> str:
    if not raw:
        return ""
    # Preserve original casing while normalising invalid characters so that
    # webspace ids used in events and YDoc room names stay identical.
    token = _WS_ID_RE.sub("-", str(raw).strip())
    return token.strip("-")


def _allocate_webspace_id(raw: str | None) -> str:
    candidate = _slugify_webspace_id(raw)
    if not candidate:
        candidate = f"space-{secrets.token_hex(2)}"
    base = candidate
    suffix = 1
    while workspace_index.get_workspace(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _is_dev_title(title: Optional[str]) -> bool:
    if not title:
        return False
    return str(title).lstrip().upper().startswith("DEV:")


def _display_name_for_kind(title: Optional[str], *, webspace_id: str, kind: str) -> str:
    raw_title = (title or webspace_id).strip() or webspace_id
    if kind == "dev":
        if _is_dev_title(raw_title):
            return raw_title
        return f"DEV: {raw_title}"
    if _is_dev_title(raw_title):
        return raw_title.lstrip()[4:].lstrip() or webspace_id
    return raw_title


def _webspace_listing() -> List[Dict[str, Any]]:
    rows = workspace_index.list_workspaces()
    return [
        {
            "id": row.workspace_id,
            "title": row.title,
            "created_at": row.created_at,
            "kind": row.effective_kind,
            "home_scenario": row.effective_home_scenario,
            "source_mode": row.effective_source_mode,
        }
        for row in rows
    ]


def _webspace_info_from_row(row: workspace_index.WebspaceManifest) -> WebspaceInfo:
    return WebspaceInfo(
        id=row.workspace_id,
        title=row.title,
        created_at=row.created_at,
        kind=row.effective_kind,
        home_scenario=row.effective_home_scenario,
        source_mode=row.effective_source_mode,
        is_dev=row.is_dev,
    )


async def describe_webspace_operational_state(webspace_id: str) -> WebspaceOperationalState:
    """
    Return the combined manifest + live scenario state for a webspace.

    The helper intentionally keeps both the raw stored ``home_scenario`` and
    the effective fallback value so Phase 2 callers can preserve legacy reload
    behaviour while still exposing stable operational semantics to control
    surfaces.
    """
    target_webspace_id = str(webspace_id or "").strip() or default_webspace_id()
    row = workspace_index.get_workspace(target_webspace_id) or workspace_index.ensure_workspace(target_webspace_id)

    current_scenario: str | None = None
    try:
        async with async_get_ydoc(target_webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            raw_current = ui_map.get("current_scenario")
            if raw_current is not None:
                token = str(raw_current).strip()
                current_scenario = token or None
    except Exception:
        current_scenario = None

    return WebspaceOperationalState(
        webspace_id=target_webspace_id,
        title=row.title,
        kind=row.effective_kind,
        source_mode=row.effective_source_mode,
        is_dev=row.is_dev,
        stored_home_scenario=str(row.home_scenario).strip() if row.home_scenario else None,
        effective_home_scenario=row.effective_home_scenario,
        current_scenario=current_scenario,
    )


def describe_webspace_overlay_state(webspace_id: str) -> dict[str, Any]:
    target_webspace_id = str(webspace_id or "").strip() or default_webspace_id()
    row = workspace_index.get_workspace(target_webspace_id) or workspace_index.ensure_workspace(target_webspace_id)
    return {
        "webspace_id": target_webspace_id,
        "source": "workspace_manifest_overlay",
        "has_overlay": bool(getattr(row, "has_ui_overlay", False)),
        "has_installed": bool(getattr(row, "has_installed_overlay", False)),
        "has_pinned_widgets": bool(getattr(row, "has_pinned_widgets_overlay", False)),
        "has_topbar": bool(getattr(row, "has_topbar_overlay", False)),
        "has_page_schema": bool(getattr(row, "has_page_schema_overlay", False)),
        "desktop": dict(getattr(row, "desktop_overlay", {}) or {}),
        "installed": _coerce_dict(getattr(row, "installed_overlay", {}) or {}),
        "pinned_widgets": _normalize_overlay_widget_entries(getattr(row, "pinned_widgets_overlay", []) or []),
        "topbar": list(getattr(row, "topbar_overlay", []) or []),
        "page_schema": _coerce_dict(getattr(row, "page_schema_overlay", {}) or {}),
    }


async def describe_webspace_projection_state(
    webspace_id: str,
    *,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    """
    Return a lightweight snapshot of the projection lifecycle for a webspace.

    This is a read-only control-surface helper: it does not refresh or mutate
    the active registry, it only explains which scenario the current layer is
    targeting and whether that matches the active scenario layer in memory.
    """
    operational = await describe_webspace_operational_state(webspace_id)
    target_scenario = (
        str(scenario_id or "").strip()
        or str(operational.current_scenario or "").strip()
        or str(operational.effective_home_scenario or "").strip()
        or None
    )

    registry = get_ctx().projections
    snapshot: Dict[str, Any] = {}
    try:
        raw = registry.snapshot() if hasattr(registry, "snapshot") else {}
        snapshot = dict(raw) if isinstance(raw, Mapping) else {}
    except Exception:
        snapshot = {}

    active_scenario = str(snapshot.get("active_scenario_id") or "").strip() or None
    active_space = str(snapshot.get("active_space") or "").strip() or "workspace"
    target_space = "dev" if str(operational.source_mode or "").strip().lower() == "dev" else "workspace"
    return {
        "webspace_id": operational.webspace_id,
        "target_scenario": target_scenario,
        "target_space": target_space,
        "active_scenario": active_scenario,
        "active_space": active_space,
        "active_matches_target": bool(target_scenario)
        and active_scenario == target_scenario
        and active_space == target_space,
        "base_rule_count": int(snapshot.get("base_rule_count") or 0),
        "scenario_rule_count": int(snapshot.get("scenario_rule_count") or 0),
        "source": "projection_registry",
    }


async def _resolve_reload_scenario_target(
    webspace_id: str,
    requested_scenario_id: str | None,
) -> tuple[WebspaceOperationalState, str, str]:
    """
    Resolve the scenario source for reload/reset.

    Ordering intentionally preserves Phase 1 / Phase 2 compatibility:

    1. explicit scenario override
    2. explicit stored manifest home_scenario
    3. current live scenario for legacy spaces without stored home
    4. default ``web_desktop``
    """
    state = await describe_webspace_operational_state(webspace_id)
    requested = str(requested_scenario_id or "").strip()
    if requested:
        return state, requested, "explicit"
    if state.stored_home_scenario:
        return state, state.stored_home_scenario, "manifest_home"
    if state.current_scenario:
        return state, state.current_scenario, "current_scenario"
    return state, "web_desktop", "default"


async def _sync_webspace_listing() -> None:
    listing = _webspace_listing()
    payload = {"items": listing}
    rows = workspace_index.list_workspaces()
    for row in rows:
        async with async_get_ydoc(row.workspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            with ydoc.begin_transaction() as txn:
                _set_map_value_if_changed(data_map, txn, "webspaces", payload)


class WebspaceService:
    """
    Helper for managing webspaces (workspaces) from core services and SDK.

    This service centralises CRUD logic that was previously spread across
    event handlers so that higher-level callers do not need to touch YDoc
    or SQLite details directly.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    def list(self, *, mode: str = "mixed") -> List[WebspaceInfo]:
        """
        List known webspaces.

        mode:
          - \"workspace\" — only non-dev webspaces,
          - \"dev\"       — only dev webspaces,
          - \"mixed\"     — all (default).
        """
        rows = workspace_index.list_workspaces()
        infos: List[WebspaceInfo] = []
        for row in rows:
            title = row.title
            kind = row.effective_kind
            is_dev = row.is_dev
            if mode == "workspace" and kind != "workspace":
                continue
            if mode == "dev" and kind != "dev":
                continue
            infos.append(_webspace_info_from_row(row))
        return infos

    async def _sync_listing(self) -> None:
        await _sync_webspace_listing()

    async def create(
        self,
        requested_id: Optional[str],
        title: Optional[str],
        *,
        scenario_id: str = "web_desktop",
        dev: bool = False,
    ) -> WebspaceInfo:
        webspace_id = _allocate_webspace_id(requested_id)
        _log.info("creating webspace %s (requested=%s dev=%s)", webspace_id, requested_id, dev)
        kind = "dev" if dev else "workspace"
        source_mode = "dev" if dev else "workspace"
        workspace_index.ensure_workspace(webspace_id)
        display_name = _display_name_for_kind(title, webspace_id=webspace_id, kind=kind)
        row = workspace_index.set_workspace_manifest(
            webspace_id,
            display_name=display_name,
            kind=kind,
            home_scenario=str(scenario_id or "").strip() or "web_desktop",
            source_mode=source_mode,
        )
        await _seed_webspace_from_scenario(webspace_id, scenario_id, dev=dev)
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def rename(self, webspace_id: str, title: str) -> Optional[WebspaceInfo]:
        webspace_id = (webspace_id or "").strip()
        title = (title or "").strip()
        if not webspace_id or not title:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot rename missing webspace %s", webspace_id)
            return None
        display_name = _display_name_for_kind(title, webspace_id=webspace_id, kind=row.effective_kind)
        row = workspace_index.set_workspace_manifest(
            webspace_id,
            display_name=display_name,
            kind=row.effective_kind,
            source_mode=row.effective_source_mode,
        )
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def update_metadata(
        self,
        webspace_id: str,
        *,
        title: str | None = None,
        home_scenario: str | None = None,
    ) -> Optional[WebspaceInfo]:
        webspace_id = str(webspace_id or "").strip()
        if not webspace_id:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot update missing webspace %s", webspace_id)
            return None

        manifest_kwargs: Dict[str, Any] = {}
        next_title = str(title or "").strip()
        if next_title:
            manifest_kwargs["display_name"] = _display_name_for_kind(
                next_title,
                webspace_id=webspace_id,
                kind=row.effective_kind,
            )

        next_home_scenario = str(home_scenario or "").strip()
        if next_home_scenario:
            manifest_kwargs["home_scenario"] = next_home_scenario

        if not manifest_kwargs:
            return _webspace_info_from_row(row)

        updated = workspace_index.set_workspace_manifest(webspace_id, **manifest_kwargs)
        await self._sync_listing()
        return _webspace_info_from_row(updated)

    async def set_home_scenario(self, webspace_id: str, scenario_id: str) -> Optional[WebspaceInfo]:
        webspace_id = (webspace_id or "").strip()
        scenario_id = (scenario_id or "").strip()
        if not webspace_id or not scenario_id:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot set home_scenario for missing webspace %s", webspace_id)
            return None
        row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
        await self._sync_listing()
        return _webspace_info_from_row(row)

    async def ensure_dev_for_scenario(
        self,
        scenario_id: str,
        *,
        requested_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[WebspaceInfo, bool]:
        scenario_id = str(scenario_id or "").strip()
        requested_id = str(requested_id or "").strip() or None
        title = str(title or "").strip() or None
        if not scenario_id:
            raise ValueError("scenario_id is required")

        existing: Optional[workspace_index.WebspaceManifest] = None
        if requested_id:
            row = workspace_index.get_workspace(requested_id)
            if row and not row.is_dev:
                raise ValueError("requested webspace is not a dev webspace")
            existing = row
        if existing is None:
            for row in workspace_index.list_workspaces():
                if row.is_dev and row.effective_home_scenario == scenario_id:
                    existing = row
                    break

        created = False
        if existing is None:
            preferred_id = requested_id or f"dev-{scenario_id}"
            info = await self.create(
                preferred_id,
                title or scenario_id,
                scenario_id=scenario_id,
                dev=True,
            )
            created = True
        else:
            info = _webspace_info_from_row(existing)

        return info, created

    async def delete(self, webspace_id: str) -> bool:
        webspace_id = (webspace_id or "").strip()
        if not webspace_id or webspace_id == default_webspace_id():
            return False
        _log.info("deleting webspace %s via WebspaceService", webspace_id)
        try:
            workspace_index.delete_workspace(webspace_id)
        except Exception as exc:
            _log.warning("failed to delete webspace %s: %s", webspace_id, exc)
            return False
        try:
            from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
            from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel
 
            try:
                await reset_live_webspace_room(webspace_id, close_reason="webspace_delete")
            except Exception:
                pass
            try:
                reset_ystore_for_webspace(webspace_id)
            except Exception:
                pass
        except Exception:
            _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)
        await self._sync_listing()
        return True

    async def refresh(self) -> None:
        try:
            workspace_index.normalize_workspaces()
        except Exception:
            _log.debug("failed to normalize webspace manifests before refresh", exc_info=True)
        await self._sync_listing()


async def _seed_webspace_from_scenario(webspace_id: str, scenario_id: str, *, dev: Optional[bool] = None) -> None:
    """
    Seed a webspace YDoc from the given scenario package using the standard
    ScenarioManager.sync_to_yjs* projection path, falling back to static
    seeds inside ensure_webspace_seeded_from_scenario when needed.
    """
    ystore = get_ystore_for_webspace(webspace_id)
    source_mode = "workspace"
    if dev is None:
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                dev = row.is_dev
                source_mode = row.effective_source_mode
            else:
                dev = False
        except Exception:
            dev = False
    elif dev:
        source_mode = "dev"
    _log.debug("seeding webspace=%s scenario=%s dev=%s", webspace_id, scenario_id, dev)
    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=scenario_id or "web_desktop",
            space=source_mode,
        )
    except Exception:
        _log.warning("failed to seed webspace=%s from scenario=%s", webspace_id, scenario_id, exc_info=True)


# --- event subscriptions (core-level) -----------------------------------


@subscribe("scenarios.synced")
async def _on_scenarios_synced(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI for a webspace when its scenario has been projected
    into YDoc by ScenarioManager.sync_to_yjs*.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    scenario_id = str(evt.get("scenario_id") or "").strip() or None
    await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_projection_sync",
        scenario_id=scenario_id,
        scenario_resolution="projected_payload",
        source_of_truth="scenario_projection",
    )


@subscribe("skills.activated")
async def _on_skill_activated(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI for the target webspace when a skill is activated.

    For MVP we only rebuild the webspace explicitly referenced in the event
    (or the default webspace), not all workspaces.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    await rebuild_webspace_from_sources(
        webspace_id,
        action="skill_activation_sync",
        source_of_truth="skill_runtime",
    )


@subscribe("skills.rolledback")
async def _on_skill_rolled_back(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI when a skill is rolled back so that its catalog
    entries and registry contributions are removed from the target webspace.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    await rebuild_webspace_from_sources(
        webspace_id,
        action="skill_rollback_sync",
        source_of_truth="skill_runtime",
    )


@subscribe("desktop.webspace.create")
async def _on_webspace_create(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    _log.debug("desktop.webspace.create payload=%s", payload)
    requested = payload.get("id") or payload.get("webspace_id")
    title = payload.get("title")
    scenario_id = str(payload.get("scenario_id") or "web_desktop")
    dev = bool(payload.get("dev"))
    svc = WebspaceService(get_ctx())
    await svc.create(str(requested) if requested is not None else None, str(title) if title is not None else None, scenario_id=scenario_id, dev=dev)


@subscribe("desktop.webspace.rename")
async def _on_webspace_rename(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    title = str(payload.get("title") or "").strip()
    if not webspace_id or not title:
        return
    svc = WebspaceService(get_ctx())
    await svc.rename(webspace_id, title)


@subscribe("desktop.webspace.update")
async def _on_webspace_update(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or payload.get("webspace_id") or "").strip()
    if not webspace_id:
        return
    title = str(payload.get("title") or "").strip() or None
    home_scenario = str(payload.get("home_scenario") or payload.get("scenario_id") or "").strip() or None
    svc = WebspaceService(get_ctx())
    await svc.update_metadata(
        webspace_id,
        title=title,
        home_scenario=home_scenario,
    )


@subscribe("desktop.webspace.delete")
async def _on_webspace_delete(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    svc = WebspaceService(get_ctx())
    await svc.delete(webspace_id)


@subscribe("desktop.webspace.refresh")
async def _on_webspace_refresh(evt: Dict[str, Any]) -> None:  # noqa: ARG001
    svc = WebspaceService(get_ctx())
    await svc.refresh()


async def rebuild_webspace_from_sources(
    webspace_id: str,
    *,
    action: str = "rebuild",
    scenario_id: str | None = None,
    scenario_resolution: str | None = None,
    source_of_truth: str = "current_runtime",
    reseed_from_scenario: bool = False,
    event_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Single semantic rebuild primitive for the current runtime.

    Phase 3 keeps the existing storage and frontend contracts intact, but
    routes reload/reset/restore-style operations through one backend-owned
    materialization step so reconcile behaviour is explicit.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    rebuild_started = time.perf_counter()
    timings_ms: Dict[str, float] = {}
    requested_action = str(action or "").strip().lower() or "rebuild"
    target_scenario = str(scenario_id or "").strip() or None
    status_started_at = time.time()
    previous_status = describe_webspace_rebuild_state(webspace_id)
    effective_switch_timings = _copy_timing_map(switch_timings_ms) or _copy_timing_map(previous_status.get("switch_timings_ms"))
    effective_switch_mode = str(switch_mode or previous_status.get("switch_mode") or "").strip() or None
    _set_webspace_rebuild_status(
        webspace_id,
        status="running",
        pending=True,
        background=bool(previous_status.get("background")),
        request_id=request_id,
        action=requested_action,
        source_of_truth=source_of_truth,
        scenario_id=target_scenario,
        scenario_resolution=scenario_resolution,
        switch_mode=effective_switch_mode,
        requested_at=previous_status.get("requested_at") or status_started_at,
        started_at=status_started_at,
        finished_at=None,
        error=None,
        projection_refresh=None,
        registry_summary=None,
        resolver=None,
        apply_summary=None,
        timings_ms=None,
        switch_timings_ms=effective_switch_timings,
        semantic_rebuild_timings_ms=None,
        phase_timings_ms=None,
    )

    if reseed_from_scenario:
        if not target_scenario:
            raise ValueError("scenario_id is required when reseed_from_scenario is enabled")
        stage_started = time.perf_counter()
        try:
            async with async_get_ydoc(webspace_id) as ydoc:
                ui_map = ydoc.get_map("ui")
                with ydoc.begin_transaction() as txn:
                    ui_map.set(txn, "current_scenario", target_scenario)
        except Exception:
            pass
        _record_timing(timings_ms, "reseed_pointer", stage_started)

        stage_started = time.perf_counter()
        try:
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="workspace")
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="dev")
        except Exception:
            pass
        _record_timing(timings_ms, "invalidate_loader_cache", stage_started)

        stage_started = time.perf_counter()
        try:
            from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
            from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

            try:
                await reset_live_webspace_room(
                    webspace_id,
                    close_reason="webspace_reset" if requested_action == "reset" else "webspace_reload",
                )
            except Exception:
                pass
            try:
                reset_ystore_for_webspace(webspace_id)
            except Exception:
                pass
        except Exception:
            _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)
        _record_timing(timings_ms, "reset_runtime_state", stage_started)

        stage_started = time.perf_counter()
        await _seed_webspace_from_scenario(webspace_id, target_scenario)
        _record_timing(timings_ms, "seed_from_scenario", stage_started)

        stage_started = time.perf_counter()
        await _sync_webspace_listing()
        _record_timing(timings_ms, "sync_listing", stage_started)

    ctx = get_ctx()
    stage_started = time.perf_counter()
    projection_refresh = await _refresh_projection_rules_for_rebuild(
        ctx,
        webspace_id,
        scenario_id=target_scenario,
    )
    _record_timing(timings_ms, "projection_refresh", stage_started)
    runtime = WebspaceScenarioRuntime(ctx)
    try:
        stage_started = time.perf_counter()
        entry = await runtime.rebuild_webspace_async(webspace_id)
        _record_timing(timings_ms, "semantic_rebuild", stage_started)
    except Exception:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=rebuild_started)
        semantic_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_timings_ms", None))
        resolver_debug = dict(getattr(runtime, "_last_resolver_debug", None) or {})
        apply_summary = dict(getattr(runtime, "_last_apply_summary", None) or {})
        phase_timings = _derive_phase_timings(
            switch_timings_ms=effective_switch_timings,
            rebuild_timings_ms=finalized_timings,
            switch_mode=effective_switch_mode,
        )
        _set_webspace_rebuild_status_if_current(
            webspace_id,
            request_id,
            status="failed",
            pending=False,
            finished_at=time.time(),
            error="webspace_rebuild_failed",
            switch_mode=effective_switch_mode,
            projection_refresh=projection_refresh,
            resolver=resolver_debug or None,
            apply_summary=apply_summary or None,
            timings_ms=finalized_timings,
            switch_timings_ms=effective_switch_timings,
            semantic_rebuild_timings_ms=semantic_timings,
            phase_timings_ms=phase_timings,
        )
        _log.warning(
            "failed to rebuild webspace from sources webspace=%s action=%s scenario=%s timings_ms=%s semantic_timings_ms=%s",
            webspace_id,
            requested_action,
            target_scenario,
            finalized_timings,
            semantic_timings,
            exc_info=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "action": requested_action,
            "source_of_truth": source_of_truth,
            "webspace_id": webspace_id,
            "scenario_id": target_scenario,
            "scenario_resolution": scenario_resolution,
            "request_id": request_id,
            "switch_mode": effective_switch_mode,
            "projection_refresh": projection_refresh,
            "resolver": resolver_debug or None,
            "apply_summary": apply_summary or None,
            "timings_ms": finalized_timings,
            "switch_timings_ms": effective_switch_timings,
            "semantic_rebuild_timings_ms": semantic_timings,
            "phase_timings_ms": phase_timings,
            "error": "webspace_rebuild_failed",
        }

    semantic_timings = _copy_timing_map(getattr(runtime, "_last_rebuild_timings_ms", None))
    resolver_debug = dict(getattr(runtime, "_last_resolver_debug", None) or {})
    apply_summary = dict(getattr(runtime, "_last_apply_summary", None) or {})

    if not target_scenario:
        stage_started = time.perf_counter()
        try:
            state_after = await describe_webspace_operational_state(webspace_id)
            target_scenario = state_after.current_scenario or state_after.effective_home_scenario
        except Exception:
            target_scenario = None
        _record_timing(timings_ms, "resolve_active_scenario", stage_started)

    should_sync_workflow = requested_action in {"scenario_switch_rebuild", "restore"}
    if target_scenario and should_sync_workflow:
        stage_started = time.perf_counter()
        try:
            wf = ScenarioWorkflowRuntime(ctx)
            await wf.sync_workflow_for_webspace(target_scenario, webspace_id)
        except Exception:
            _log.warning(
                "failed to sync workflow during semantic rebuild webspace=%s scenario=%s action=%s",
                webspace_id,
                target_scenario,
                requested_action,
                exc_info=True,
            )
        _record_timing(timings_ms, "workflow_sync", stage_started)

    event_topic = None
    if requested_action in {"reload", "reset"}:
        event_topic = "desktop.webspace.reloaded"
    elif requested_action == "restore":
        event_topic = "desktop.webspace.restored"
    if event_topic:
        stage_started = time.perf_counter()
        try:
            payload: dict[str, Any] = {
                "webspace_id": webspace_id,
                "action": requested_action,
            }
            if target_scenario:
                payload["scenario_id"] = target_scenario
            if isinstance(event_payload, dict):
                payload.update(event_payload)
            emit(ctx.bus, event_topic, payload, "scenario.webspace_runtime")
        except Exception:
            _log.debug("failed to emit %s for webspace=%s", event_topic, webspace_id, exc_info=True)
        _record_timing(timings_ms, "event_emit", stage_started)

    finalized_timings = _finalize_timing_map(timings_ms, started_at=rebuild_started)
    phase_timings = _derive_phase_timings(
        switch_timings_ms=effective_switch_timings,
        rebuild_timings_ms=finalized_timings,
        switch_mode=effective_switch_mode,
    )
    result = {
        "ok": True,
        "accepted": True,
        "action": requested_action,
        "source_of_truth": source_of_truth,
        "webspace_id": webspace_id,
        "scenario_id": target_scenario,
        "scenario_resolution": scenario_resolution,
        "request_id": request_id,
        "switch_mode": effective_switch_mode,
        "projection_refresh": projection_refresh,
        "registry_summary": {
            "scenario_id": str(getattr(entry, "scenario_id", target_scenario) or ""),
            "apps": len(getattr(entry, "apps", []) or []),
            "widgets": len(getattr(entry, "widgets", []) or []),
        },
        "resolver": resolver_debug or None,
        "apply_summary": apply_summary or None,
        "timings_ms": finalized_timings,
        "switch_timings_ms": effective_switch_timings,
        "semantic_rebuild_timings_ms": semantic_timings,
        "phase_timings_ms": phase_timings,
    }
    _set_webspace_rebuild_status_if_current(
        webspace_id,
        request_id,
        status="ready",
        pending=False,
        finished_at=time.time(),
        error=None,
        switch_mode=effective_switch_mode,
        scenario_id=target_scenario,
        projection_refresh=projection_refresh,
        registry_summary=result.get("registry_summary"),
        resolver=resolver_debug or None,
        apply_summary=apply_summary or None,
        timings_ms=finalized_timings,
        switch_timings_ms=effective_switch_timings,
        semantic_rebuild_timings_ms=semantic_timings,
        phase_timings_ms=phase_timings,
    )
    _log.info(
        "semantic rebuild completed webspace=%s action=%s scenario=%s timings_ms=%s semantic_timings_ms=%s",
        webspace_id,
        requested_action,
        target_scenario,
        finalized_timings,
        semantic_timings,
    )
    return result


async def _complete_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
    request_id: str | None = None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_switch_rebuild",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        source_of_truth="scenario_switch",
        reseed_from_scenario=False,
        request_id=request_id,
        switch_mode=switch_mode,
        switch_timings_ms=switch_timings_ms,
    )


def _schedule_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
    switch_mode: str | None = None,
    switch_timings_ms: Mapping[str, Any] | None = None,
) -> None:
    request_id = secrets.token_hex(8)
    initial_phase_timings = _derive_phase_timings(
        switch_timings_ms=switch_timings_ms,
        rebuild_timings_ms=None,
        switch_mode=switch_mode,
    )
    _set_webspace_rebuild_status(
        webspace_id,
        status="scheduled",
        pending=True,
        background=True,
        request_id=request_id,
        action="scenario_switch_rebuild",
        source_of_truth="scenario_switch",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        switch_mode=str(switch_mode or "") or None,
        requested_at=time.time(),
        started_at=None,
        finished_at=None,
        error=None,
        projection_refresh=None,
        registry_summary=None,
        resolver=None,
        apply_summary=None,
        timings_ms=None,
        switch_timings_ms=_copy_timing_map(switch_timings_ms),
        semantic_rebuild_timings_ms=None,
        phase_timings_ms=initial_phase_timings,
    )
    existing = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
    if existing and not existing.done():
        existing.cancel()

    async def _runner() -> None:
        try:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="running",
                pending=True,
                background=True,
                switch_mode=str(switch_mode or "") or None,
                started_at=time.time(),
                finished_at=None,
                error=None,
                projection_refresh=None,
                registry_summary=None,
                resolver=None,
                apply_summary=None,
                timings_ms=None,
                semantic_rebuild_timings_ms=None,
            )
            result = await _complete_scenario_switch_rebuild(
                webspace_id,
                scenario_id=scenario_id,
                scenario_resolution=scenario_resolution,
                request_id=request_id,
                switch_mode=switch_mode,
                switch_timings_ms=None,
            )
            if not bool(result.get("accepted")):
                _set_webspace_rebuild_status_if_current(
                    webspace_id,
                    request_id,
                    status="failed",
                    pending=False,
                    background=True,
                    finished_at=time.time(),
                    error=str(result.get("error") or "scenario_switch_rebuild_failed"),
                    switch_mode=str(switch_mode or "") or None,
                    projection_refresh=result.get("projection_refresh"),
                    resolver=result.get("resolver"),
                    apply_summary=result.get("apply_summary"),
                    timings_ms=_copy_timing_map(result.get("timings_ms")),
                    switch_timings_ms=_copy_timing_map(result.get("switch_timings_ms") or switch_timings_ms),
                    semantic_rebuild_timings_ms=_copy_timing_map(result.get("semantic_rebuild_timings_ms")),
                    phase_timings_ms=_copy_timing_map(result.get("phase_timings_ms")),
                )
                _log.warning(
                    "background scenario switch rebuild rejected webspace=%s scenario=%s error=%s",
                    webspace_id,
                    scenario_id,
                    result.get("error"),
                )
        except asyncio.CancelledError:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="cancelled",
                pending=False,
                background=True,
                finished_at=time.time(),
                error="cancelled",
            )
            raise
        except Exception:
            _set_webspace_rebuild_status_if_current(
                webspace_id,
                request_id,
                status="failed",
                pending=False,
                background=True,
                finished_at=time.time(),
                error="background_scenario_switch_rebuild_failed",
            )
            _log.warning(
                "background scenario switch rebuild failed webspace=%s scenario=%s",
                webspace_id,
                scenario_id,
                exc_info=True,
            )
        finally:
            current = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
            if current is task:
                _SCENARIO_SWITCH_REBUILD_TASKS.pop(webspace_id, None)

    task = asyncio.create_task(
        _runner(),
        name=f"webspace-scenario-switch:{webspace_id}:{scenario_id}",
    )
    _SCENARIO_SWITCH_REBUILD_TASKS[webspace_id] = task


async def reload_webspace_from_scenario(
    webspace_id: str,
    *,
    scenario_id: str | None = None,
    action: str = "reload",
) -> dict[str, Any]:
    """
    Re-seed a single webspace from its current or explicit scenario source and
    rebuild its effective UI/runtime projection.

    This is the explicit operator-facing sync recovery path used by event
    handlers as well as local control API endpoints.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    state, scenario_id, scenario_resolution = await _resolve_reload_scenario_target(webspace_id, scenario_id)

    verb = "resetting" if str(action or "").strip().lower() == "reset" else "reloading"
    _log.info(
        "%s webspace %s from scenario %s (resolution=%s kind=%s source_mode=%s current=%s home=%s)",
        verb,
        webspace_id,
        scenario_id,
        scenario_resolution,
        state.kind,
        state.source_mode,
        state.current_scenario,
        state.effective_home_scenario,
    )

    result = await rebuild_webspace_from_sources(
        webspace_id,
        action="reset" if verb == "resetting" else "reload",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        source_of_truth="scenario",
        reseed_from_scenario=True,
    )
    result.update(
        {
            "kind": state.kind,
            "source_mode": state.source_mode,
            "home_scenario": state.effective_home_scenario,
            "current_scenario_before": state.current_scenario,
        }
    )
    return result


async def restore_webspace_from_snapshot(webspace_id: str) -> dict[str, Any]:
    """
    Restore a webspace from its latest persisted YStore snapshot and reconcile
    its materialized effective UI/runtime projection.
    """
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")

    from adaos.services.yjs.gateway import reset_live_webspace_room  # pylint: disable=import-outside-toplevel
    from adaos.services.yjs.store import restore_ystore_for_webspace  # pylint: disable=import-outside-toplevel

    restore_result = await restore_ystore_for_webspace(webspace_id)
    if not bool(restore_result.get("accepted")):
        return restore_result

    reset_result: dict[str, Any] = {}
    try:
        reset_result = await reset_live_webspace_room(webspace_id, close_reason="webspace_restore")
    except Exception:
        _log.warning("failed to reset live room before restore for webspace=%s", webspace_id, exc_info=True)

    rebuild_result = await rebuild_webspace_from_sources(
        webspace_id,
        action="restore",
        source_of_truth="snapshot",
        reseed_from_scenario=False,
        event_payload={"snapshot_path": str(restore_result.get("snapshot_path") or "")},
    )

    return {
        **restore_result,
        **rebuild_result,
        "action": "restore",
        "source_of_truth": "snapshot",
        "reset_room": reset_result,
    }


async def switch_webspace_scenario(
    webspace_id: str,
    scenario_id: str,
    *,
    set_home: bool | None = None,
    wait_for_rebuild: bool = True,
) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    scenario_id = str(scenario_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    if not scenario_id:
        raise ValueError("scenario_id is required")

    switch_started = time.perf_counter()
    timings_ms: Dict[str, float] = {}
    stage_started = time.perf_counter()
    state_before = await describe_webspace_operational_state(webspace_id)
    _record_timing(timings_ms, "describe_state_before", stage_started)

    stage_started = time.perf_counter()
    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    resolved_set_home = bool(set_home) if set_home is not None else bool(row.is_dev or row.effective_source_mode == "dev")
    _record_timing(timings_ms, "resolve_manifest_policy", stage_started)
    stage_started = time.perf_counter()
    rebuild_state_before = describe_webspace_rebuild_state(webspace_id)
    _record_timing(timings_ms, "describe_rebuild_before", stage_started)

    _log.info(
        "desktop.scenario.set webspace=%s scenario=%s requested_set_home=%s resolved_set_home=%s",
        webspace_id,
        scenario_id,
        set_home,
        resolved_set_home,
    )
    switch_mode = "pointer_first" if _pointer_first_scenario_switch_enabled() else "materialize_and_copy"
    loader_space = "workspace"
    try:
        if row:
            loader_space = row.effective_source_mode
    except Exception:
        loader_space = "workspace"
    switch_content: Dict[str, Any] | None = None

    def _build_switch_skip_result(*, skip_reason: str, rebuild_state: Mapping[str, Any], background_rebuild: bool) -> dict[str, Any]:
        phase_timings = _copy_timing_map(rebuild_state.get("phase_timings_ms"))
        if not phase_timings:
            phase_timings = _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                rebuild_timings_ms=_copy_timing_map(rebuild_state.get("timings_ms")),
                switch_mode="noop",
            )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "kind": row.effective_kind,
            "source_mode": row.effective_source_mode,
            "current_scenario_before": state_before.current_scenario,
            "home_scenario_before": state_before.effective_home_scenario,
            "home_scenario": row.effective_home_scenario,
            "set_home": resolved_set_home,
            "background_rebuild": background_rebuild,
            "scenario_switch_mode": switch_mode,
            "switch_skipped": True,
            "skip_reason": skip_reason,
            "timings_ms": finalized_timings,
            "rebuild_timings_ms": _copy_timing_map(rebuild_state.get("timings_ms")),
            "semantic_rebuild_timings_ms": _copy_timing_map(rebuild_state.get("semantic_rebuild_timings_ms")),
            "resolver": dict(rebuild_state.get("resolver") or {})
            if isinstance(rebuild_state.get("resolver"), Mapping)
            else None,
            "apply_summary": dict(rebuild_state.get("apply_summary") or {})
            if isinstance(rebuild_state.get("apply_summary"), Mapping)
            else None,
            "phase_timings_ms": phase_timings,
        }

    if (
        str(state_before.current_scenario or "").strip() == scenario_id
        and not bool(rebuild_state_before.get("pending"))
        and str(rebuild_state_before.get("status") or "").strip().lower() == "ready"
        and str(rebuild_state_before.get("scenario_id") or "").strip() == scenario_id
    ):
        if resolved_set_home and row.effective_home_scenario != scenario_id:
            stage_started = time.perf_counter()
            row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
            _record_timing(timings_ms, "persist_home_scenario", stage_started)

            stage_started = time.perf_counter()
            await _sync_webspace_listing()
            _record_timing(timings_ms, "sync_listing", stage_started)

        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _log.info(
            "desktop.scenario.set skipped webspace=%s scenario=%s mode=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            finalized_timings,
        )
        return _build_switch_skip_result(
            skip_reason="already_current_ready",
            rebuild_state=rebuild_state_before,
            background_rebuild=False,
        )

    if (
        str(state_before.current_scenario or "").strip() == scenario_id
        and bool(rebuild_state_before.get("pending"))
        and str(rebuild_state_before.get("scenario_id") or "").strip() == scenario_id
    ):
        if resolved_set_home and row.effective_home_scenario != scenario_id:
            stage_started = time.perf_counter()
            row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
            _record_timing(timings_ms, "persist_home_scenario", stage_started)

            stage_started = time.perf_counter()
            await _sync_webspace_listing()
            _record_timing(timings_ms, "sync_listing", stage_started)

        if wait_for_rebuild:
            existing_task = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
            if existing_task and not existing_task.done():
                stage_started = time.perf_counter()
                try:
                    await asyncio.shield(existing_task)
                except Exception:
                    pass
                _record_timing(timings_ms, "wait_existing_rebuild", stage_started)
                rebuild_state_before = describe_webspace_rebuild_state(webspace_id)

        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _log.info(
            "desktop.scenario.set deduplicated webspace=%s scenario=%s mode=%s pending=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            bool(rebuild_state_before.get("pending")),
            finalized_timings,
        )
        return _build_switch_skip_result(
            skip_reason="already_pending_rebuild",
            rebuild_state=rebuild_state_before,
            background_rebuild=bool(rebuild_state_before.get("pending") or (not wait_for_rebuild and rebuild_state_before.get("background"))),
        )

    stage_started = time.perf_counter()
    scenario_exists = _scenario_exists_for_switch(scenario_id, space=loader_space)
    _record_timing(timings_ms, "validate_scenario", stage_started)
    if not scenario_exists:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            background=not wait_for_rebuild,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch",
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            requested_at=time.time(),
            finished_at=time.time(),
            error="scenario_not_found",
            projection_refresh=None,
            registry_summary=None,
            resolver=None,
            apply_summary=None,
            timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_not_found",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    if switch_mode != "pointer_first":
        stage_started = time.perf_counter()
        switch_content = _load_scenario_switch_content(scenario_id, space=loader_space)
        _record_timing(timings_ms, "load_scenario", stage_started)
        if not isinstance(switch_content, dict) or not switch_content:
            _log.warning("desktop.scenario.set: no scenario.json for %s", scenario_id)
            finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
            _set_webspace_rebuild_status(
                webspace_id,
                status="failed",
                pending=False,
                background=not wait_for_rebuild,
                action="scenario_switch_rebuild",
                source_of_truth="scenario_switch",
                scenario_id=scenario_id,
                scenario_resolution="explicit",
                switch_mode=switch_mode,
                requested_at=time.time(),
                finished_at=time.time(),
                error="scenario_not_found",
                projection_refresh=None,
                registry_summary=None,
                resolver=None,
                apply_summary=None,
                timings_ms=finalized_timings,
                phase_timings_ms=_derive_phase_timings(
                    switch_timings_ms=finalized_timings,
                    switch_mode=switch_mode,
                ),
            )
            return {
                "ok": False,
                "accepted": False,
                "error": "scenario_not_found",
                "webspace_id": webspace_id,
                "scenario_id": scenario_id,
                "scenario_switch_mode": switch_mode,
                "timings_ms": finalized_timings,
                "phase_timings_ms": _derive_phase_timings(
                    switch_timings_ms=finalized_timings,
                    switch_mode=switch_mode,
                ),
            }

    try:
        stage_started = time.perf_counter()
        async with async_get_ydoc(webspace_id) as ydoc:
            _record_timing(timings_ms, "open_doc", stage_started)
            if switch_mode == "pointer_first":
                ui_map = ydoc.get_map("ui")
                stage_started = time.perf_counter()
                with ydoc.begin_transaction() as txn:
                    _set_map_value_if_changed(ui_map, txn, "current_scenario", scenario_id)
                _record_timing(timings_ms, "write_switch_pointer", stage_started)
            else:
                stage_started = time.perf_counter()
                _materialize_scenario_switch_content_in_doc(
                    ydoc,
                    scenario_id=scenario_id,
                    content=switch_content or {},
                )
                _record_timing(timings_ms, "materialize_switch_payload", stage_started)
    except Exception:
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            background=not wait_for_rebuild,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch",
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            requested_at=time.time(),
            finished_at=time.time(),
            error="scenario_switch_failed",
            projection_refresh=None,
            registry_summary=None,
            resolver=None,
            apply_summary=None,
            timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        _log.warning(
            "failed to switch scenario for webspace=%s scenario=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            finalized_timings,
            exc_info=True,
        )
        return {
            "ok": False,
            "accepted": False,
            "error": "scenario_switch_failed",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    stage_started = time.perf_counter()
    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    _record_timing(timings_ms, "refresh_manifest_row", stage_started)
    if resolved_set_home:
        stage_started = time.perf_counter()
        row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
        _record_timing(timings_ms, "persist_home_scenario", stage_started)

        stage_started = time.perf_counter()
        await _sync_webspace_listing()
        _record_timing(timings_ms, "sync_listing", stage_started)

    if not wait_for_rebuild:
        scheduled_switch_timings = _finalize_timing_map(dict(timings_ms), started_at=switch_started)
        stage_started = time.perf_counter()
        _schedule_scenario_switch_rebuild(
            webspace_id,
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            switch_mode=switch_mode,
            switch_timings_ms=scheduled_switch_timings,
        )
        _record_timing(timings_ms, "schedule_background_rebuild", stage_started)
        finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        current_status = describe_webspace_rebuild_state(webspace_id)
        _set_webspace_rebuild_status_if_current(
            webspace_id,
            str(current_status.get("request_id") or "").strip() or None,
            switch_timings_ms=finalized_timings,
            phase_timings_ms=_derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        )
        _log.info(
            "desktop.scenario.set accepted webspace=%s scenario=%s mode=%s background=%s timings_ms=%s",
            webspace_id,
            scenario_id,
            switch_mode,
            True,
            finalized_timings,
        )
        return {
            "ok": True,
            "accepted": True,
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "kind": row.effective_kind,
            "source_mode": row.effective_source_mode,
            "current_scenario_before": state_before.current_scenario,
            "home_scenario_before": state_before.effective_home_scenario,
            "home_scenario": row.effective_home_scenario,
            "set_home": resolved_set_home,
            "background_rebuild": True,
            "scenario_switch_mode": switch_mode,
            "timings_ms": finalized_timings,
            "phase_timings_ms": _derive_phase_timings(
                switch_timings_ms=finalized_timings,
                switch_mode=switch_mode,
            ),
        }

    stage_started = time.perf_counter()
    rebuild_result = await _complete_scenario_switch_rebuild(
        webspace_id,
        scenario_id=scenario_id,
        scenario_resolution="explicit",
        switch_mode=switch_mode,
        switch_timings_ms=_finalize_timing_map(dict(timings_ms), started_at=switch_started),
    )
    _record_timing(timings_ms, "wait_rebuild", stage_started)
    if not bool(rebuild_result.get("accepted")):
        final_switch_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
        rebuild_result["switch_timings_ms"] = final_switch_timings
        rebuild_result["phase_timings_ms"] = _derive_phase_timings(
            switch_timings_ms=final_switch_timings,
            rebuild_timings_ms=rebuild_result.get("timings_ms"),
            switch_mode=switch_mode,
        )
        return rebuild_result

    finalized_timings = _finalize_timing_map(timings_ms, started_at=switch_started)
    phase_timings = _derive_phase_timings(
        switch_timings_ms=finalized_timings,
        rebuild_timings_ms=rebuild_result.get("timings_ms"),
        switch_mode=switch_mode,
    )
    _log.info(
        "desktop.scenario.set completed webspace=%s scenario=%s mode=%s background=%s timings_ms=%s rebuild_timings_ms=%s",
        webspace_id,
        scenario_id,
        switch_mode,
        False,
        finalized_timings,
        rebuild_result.get("timings_ms"),
    )
    return {
        "ok": True,
        "accepted": True,
        "webspace_id": webspace_id,
        "scenario_id": scenario_id,
        "kind": row.effective_kind,
        "source_mode": row.effective_source_mode,
        "current_scenario_before": state_before.current_scenario,
        "home_scenario_before": state_before.effective_home_scenario,
        "home_scenario": row.effective_home_scenario,
        "set_home": resolved_set_home,
        "background_rebuild": False,
        "scenario_switch_mode": switch_mode,
        "timings_ms": finalized_timings,
        "rebuild_timings_ms": _copy_timing_map(rebuild_result.get("timings_ms")),
        "semantic_rebuild_timings_ms": _copy_timing_map(rebuild_result.get("semantic_rebuild_timings_ms")),
        "resolver": dict(rebuild_result.get("resolver") or {})
        if isinstance(rebuild_result.get("resolver"), Mapping)
        else None,
        "apply_summary": dict(rebuild_result.get("apply_summary") or {})
        if isinstance(rebuild_result.get("apply_summary"), Mapping)
        else None,
        "phase_timings_ms": phase_timings,
    }


async def go_home_webspace(webspace_id: str, *, wait_for_rebuild: bool = True) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    state = await describe_webspace_operational_state(webspace_id)
    result = await switch_webspace_scenario(
        webspace_id,
        state.effective_home_scenario,
        set_home=False,
        wait_for_rebuild=wait_for_rebuild,
    )
    result["action"] = "go_home"
    result["source_of_truth"] = "manifest_home_scenario"
    result["scenario_resolution"] = "manifest_home"
    return result


async def set_current_webspace_home(webspace_id: str) -> dict[str, Any]:
    webspace_id = str(webspace_id or "").strip()
    if not webspace_id:
        raise ValueError("webspace_id is required")
    state = await describe_webspace_operational_state(webspace_id)
    scenario_id = str(state.current_scenario or "").strip()
    if not scenario_id:
        return {
            "ok": False,
            "accepted": False,
            "action": "set_home_current",
            "source_of_truth": "current_scenario",
            "webspace_id": webspace_id,
            "scenario_id": None,
            "current_scenario": None,
            "home_scenario_before": state.effective_home_scenario,
            "error": "current_scenario_unavailable",
        }
    svc = WebspaceService(get_ctx())
    info = await svc.set_home_scenario(webspace_id, scenario_id)
    if info is None:
        return {
            "ok": False,
            "accepted": False,
            "action": "set_home_current",
            "source_of_truth": "current_scenario",
            "webspace_id": webspace_id,
            "scenario_id": scenario_id,
            "current_scenario": scenario_id,
            "home_scenario_before": state.effective_home_scenario,
            "error": "webspace_not_found",
        }
    return {
        "ok": True,
        "accepted": True,
        "action": "set_home_current",
        "source_of_truth": "current_scenario",
        "webspace_id": info.id,
        "scenario_id": scenario_id,
        "current_scenario": scenario_id,
        "home_scenario_before": state.effective_home_scenario,
        "home_scenario": info.home_scenario,
        "changed": str(state.effective_home_scenario or "").strip() != str(info.home_scenario or "").strip(),
        "kind": info.kind,
        "source_mode": info.source_mode,
    }


async def ensure_dev_webspace_for_scenario(
    scenario_id: str,
    *,
    requested_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    scenario_id = str(scenario_id or "").strip()
    if not scenario_id:
        raise ValueError("scenario_id is required")
    svc = WebspaceService(get_ctx())
    info, created = await svc.ensure_dev_for_scenario(
        scenario_id,
        requested_id=requested_id,
        title=title,
    )
    return {
        "ok": True,
        "accepted": True,
        "created": created,
        "webspace_id": info.id,
        "scenario_id": scenario_id,
        "home_scenario": info.home_scenario,
        "kind": info.kind,
        "source_mode": info.source_mode,
    }


async def reload_preview_webspaces_for_project(
    object_type: str,
    object_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    object_type = str(object_type or "").strip().lower()
    object_id = str(object_id or "").strip()
    if object_type not in {"scenario", "skill"} or not object_id:
        return {
            "ok": False,
            "accepted": False,
            "error": "project_identity_required",
        }

    targets: list[tuple[str, str]] = []
    for row in workspace_index.list_workspaces():
        if not row.is_dev:
            continue
        home_scenario = str(row.effective_home_scenario or "").strip()
        if not home_scenario:
            continue
        if object_type == "scenario":
            if home_scenario == object_id:
                targets.append((row.workspace_id, home_scenario))
            continue
        try:
            manifest = scenarios_loader.read_manifest(home_scenario, space=row.effective_source_mode)
            depends_raw = manifest.get("depends") or []
            depends = {
                str(item).strip()
                for item in depends_raw
                if str(item).strip()
            }
            if object_id in depends:
                targets.append((row.workspace_id, home_scenario))
        except Exception:
            _log.debug(
                "failed to resolve scenario depends for preview webspace=%s home=%s",
                row.workspace_id,
                home_scenario,
                exc_info=True,
            )

    reloaded: list[str] = []
    failed: list[str] = []
    for webspace_id, scenario_id in targets:
        try:
            await reload_webspace_from_scenario(
                webspace_id,
                scenario_id=scenario_id,
                action="reload",
            )
            reloaded.append(webspace_id)
        except Exception:
            failed.append(webspace_id)
            _log.warning(
                "failed to reload preview webspace=%s for %s:%s reason=%s",
                webspace_id,
                object_type,
                object_id,
                reason,
                exc_info=True,
            )

    return {
        "ok": not failed,
        "accepted": bool(targets),
        "object_type": object_type,
        "object_id": object_id,
        "reason": str(reason or "").strip() or None,
        "reloaded_webspaces": reloaded,
        "failed_webspaces": failed,
    }


@subscribe("desktop.webspace.reload")
async def _on_webspace_reload(evt: Dict[str, Any]) -> None:
    """
    Re-seed the current webspace from its scenario, effectively
    rebuilding ui/data/registry for debugging or recovery.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await reload_webspace_from_scenario(
        webspace_id,
        scenario_id=str(payload.get("scenario_id") or "").strip() or None,
        action="reload",
    )


@subscribe("desktop.webspace.reset")
async def _on_webspace_reset(evt: Dict[str, Any]) -> None:
    """
    Hard reset of the current webspace from its scenario. For now this
    mirrors desktop.webspace.reload behaviour; it is introduced as a
    separate event so that future versions can differentiate between
    soft reload (updatable-only) and full reset.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await reload_webspace_from_scenario(
        webspace_id,
        scenario_id=str(payload.get("scenario_id") or "").strip() or None,
        action="reset",
    )


@subscribe("desktop.webspace.go_home")
async def _on_webspace_go_home(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await go_home_webspace(webspace_id, wait_for_rebuild=False)


@subscribe("desktop.webspace.set_home")
async def _on_webspace_set_home(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    scenario_id = str(payload.get("scenario_id") or "").strip()
    if not webspace_id or not scenario_id:
        return
    svc = WebspaceService(get_ctx())
    await svc.set_home_scenario(webspace_id, scenario_id)


@subscribe("desktop.webspace.set_home_current")
async def _on_webspace_set_home_current(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    if not webspace_id:
        return
    await set_current_webspace_home(webspace_id)


@subscribe("desktop.scenario.set")
async def _on_desktop_scenario_set(evt: Dict[str, Any]) -> None:
    """
    Switch the current desktop scenario for a webspace and re-sync the
    target YDoc from the selected scenario package.

    Payload:
      - scenario_id: id of the desktop scenario (required)
      - webspace_id / workspace_id: optional, defaults to current/default.
    """
    payload = _payload(evt)
    scenario_id = str(payload.get("scenario_id") or "").strip()
    if not scenario_id:
        return
    webspace_id = _webspace_id(payload)
    set_home: bool | None = None
    if "set_home" in payload:
        set_home = bool(payload.get("set_home"))
    elif "persist_home" in payload:
        set_home = bool(payload.get("persist_home"))
    await switch_webspace_scenario(
        webspace_id,
        scenario_id,
        set_home=set_home,
        wait_for_rebuild=False,
    )


@subscribe("prompt.project.changed")
async def _on_prompt_project_changed(evt: Dict[str, Any]) -> None:
    payload = _payload(evt)
    object_type = str(payload.get("object_type") or "").strip().lower()
    object_id = str(payload.get("object_id") or "").strip()
    if object_type not in {"scenario", "skill"} or not object_id:
        return
    await reload_preview_webspaces_for_project(
        object_type,
        object_id,
        reason=str(payload.get("reason") or "").strip() or None,
    )


__all__ = [
    "WebUIRegistryEntry",
    "WebspaceResolverInputs",
    "WebspaceResolverOutputs",
    "WebspaceScenarioRuntime",
    "describe_webspace_operational_state",
    "describe_webspace_overlay_state",
    "describe_webspace_projection_state",
    "describe_webspace_rebuild_state",
    "set_current_webspace_home",
    "rebuild_webspace_from_sources",
]
