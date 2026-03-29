from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from collections.abc import Iterable
import asyncio
import json
import logging
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
        "source_of_truth": str(current.get("source_of_truth") or "") or None,
        "scenario_id": str(current.get("scenario_id") or "") or None,
        "scenario_resolution": str(current.get("scenario_resolution") or "") or None,
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
        "error": str(current.get("error") or "") or None,
    }


class WebspaceScenarioRuntime:
    """
    Core runtime responsible for computing and applying the effective UI
    (application + catalog + registry + installed) for a given webspace.

    It reads:
      - ui.current_scenario,
      - ui.scenarios[scenario_id].application,
      - data.scenarios[scenario_id].catalog,
      - registry.scenarios[scenario_id],
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
            _log.debug("webui.json missing for %s (%s)", skill_name, space)
            return {}
        try:
            # Accept UTF-8 with BOM produced by some Windows/PowerShell editors.
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _log.warning("failed to read webui.json for %s: %s", skill_name, exc)
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

        return {
            "skill": skill_name,
            "space": space,
            "apps": [it for it in apps if isinstance(it, dict)],
            "widgets": [it for it in widgets if isinstance(it, dict)],
            "registry": {
                "modals": ({str(k): v for k, v in reg_modals_raw.items()} if isinstance(reg_modals_raw, dict) else [str(x) for x in reg_modals_raw if isinstance(x, (str, int))]),
                "widgets": (
                    {str(k): v for k, v in reg_widgets_raw.items()} if isinstance(reg_widgets_raw, dict) else [str(x) for x in reg_widgets_raw if isinstance(x, (str, int))]
                ),
            },
            "ydoc_defaults": ydoc_defaults if isinstance(ydoc_defaults, dict) else {},
            "contributions": contributions,
        }

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
        registry_map = ydoc.get_map("registry")

        scenario_id = ui_map.get("current_scenario") or "web_desktop"
        scenarios_ui = _coerce_dict(ui_map.get("scenarios") or {})
        scenario_ui_entry = _coerce_dict(scenarios_ui.get(scenario_id) or {})
        scenario_app_ui = _coerce_dict(scenario_ui_entry.get("application") or {})

        scenarios_data = _coerce_dict(data_map.get("scenarios") or {})
        scenario_entry = _coerce_dict(scenarios_data.get(scenario_id) or {})
        base_catalog = _coerce_dict(scenario_entry.get("catalog") or {})

        scenario_registry_map = _coerce_dict(registry_map.get("scenarios") or {})
        registry_entry = _coerce_dict(scenario_registry_map.get(scenario_id) or {})

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
        )

    def resolve_webspace(self, inputs: WebspaceResolverInputs) -> WebspaceResolverOutputs:
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
                "schema": {
                    "id": "apps_catalog",
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
                "schema": {
                    "id": "widgets_catalog",
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

        return WebspaceResolverOutputs(
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

    def _apply_resolved_state_in_doc(self, ydoc: Y.YDoc, webspace_id: str, resolved: WebspaceResolverOutputs) -> None:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")

        with ydoc.begin_transaction() as txn:
            try:
                self._apply_ydoc_defaults_in_txn(ydoc, txn, resolved.skill_decls)
            except Exception:
                _log.warning("failed to apply ydoc_defaults for webspace=%s", webspace_id, exc_info=True)

            ui_map.set(txn, "application", resolved.application)
            data_map.set(txn, "catalog", resolved.catalog)
            data_map.set(txn, "installed", resolved.installed)
            try:
                data_map.set(txn, "desktop", resolved.desktop)
            except Exception:
                pass
            try:
                data_map.set(txn, "routing", resolved.routing)
            except Exception:
                pass
            registry_map.set(txn, "merged", resolved.registry)

    def _resolve_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebspaceResolverOutputs:
        return self.resolve_webspace(self._collect_resolver_inputs_in_doc(ydoc, webspace_id))

    def _rebuild_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebUIRegistryEntry:
        resolved = self._resolve_in_doc(ydoc, webspace_id)
        self._apply_resolved_state_in_doc(ydoc, webspace_id, resolved)
        entry = resolved.to_registry_entry()

        try:
            _log.debug(
                "rebuilt webspace=%s scenario=%s apps=%d widgets=%d",
                webspace_id,
                resolved.scenario_id,
                len(entry.apps),
                len(entry.widgets),
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
    rows = workspace_index.list_workspaces()
    for row in rows:
        async with async_get_ydoc(row.workspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "webspaces", {"items": listing})


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

    requested_action = str(action or "").strip().lower() or "rebuild"
    target_scenario = str(scenario_id or "").strip() or None
    status_started_at = time.time()
    previous_status = describe_webspace_rebuild_state(webspace_id)
    _set_webspace_rebuild_status(
        webspace_id,
        status="running",
        pending=True,
        background=bool(previous_status.get("background")),
        action=requested_action,
        source_of_truth=source_of_truth,
        scenario_id=target_scenario,
        scenario_resolution=scenario_resolution,
        requested_at=previous_status.get("requested_at") or status_started_at,
        started_at=status_started_at,
        finished_at=None,
        error=None,
        projection_refresh=None,
        registry_summary=None,
    )

    if reseed_from_scenario:
        if not target_scenario:
            raise ValueError("scenario_id is required when reseed_from_scenario is enabled")
        try:
            async with async_get_ydoc(webspace_id) as ydoc:
                ui_map = ydoc.get_map("ui")
                with ydoc.begin_transaction() as txn:
                    ui_map.set(txn, "current_scenario", target_scenario)
        except Exception:
            pass

        try:
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="workspace")
            scenarios_loader.invalidate_cache(scenario_id=target_scenario, space="dev")
        except Exception:
            pass
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

        await _seed_webspace_from_scenario(webspace_id, target_scenario)
        await _sync_webspace_listing()

    ctx = get_ctx()
    projection_refresh = await _refresh_projection_rules_for_rebuild(
        ctx,
        webspace_id,
        scenario_id=target_scenario,
    )
    runtime = WebspaceScenarioRuntime(ctx)
    try:
        entry = await runtime.rebuild_webspace_async(webspace_id)
    except Exception:
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            finished_at=time.time(),
            error="webspace_rebuild_failed",
            projection_refresh=projection_refresh,
        )
        _log.warning(
            "failed to rebuild webspace from sources webspace=%s action=%s scenario=%s",
            webspace_id,
            requested_action,
            target_scenario,
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
            "projection_refresh": projection_refresh,
            "error": "webspace_rebuild_failed",
        }

    if not target_scenario:
        try:
            state_after = await describe_webspace_operational_state(webspace_id)
            target_scenario = state_after.current_scenario or state_after.effective_home_scenario
        except Exception:
            target_scenario = None

    should_sync_workflow = requested_action in {"scenario_switch_rebuild", "restore"}
    if target_scenario and should_sync_workflow:
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

    event_topic = None
    if requested_action in {"reload", "reset"}:
        event_topic = "desktop.webspace.reloaded"
    elif requested_action == "restore":
        event_topic = "desktop.webspace.restored"
    if event_topic:
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

    result = {
        "ok": True,
        "accepted": True,
        "action": requested_action,
        "source_of_truth": source_of_truth,
        "webspace_id": webspace_id,
        "scenario_id": target_scenario,
        "scenario_resolution": scenario_resolution,
        "projection_refresh": projection_refresh,
        "registry_summary": {
            "scenario_id": str(getattr(entry, "scenario_id", target_scenario) or ""),
            "apps": len(getattr(entry, "apps", []) or []),
            "widgets": len(getattr(entry, "widgets", []) or []),
        },
    }
    _set_webspace_rebuild_status(
        webspace_id,
        status="ready",
        pending=False,
        finished_at=time.time(),
        error=None,
        scenario_id=target_scenario,
        projection_refresh=projection_refresh,
        registry_summary=result.get("registry_summary"),
    )
    return result


async def _complete_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
) -> dict[str, Any]:
    return await rebuild_webspace_from_sources(
        webspace_id,
        action="scenario_switch_rebuild",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        source_of_truth="scenario_switch",
        reseed_from_scenario=False,
    )


def _schedule_scenario_switch_rebuild(
    webspace_id: str,
    *,
    scenario_id: str,
    scenario_resolution: str | None,
) -> None:
    _set_webspace_rebuild_status(
        webspace_id,
        status="scheduled",
        pending=True,
        background=True,
        action="scenario_switch_rebuild",
        source_of_truth="scenario_switch",
        scenario_id=scenario_id,
        scenario_resolution=scenario_resolution,
        requested_at=time.time(),
        started_at=None,
        finished_at=None,
        error=None,
    )
    existing = _SCENARIO_SWITCH_REBUILD_TASKS.get(webspace_id)
    if existing and not existing.done():
        existing.cancel()

    async def _runner() -> None:
        try:
            _set_webspace_rebuild_status(
                webspace_id,
                status="running",
                pending=True,
                background=True,
                started_at=time.time(),
                finished_at=None,
                error=None,
            )
            result = await _complete_scenario_switch_rebuild(
                webspace_id,
                scenario_id=scenario_id,
                scenario_resolution=scenario_resolution,
            )
            if not bool(result.get("accepted")):
                _set_webspace_rebuild_status(
                    webspace_id,
                    status="failed",
                    pending=False,
                    background=True,
                    finished_at=time.time(),
                    error=str(result.get("error") or "scenario_switch_rebuild_failed"),
                    projection_refresh=result.get("projection_refresh"),
                )
                _log.warning(
                    "background scenario switch rebuild rejected webspace=%s scenario=%s error=%s",
                    webspace_id,
                    scenario_id,
                    result.get("error"),
                )
        except asyncio.CancelledError:
            _set_webspace_rebuild_status(
                webspace_id,
                status="cancelled",
                pending=False,
                background=True,
                finished_at=time.time(),
                error="cancelled",
            )
            raise
        except Exception:
            _set_webspace_rebuild_status(
                webspace_id,
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

    state_before = await describe_webspace_operational_state(webspace_id)
    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    resolved_set_home = bool(set_home) if set_home is not None else bool(row.is_dev or row.effective_source_mode == "dev")

    _log.info(
        "desktop.scenario.set webspace=%s scenario=%s requested_set_home=%s resolved_set_home=%s",
        webspace_id,
        scenario_id,
        set_home,
        resolved_set_home,
    )

    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            space = "workspace"
            try:
                if row:
                    space = row.effective_source_mode
            except Exception:
                space = "workspace"

            content = _load_scenario_switch_content(scenario_id, space=space)
            if not isinstance(content, dict) or not content:
                _log.warning("desktop.scenario.set: no scenario.json for %s", scenario_id)
                _set_webspace_rebuild_status(
                    webspace_id,
                    status="failed",
                    pending=False,
                    background=not wait_for_rebuild,
                    action="scenario_switch_rebuild",
                    source_of_truth="scenario_switch",
                    scenario_id=scenario_id,
                    scenario_resolution="explicit",
                    requested_at=time.time(),
                    finished_at=time.time(),
                    error="scenario_not_found",
                )
                return {"ok": False, "accepted": False, "error": "scenario_not_found", "webspace_id": webspace_id, "scenario_id": scenario_id}
            ui_section = ((content.get("ui") or {}).get("application")) or {}
            if not isinstance(ui_section, dict):
                ui_section = {}
            registry_section = content.get("registry") or {}
            if not isinstance(registry_section, dict):
                registry_section = {}
            catalog_section = content.get("catalog") or {}
            if not isinstance(catalog_section, dict):
                catalog_section = {}
            data_section = content.get("data") or {}
            if not isinstance(data_section, dict):
                data_section = {}

            ui_map = ydoc.get_map("ui")
            registry_map = ydoc.get_map("registry")
            data_map = ydoc.get_map("data")

            with ydoc.begin_transaction() as txn:
                scenarios_ui_raw = ui_map.get("scenarios")
                scenarios_ui = dict(scenarios_ui_raw) if isinstance(scenarios_ui_raw, Mapping) else {}
                updated_ui = dict(scenarios_ui)
                updated_ui[scenario_id] = {"application": ui_section}
                ui_map.set(txn, "scenarios", updated_ui)
                ui_map.set(txn, "current_scenario", scenario_id)
                ui_map.set(txn, "application", ui_section)

                reg_scenarios_raw = registry_map.get("scenarios")
                reg_scenarios = dict(reg_scenarios_raw) if isinstance(reg_scenarios_raw, Mapping) else {}
                reg_updated = dict(reg_scenarios)
                reg_updated[scenario_id] = registry_section
                registry_map.set(txn, "scenarios", reg_updated)
                registry_map.set(txn, "merged", registry_section)

                data_scenarios_raw = data_map.get("scenarios")
                data_scenarios = dict(data_scenarios_raw) if isinstance(data_scenarios_raw, Mapping) else {}
                data_updated = dict(data_scenarios)
                entry_raw = data_updated.get(scenario_id) or {}
                entry = dict(entry_raw) if isinstance(entry_raw, Mapping) else {}
                entry["catalog"] = catalog_section
                data_updated[scenario_id] = entry
                data_map.set(txn, "scenarios", data_updated)
                data_map.set(txn, "catalog", catalog_section)

                if isinstance(data_section, dict):
                    for key, value in data_section.items():
                        if not isinstance(key, str):
                            continue
                        if key == "installed":
                            continue
                        try:
                            payload_value = json.loads(json.dumps(value))
                        except Exception:
                            payload_value = value
                        data_map.set(txn, key, payload_value)
    except Exception:
        _set_webspace_rebuild_status(
            webspace_id,
            status="failed",
            pending=False,
            background=not wait_for_rebuild,
            action="scenario_switch_rebuild",
            source_of_truth="scenario_switch",
            scenario_id=scenario_id,
            scenario_resolution="explicit",
            requested_at=time.time(),
            finished_at=time.time(),
            error="scenario_switch_failed",
        )
        _log.warning("failed to switch scenario for webspace=%s scenario=%s", webspace_id, scenario_id, exc_info=True)
        return {"ok": False, "accepted": False, "error": "scenario_switch_failed", "webspace_id": webspace_id, "scenario_id": scenario_id}

    row = workspace_index.get_workspace(webspace_id) or workspace_index.ensure_workspace(webspace_id)
    if resolved_set_home:
        row = workspace_index.set_workspace_manifest(webspace_id, home_scenario=scenario_id)
        await _sync_webspace_listing()

    if not wait_for_rebuild:
        _schedule_scenario_switch_rebuild(
            webspace_id,
            scenario_id=scenario_id,
            scenario_resolution="explicit",
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
        }

    rebuild_result = await _complete_scenario_switch_rebuild(
        webspace_id,
        scenario_id=scenario_id,
        scenario_resolution="explicit",
    )
    if not bool(rebuild_result.get("accepted")):
        return rebuild_result

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
    "rebuild_webspace_from_sources",
]
