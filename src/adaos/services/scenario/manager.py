# src\adaos\services\scenario\manager.py
from __future__ import annotations
from dataclasses import dataclass
import logging
import re, os, json
from pathlib import Path
from typing import Optional, Literal

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, Capabilities
from adaos.ports.paths import PathProvider
from adaos.ports.scenarios import ScenarioRepository
from adaos.services.eventbus import emit
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.capacity import install_scenario_in_capacity, uninstall_scenario_from_capacity
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.capacity import get_local_capacity
from adaos.services.node_config import load_config
from adaos.services.scenarios.loader import read_manifest, read_content
import y_py as Y
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.skill.manager import SkillManager

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
_log = logging.getLogger("adaos.scenario.manager")


@dataclass(slots=True)
class ArtifactPublishResult:
    kind: str
    name: str
    source_path: Path
    target_path: Path
    version: str
    previous_version: str | None
    updated_at: str
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


class ScenarioManager:
    """Coordinate scenario lifecycle operations via repository and registry."""

    def __init__(
        self,
        *,
        repo: ScenarioRepository,
        registry: SqliteScenarioRegistry,
        git: GitClient,
        paths: PathProvider,
        bus: EventBus,
        caps: Capabilities,  # SqliteScenarioRegistry протоколом не ограничиваем
    ):
        self.repo, self.reg, self.git, self.paths, self.bus, self.caps = repo, registry, git, paths, bus, caps
        self.ctx: AgentContext = get_ctx()

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "scenarios.manage")
        return self.reg.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "scenarios.manage")
        self.repo.ensure()
        return self.ctx.scenarios_repo.list()  # .repo.list()

    def sync(self) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        self.repo.ensure()
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        prefixed = [f"scenarios/{n}" for n in names]
        self.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.git.sparse_set(str(root), prefixed, no_cone=True)
        self.git.pull(str(root))
        emit(self.bus, "scenario.sync", {"count": len(names)}, "scenario.mgr")

    def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
        self.caps.require("core", "scenarios.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")

        self.reg.register(name, pin=pin)
        try:
            # TODO move test_mode to global ctx (single truth)
            test_mode = os.getenv("ADAOS_TESTING") == "1"
            if test_mode:
                return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
            # 3) mono-only установка через репозиторий (sparse-add + pull)
            meta = self.ctx.scenarios_repo.install(name, branch=None)
            if not meta:
                raise FileNotFoundError(f"scenario '{name}' not found in monorepo")
            emit(self.bus, "scenario.installed", {"id": meta.id.value, "pin": pin}, "scenario.mgr")
            try:
                install_scenario_in_capacity(meta.id.value, getattr(meta, "version", "unknown"), active=True)
                try:
                    conf = load_config()
                    if conf.role == "hub":
                        cap = get_local_capacity()
                        get_directory().repo.replace_scenario_capacity(conf.node_id, (cap.get("scenarios") or []))
                except Exception:
                    pass
            except Exception:
                pass
            return meta
        except Exception:
            self.reg.unregister(name)
            raise

    # --- Stage A2 helpers -------------------------------------------------

    def install_with_deps(self, name: str, *, pin: Optional[str] = None, webspace_id: str | None = None) -> SkillMeta:
        """
        Install a scenario (as with :meth:`install`) and then apply its
        manifest-defined dependencies inside the local workspace:

          - read scenario.yaml and ensure dependent skills are installed
            and activated via :class:`SkillManager`;
          - keep Yjs/bootstrap concerns in y_bootstrap (Stage A2).
        """
        target_webspace = webspace_id or default_webspace_id()
        meta = self.install(name, pin=pin)
        try:
            self._post_install_bootstrap(meta.id.value, webspace_id=target_webspace)
        except Exception:
            # Best-effort; do not fail install on bootstrap errors.
            pass
        try:
            self.sync_to_yjs(meta.id.value, webspace_id=target_webspace)
        except Exception:
            pass
        return meta

    def _ensure_yjs_payload(self, scenario_id: str, *, space: str = "workspace") -> tuple[dict, dict, dict, dict]:
        """
        Load and normalise declarative payload from scenario.json so that it can
        be projected into a webspace YDoc.

        The returned tuple is:
          - ui_section:    content["ui"]["application"]  (or {}),
          - registry:      content["registry"]           (or {}),
          - catalog:       content["catalog"]            (or {}),
          - data_section:  content["data"]               (or {}).

        This shape matches the target-state description in
        docs/concepts/scenarios-target-state.md and is the only input for the
        ScenarioManager.sync_to_yjs* projection helpers.
        """
        content = read_content(scenario_id, space=space)
        if not content:
            raise FileNotFoundError(f"scenario '{scenario_id}' has no scenario.json content")
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

        # Debug trace to help diagnose scenario→YDoc projection issues without
        # failing the projection if logging is misconfigured.
        try:
            desktop = (ui_section.get("desktop") or {}) if isinstance(ui_section, dict) else {}
            topbar = desktop.get("topbar")
            _log.debug("scenario '%s' ui.desktop.topbar=%s", scenario_id, topbar)
        except Exception:
            pass

        return ui_section, registry_section, catalog_section, data_section

    def _project_to_doc(
        self,
        ydoc: Y.YDoc,
        scenario_id: str,
        ui_section: dict,
        registry_section: dict,
        catalog_section: dict,
        data_section: dict,
    ) -> None:
        with ydoc.begin_transaction() as txn:
            ui_map = ydoc.get_map("ui")
            registry_map = ydoc.get_map("registry")
            data_map = ydoc.get_map("data")

            scenarios_ui = ui_map.get("scenarios")
            if not isinstance(scenarios_ui, dict):
                scenarios_ui = {}
            updated_ui = dict(scenarios_ui)
            updated_ui[scenario_id] = {"application": ui_section}
            ui_map.set(txn, "scenarios", updated_ui)
            if not ui_map.get("current_scenario"):
                ui_map.set(txn, "current_scenario", scenario_id)

            reg_scenarios = registry_map.get("scenarios")
            if not isinstance(reg_scenarios, dict):
                reg_scenarios = {}
            reg_updated = dict(reg_scenarios)
            reg_updated[scenario_id] = registry_section
            registry_map.set(txn, "scenarios", reg_updated)

            data_scenarios = data_map.get("scenarios")
            if not isinstance(data_scenarios, dict):
                data_scenarios = {}
            data_updated = dict(data_scenarios)
            entry = dict(data_updated.get(scenario_id) or {})
            entry["catalog"] = catalog_section
            data_updated[scenario_id] = entry
            data_map.set(txn, "scenarios", data_updated)

            # Optional root data overrides from scenario.json:
            # if "data" section is present in the scenario payload, its
            # top-level keys are projected into the webspace data map.
            # This is used for initial data seeding (e.g. data.weather)
            # and for YJS reload; only explicitly defined keys are
            # overwritten.
            if isinstance(data_section, dict):
                for key, value in data_section.items():
                    if not isinstance(key, str):
                        continue
                    try:
                        payload = json.loads(json.dumps(value))
                    except Exception:
                        payload = value
                    data_map.set(txn, key, payload)

    def sync_to_yjs(self, scenario_id: str, webspace_id: str | None = None, *, space: str = "workspace") -> None:
        """
        Project the declarative scenario payload into the webspace YDoc.

        This is the generic projection entry point described in the
        scenario target-state docs: it takes scenario.json, writes
        ui/data/registry sections into the YDoc, and emits a single
        ``scenarios.synced`` event that Webspace Scenario Runtime and other
        downstream services can react to.
        """
        target_webspace = webspace_id or default_webspace_id()
        self.caps.require("core", "scenarios.manage")
        ui_section, registry_section, catalog_section, data_section = self._ensure_yjs_payload(scenario_id, space=space)

        with get_ydoc(target_webspace) as ydoc:
            self._project_to_doc(ydoc, scenario_id, ui_section, registry_section, catalog_section, data_section)

        emit(self.bus, "scenarios.synced", {"scenario_id": scenario_id, "webspace_id": target_webspace}, "scenario.mgr")

    async def sync_to_yjs_async(self, scenario_id: str, webspace_id: str | None = None, *, space: str = "workspace") -> None:
        """
        Async variant of :meth:`sync_to_yjs` for use inside running event loops.
        """
        target_webspace = webspace_id or default_webspace_id()
        self.caps.require("core", "scenarios.manage")
        ui_section, registry_section, catalog_section, data_section = self._ensure_yjs_payload(scenario_id, space=space)

        async with async_get_ydoc(target_webspace) as ydoc:
            self._project_to_doc(ydoc, scenario_id, ui_section, registry_section, catalog_section, data_section)

        emit(self.bus, "scenarios.synced", {"scenario_id": scenario_id, "webspace_id": target_webspace}, "scenario.mgr")

    def _post_install_bootstrap(self, scenario_id: str, webspace_id: str | None = None) -> None:
        """
        Stage A2: after a scenario is installed into the workspace repo,
        apply its manifest-defined dependencies by installing/activating
        skills via the existing SkillManager.
        """
        manifest = read_manifest(scenario_id)
        depends = manifest.get("depends") or []
        if not isinstance(depends, (list, tuple)):
            return

        # Use the same construction pattern as CLI SkillManager.
        target_webspace = webspace_id or default_webspace_id()
        skill_reg = SqliteSkillRegistry(self.ctx.sql)
        skill_mgr = SkillManager(
            repo=self.ctx.skills_repo,
            registry=skill_reg,
            git=self.ctx.git,
            paths=self.ctx.paths,
            bus=self.bus,
            caps=self.ctx.caps,
        )
        for dep in depends:
            if not isinstance(dep, str) or not dep:
                continue
            try:
                # Ensure installed in monorepo and then activate runtime.
                skill_mgr.install(dep)
                version = None
                slot = None
                try:
                    runtime = skill_mgr.prepare_runtime(dep, run_tests=False)
                except Exception:
                    runtime = None
                if runtime:
                    version = getattr(runtime, "version", None)
                    slot = getattr(runtime, "slot", None)
                skill_mgr.activate_for_space(dep, version=version, slot=slot, space="default", webspace_id=target_webspace)
            except Exception:
                # Do not break scenario install on individual dependency issues.
                continue

    def remove(self, name: str, *, safe: bool = False) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")
        self.repo.ensure()
        self.reg.unregister(name)
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        prefixed = [f"scenarios/{n}" for n in names]
        # в тестах/без .git — только реестр, без git операций
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode or not (root / ".git").exists():
            return f"uninstalled: {name} (registry-only{' test-mode' if test_mode else ''})"
        self.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.git.sparse_set(str(root), prefixed, no_cone=True)
        if safe:
            # Безопасный режим: синхронизируем workspace с удалённым репо.
            self.git.pull(str(root))
        remove_tree(
            str(root / "scenarios" / name),
            fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs,
        )
        emit(self.bus, "scenario.removed", {"id": name}, "scenario.mgr")
        try:
            uninstall_scenario_from_capacity(name)
            try:
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_scenario_capacity(conf.node_id, cap.get("scenarios") or [])
            except Exception:
                pass
        except Exception:
            pass

    def push(self, name: str, message: str, *, signoff: bool = False) -> str:
        self.caps.require("core", "scenarios.manage", "git.write", "net.git")
        root = self.ctx.paths.workspace_dir()
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Scenarios repo is not initialized. Run `adaos scenario sync` once.")
        sub = name.strip()
        subpath = f"scenarios/{sub}"
        changed = self.git.changed_files(str(root), subpath=subpath)
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        msg = sanitize_message(message)
        ctx = get_ctx()
        sha = self.git.commit_subpath(
            str(root),
            subpath=subpath,
            message=msg,
            author_name=ctx.settings.git_author_name,
            author_email=ctx.settings.git_author_email,
            signoff=signoff,
        )
        if sha != "nothing-to-commit":
            self.git.push(str(root))
        return sha

    def publish(self, name: str, *, bump: Literal["major", "minor", "patch"] = "patch", force: bool = False, dry_run: bool = False, signoff: bool = False) -> ArtifactPublishResult:
        """
        Полный publish: локальная сборка версии (dev→workspace) + git commit/push подпути в workspace репо.
        """
        result = self._publish_artifact(
            self.ctx.config,  # или self._load_config() если нужно
            "skills",
            name,
            bump=bump,
            force=force,
            dry_run=dry_run,
        )
        if result.dry_run:
            return result
        # гарантируем, что подпуть есть в sparse-checkout (на случай узкой sparse-конфигурации)
        try:
            self.ctx.git.sparse_add(str(self.ctx.paths.workspace_dir()), f"skills/{name}")
        except Exception:
            pass
        # коммитим и пушим изменения только своего подпути
        msg = f"publish(skill): {result.name} v{result.version}"
        sha = self.push(result.name, msg, signoff=signoff)
        # ничего не мешает вернуть sha в result через setattr/обновлённый датакласс — но это опционально
        return result

    def uninstall(self, name: str, *, safe: bool = False) -> None:
        """Alias for :meth:`remove` to keep parity with :class:`SkillManager`."""
        self.remove(name, safe=safe)
