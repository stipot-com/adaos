from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
import asyncio
import json
import logging
import re
import secrets

import y_py as Y

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.capacity import get_local_capacity
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc
from adaos.services.scenarios import loader as scenarios_loader
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspaces import index as workspace_index
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.sdk.core.decorators import subscribe
from .workflow_runtime import ScenarioWorkflowRuntime

_log = logging.getLogger("adaos.scenario.webspace_runtime")
_WS_ID_RE = re.compile(r"[^a-zA-Z0-9-_]+")


@dataclass(slots=True)
class WebUIRegistryEntry:
    """
    Effective UI model snapshot for a single webspace after merging:

      - scenario-projected catalog/registry,
      - skill contributions from webui.json,
      - auto-installed items and current data.installed.
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
    is_dev: bool = False


def _mark_entry(entry: Dict[str, Any], *, source: str, dev: bool) -> Dict[str, Any]:
    """
    Attach provenance / dev flag to a catalog entry without overwriting its
    semantic "source" (which may already contain a YDoc path like "y:data/...").
    """
    data = dict(entry)
    if "source" in data and data["source"]:
        data["origin"] = source
    else:
        data["source"] = source
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
      - data.installed,
    and writes:
      - ui.application,
      - data.catalog,
      - data.installed,
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
            _log.debug("webui.json missing for %s (%s)", skill_name, space)
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
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

        # Ensure base weather skill webui remains available even if capacity
        # metadata is out of sync or the skill is not explicitly listed.
        try:
            weather_decl = self._load_webui("weather_skill", "default")
        except Exception:
            weather_decl = {}
        if isinstance(weather_decl, dict) and weather_decl:
            if not any(d.get("skill") == "weather_skill" for d in decls):
                decls.append(weather_decl)

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

    def _rebuild_in_doc(self, ydoc: Y.YDoc, webspace_id: str) -> WebUIRegistryEntry:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")

        scenario_id = ui_map.get("current_scenario") or "web_desktop"
        scenarios_ui = ui_map.get("scenarios") or {}
        scenario_ui_entry = scenarios_ui.get(scenario_id) if isinstance(scenarios_ui, dict) else {}
        scenario_app_ui = scenario_ui_entry.get("application") if isinstance(scenario_ui_entry, dict) else {}
        if not isinstance(scenario_app_ui, dict):
            scenario_app_ui = {}

        scenarios_data = data_map.get("scenarios") or {}
        scenario_entry = scenarios_data.get(scenario_id) if isinstance(scenarios_data, dict) else {}
        base_catalog = scenario_entry.get("catalog") if isinstance(scenario_entry, dict) else {}
        if not isinstance(base_catalog, dict):
            base_catalog = {}
        # Load/refresh data_projections for this scenario into the shared
        # ProjectionRegistry so that ctx.* writes can be routed correctly.
        try:
            self.ctx.projections.load_from_scenario(str(scenario_id))
        except Exception:
            _log.debug("failed to load data_projections for scenario=%s", scenario_id, exc_info=True)
        scenario_apps = [it for it in (base_catalog.get("apps") or []) if isinstance(it, Mapping)]
        scenario_widgets = [it for it in (base_catalog.get("widgets") or []) if isinstance(it, Mapping)]

        scenario_registry = registry_map.get("scenarios") or {}
        raw_registry_entry = scenario_registry.get(scenario_id) if isinstance(scenario_registry, dict) else {}
        registry_entry = raw_registry_entry if isinstance(raw_registry_entry, dict) else {}
        registry_entry = registry_entry or {}
        base_registry_modals = [str(x) for x in (registry_entry.get("modals") or [])]
        base_registry_widgets = [str(x) for x in (registry_entry.get("widgets") or [])]

        # Decide which skills to project based on webspace type:
        # - dev webspaces: only dev skills (mode="dev"),
        # - regular webspaces: only non-dev skills (mode="workspace"),
        # - fallback: mixed.
        mode = "mixed"
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                title = row.display_name or row.workspace_id
                mode = "dev" if _is_dev_title(title) else "workspace"
        except Exception:
            mode = "mixed"

        skill_decls = self._collect_skill_decls(mode=mode)
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

        # Scenario-defined apps and widgets (base desktop scenario content).
        merged_apps = [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_apps]
        merged_widgets = [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_widgets]

        # Inject desktop scenario launchers as pseudo-apps into the global
        # catalog so that the Apps catalog can expose them as icons. These
        # are not auto-installed; users can pin them to the desktop using
        # the standard desktop.toggleInstall flow.
        extra_apps: List[Dict[str, Any]] = []
        for sid, title in self._list_desktop_scenarios(space=mode):
            # Avoid creating a launcher for the currently active scenario and
            # only add one entry per logical id.
            if sid == scenario_id:
                continue
            app_id = f"scenario:{sid}"
            extra_apps.append(
                {
                    "id": app_id,
                    "title": title,
                    "icon": "apps-outline",
                    "launchModal": "scenario_switcher",
                    "scenario_id": sid,
                }
            )
            # Keep launchers visible after reloads by auto-installing them when
            # rebuilding the effective catalog.
            auto_app_ids.add(app_id)

        merged_apps = _merge_by_id(merged_apps + extra_apps + skill_apps)

        merged_widgets = _merge_by_id(merged_widgets + skill_widgets)
        merged_registry = {
            "modals": _merge_registry_lists(
                base_registry_modals,
                skill_registry_modals + [["apps_catalog", "widgets_catalog", "scenario_switcher"]],
            ),
            "widgets": _merge_registry_lists(base_registry_widgets, skill_registry_widgets),
        }

        installed_current = data_map.get("installed") or {}
        if not isinstance(installed_current, dict):
            installed_current = {}
        apps_set = set(installed_current.get("apps") or [])
        widgets_set = set(installed_current.get("widgets") or [])
        apps_set |= auto_app_ids
        widgets_set |= auto_widget_ids
        installed_with_auto = {"apps": list(apps_set), "widgets": list(widgets_set)}
        filtered_installed = _filter_installed(installed_with_auto, merged_apps, merged_widgets)

        # Merge scenario-defined modals with skill-provided modal schemas and
        # ensure the generic desktop catalogs (apps/widgets) remain available.
        merged_modals_map: Dict[str, Any] = {}
        base_modals_map: Dict[str, Any] = {}
        try:
            raw = scenario_app_ui.get("modals") if isinstance(scenario_app_ui, dict) else None
            if isinstance(raw, dict):
                base_modals_map = raw
        except Exception:
            base_modals_map = {}
        for key, value in (base_modals_map or {}).items():
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

        # Ensure generic desktop catalog modals exist so that apps/widgets
        # (including scenario launchers) can be managed even if the base
        # scenario UI is minimal.
        if "apps_catalog" not in merged_modals_map:
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
        if "widgets_catalog" not in merged_modals_map:
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

        app_with_modals: Dict[str, Any] = dict(scenario_app_ui)
        if merged_modals_map:
            app_with_modals["modals"] = merged_modals_map

        entry = WebUIRegistryEntry(
            scenario_id=str(scenario_id),
            apps=[dict(it) for it in merged_apps],
            widgets=[dict(it) for it in merged_widgets],
            registry_modals=list(merged_registry.get("modals") or []),
            registry_widgets=list(merged_registry.get("widgets") or []),
            installed={"apps": list(filtered_installed.get("apps") or []), "widgets": list(filtered_installed.get("widgets") or [])},
        )

        with ydoc.begin_transaction() as txn:
            # Apply YDoc defaults from skills first so that data paths
            # referenced by widgets/modals exist.
            try:
                self._apply_ydoc_defaults_in_txn(ydoc, txn, skill_decls)
            except Exception:
                _log.warning("failed to apply ydoc_defaults for webspace=%s", webspace_id, exc_info=True)

            ui_map.set(txn, "application", app_with_modals)
            data_map.set(txn, "catalog", {"apps": merged_apps, "widgets": merged_widgets})
            data_map.set(txn, "installed", filtered_installed)
            registry_map.set(txn, "merged", merged_registry)

        try:
            _log.debug(
                "rebuilt webspace=%s scenario=%s apps=%d widgets=%d",
                webspace_id,
                scenario_id,
                len(entry.apps),
                len(entry.widgets),
            )
        except Exception:
            # Debug logging should never break callers.
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


def _webspace_listing() -> List[Dict[str, Any]]:
    rows = workspace_index.list_workspaces()
    return [
        {
            "id": row.workspace_id,
            "title": (row.display_name or row.workspace_id),
            "created_at": row.created_at,
            "kind": "dev" if _is_dev_title(row.display_name or row.workspace_id) else "workspace",
        }
        for row in rows
    ]


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
            title = row.display_name or row.workspace_id
            is_dev = _is_dev_title(title)
            if mode == "workspace" and is_dev:
                continue
            if mode == "dev" and not is_dev:
                continue
            infos.append(
                WebspaceInfo(
                    id=row.workspace_id,
                    title=title,
                    created_at=row.created_at,
                    is_dev=is_dev,
                )
            )
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
        workspace_index.ensure_workspace(webspace_id)
        raw_title = (title or webspace_id).strip()
        if dev and not _is_dev_title(raw_title):
            display_name = f"DEV: {raw_title}"
        elif not dev and _is_dev_title(raw_title):
            # Normalise accidental prefix when dev=False.
            display_name = raw_title.lstrip()[4:].lstrip() or webspace_id
        else:
            display_name = raw_title or webspace_id
        workspace_index.set_display_name(webspace_id, display_name)
        await _seed_webspace_from_scenario(webspace_id, scenario_id, dev=dev)
        await self._sync_listing()
        return WebspaceInfo(
            id=webspace_id,
            title=display_name,
            created_at=workspace_index.get_workspace(webspace_id).created_at,  # type: ignore[union-attr]
            is_dev=_is_dev_title(display_name),
        )

    async def rename(self, webspace_id: str, title: str) -> Optional[WebspaceInfo]:
        webspace_id = (webspace_id or "").strip()
        title = (title or "").strip()
        if not webspace_id or not title:
            return None
        row = workspace_index.get_workspace(webspace_id)
        if not row:
            _log.warning("cannot rename missing webspace %s", webspace_id)
            return None
        keep_dev = _is_dev_title(row.display_name or row.workspace_id)
        raw_title = title
        if keep_dev and not _is_dev_title(raw_title):
            display_name = f"DEV: {raw_title}"
        elif not keep_dev and _is_dev_title(raw_title):
            display_name = raw_title.lstrip()[4:].lstrip() or webspace_id
        else:
            display_name = raw_title
        workspace_index.set_display_name(webspace_id, display_name)
        await self._sync_listing()
        return WebspaceInfo(
            id=webspace_id,
            title=display_name,
            created_at=row.created_at,
            is_dev=_is_dev_title(display_name),
        )

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
            from adaos.services.yjs.gateway import y_server  # pylint: disable=import-outside-toplevel
            from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

            try:
                y_server.rooms.pop(webspace_id, None)
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
        await self._sync_listing()


async def _seed_webspace_from_scenario(webspace_id: str, scenario_id: str, *, dev: Optional[bool] = None) -> None:
    """
    Seed a webspace YDoc from the given scenario package using the standard
    ScenarioManager.sync_to_yjs* projection path, falling back to static
    seeds inside ensure_webspace_seeded_from_scenario when needed.
    """
    ystore = get_ystore_for_webspace(webspace_id)
    if dev is None:
        try:
            row = workspace_index.get_workspace(webspace_id)
            if row:
                dev = _is_dev_title(row.display_name or row.workspace_id)
            else:
                dev = False
        except Exception:
            dev = False
    _log.debug("seeding webspace=%s scenario=%s dev=%s", webspace_id, scenario_id, dev)
    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=scenario_id or "web_desktop",
            space="dev" if dev else "workspace",
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
    ctx = get_ctx()
    runtime = WebspaceScenarioRuntime(ctx)
    await runtime.rebuild_webspace_async(webspace_id)


@subscribe("skills.activated")
async def _on_skill_activated(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI for the target webspace when a skill is activated.

    For MVP we only rebuild the webspace explicitly referenced in the event
    (or the default webspace), not all workspaces.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    ctx = get_ctx()
    runtime = WebspaceScenarioRuntime(ctx)
    await runtime.rebuild_webspace_async(webspace_id)


@subscribe("skills.rolledback")
async def _on_skill_rolled_back(evt: Dict[str, Any]) -> None:
    """
    Rebuild effective UI when a skill is rolled back so that its catalog
    entries and registry contributions are removed from the target webspace.
    """
    webspace_id = str(evt.get("webspace_id") or default_webspace_id())
    ctx = get_ctx()
    runtime = WebspaceScenarioRuntime(ctx)
    await runtime.rebuild_webspace_async(webspace_id)


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


@subscribe("desktop.webspace.reload")
async def _on_webspace_reload(evt: Dict[str, Any]) -> None:
    """
    Re-seed the current webspace from its scenario, effectively
    rebuilding ui/data/registry for debugging or recovery.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    scenario_id = str(payload.get("scenario_id") or "web_desktop")
    if not webspace_id:
        return
    _log.info("reloading webspace %s from scenario %s", webspace_id, scenario_id)
    try:
        from adaos.services.yjs.gateway import y_server  # pylint: disable=import-outside-toplevel
        from adaos.services.yjs.store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

        try:
            y_server.rooms.pop(webspace_id, None)
        except Exception:
            pass
        try:
            reset_ystore_for_webspace(webspace_id)
        except Exception:
            pass
    except Exception:
        _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)

    await _seed_webspace_from_scenario(webspace_id, scenario_id)
    await _sync_webspace_listing()

    # After reseeding the webspace from scenario sources, rebuild the
    # effective UI (application + catalog + installed) so that the
    # desktop reflects the current scenario and skill contributions.
    try:
        ctx = get_ctx()
        runtime = WebspaceScenarioRuntime(ctx)
        await runtime.rebuild_webspace_async(webspace_id)
    except Exception:
        _log.warning(
            "failed to rebuild webspace after reload webspace=%s scenario=%s",
            webspace_id,
            scenario_id,
            exc_info=True,
        )


@subscribe("desktop.webspace.reset")
async def _on_webspace_reset(evt: Dict[str, Any]) -> None:
    """
    Hard reset of the current webspace from its scenario. For now this
    mirrors desktop.webspace.reload behaviour; it is introduced as a
    separate event so that future versions can differentiate between
    soft reload (updatable-only) and full reset.
    """
    await _on_webspace_reload(evt)


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
    _log.info("desktop.scenario.set webspace=%s scenario=%s", webspace_id, scenario_id)

    # Lightweight projection of scenario.json into the target webspace YDoc:
    # load declarative sections and update ui.scenarios/data.scenarios/registry.
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            # Use dev or workspace scenario content depending on the target webspace.
            space = "workspace"
            try:
                row = workspace_index.get_workspace(webspace_id)
                if row and _is_dev_title(row.display_name or row.workspace_id):
                    space = "dev"
            except Exception:
                space = "workspace"

            content = scenarios_loader.read_content(scenario_id, space=space)
            if not isinstance(content, dict) or not content:
                _log.warning("desktop.scenario.set: no scenario.json for %s", scenario_id)
                return
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
                # ui.scenarios[scenario_id].application
                scenarios_ui = ui_map.get("scenarios")
                if not isinstance(scenarios_ui, dict):
                    scenarios_ui = {}
                updated_ui = dict(scenarios_ui)
                updated_ui[scenario_id] = {"application": ui_section}
                ui_map.set(txn, "scenarios", updated_ui)
                # always switch current_scenario explicitly
                ui_map.set(txn, "current_scenario", scenario_id)

                # registry.scenarios[scenario_id]
                reg_scenarios = registry_map.get("scenarios")
                if not isinstance(reg_scenarios, dict):
                    reg_scenarios = {}
                reg_updated = dict(reg_scenarios)
                reg_updated[scenario_id] = registry_section
                registry_map.set(txn, "scenarios", reg_updated)

                # data.scenarios[scenario_id].catalog
                data_scenarios = data_map.get("scenarios")
                if not isinstance(data_scenarios, dict):
                    data_scenarios = {}
                data_updated = dict(data_scenarios)
                entry = dict(data_updated.get(scenario_id) or {})
                entry["catalog"] = catalog_section
                data_updated[scenario_id] = entry
                data_map.set(txn, "scenarios", data_updated)

                # Optional root data overrides from scenario.json["data"].
                # Do not overwrite runtime-managed keys such as ``installed``,
                # otherwise switching scenarios would reset desktop icons/apps
                # to their initial defaults and discard user choices.
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
        _log.warning("failed to switch scenario for webspace=%s scenario=%s", webspace_id, scenario_id, exc_info=True)
        return

    # Recompute effective UI for the webspace so that desktop/catalog
    # reflect the selected scenario immediately.
    ctx = get_ctx()
    runtime = WebspaceScenarioRuntime(ctx)
    await runtime.rebuild_webspace_async(webspace_id)

    # Initialise workflow state/next_actions for the selected scenario.
    try:
        wf = ScenarioWorkflowRuntime(ctx)
        await wf.sync_workflow_for_webspace(scenario_id, webspace_id)
    except Exception:
        _log.warning(
            "failed to sync workflow for webspace=%s scenario=%s",
            webspace_id,
            scenario_id,
            exc_info=True,
        )


__all__ = ["WebUIRegistryEntry", "WebspaceScenarioRuntime"]
