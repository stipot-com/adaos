from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
import yaml

_LOG = logging.getLogger("adaos.services.skills_loader")


class ImportlibSkillsLoader(SkillsLoaderPort):
    async def import_all_handlers(self, skills_root: Any) -> None:
        root = Path(skills_root() if callable(skills_root) else skills_root)
        self._sync_runtime_from_repo_workspace_if_missing(root)
        self._sync_runtime_from_workspace_if_debug(root)
        loaded: set[str] = set()
        loaded_projection_manifests: set[Path] = set()
        for handler, skill_name in self._discover_runtime_handlers(root):
            self._load_skill_data_projections(handler, loaded_projection_manifests)
            self._load_handler(handler)
            if skill_name:
                loaded.add(skill_name)
                _LOG.info("imported skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported skill handler path=%s", handler)

        # Dev/fast-path: load handlers straight from the workspace tree when a
        # skill does not have an installed runtime bundle under .runtime.
        for handler, skill_name in self._discover_workspace_handlers(root, loaded):
            self._load_skill_data_projections(handler, loaded_projection_manifests)
            self._load_handler(handler)
            if skill_name:
                loaded.add(skill_name)
                _LOG.info("imported workspace skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported workspace skill handler path=%s", handler)

        # Repo-bundled workspace skills are a final fallback for builtin skills
        # when the node-local workspace tree does not contain the sources.
        for handler, skill_name in self._discover_repo_workspace_handlers(root, loaded):
            self._load_skill_data_projections(handler, loaded_projection_manifests)
            self._load_handler(handler)
            if skill_name:
                loaded.add(skill_name)
                _LOG.info("imported repo workspace skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported repo workspace skill handler path=%s", handler)

    def _load_handler(self, handler: Path) -> None:
        mod_name = "adaos_skill_" + handler.parent.as_posix().replace("/", "_")
        existing = sys.modules.get(mod_name)
        if existing is not None:
            _LOG.debug("reusing already imported skill handler module=%s path=%s", mod_name, handler)
            return
        spec = importlib.util.spec_from_file_location(mod_name, handler)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        _LOG.info("imported skill handler module=%s path=%s", mod_name, handler)

    def _load_skill_data_projections(self, handler: Path, loaded: set[Path]) -> None:
        manifest_path = self._find_skill_manifest(handler)
        if manifest_path is None:
            return
        try:
            resolved = manifest_path.resolve()
        except OSError:
            resolved = manifest_path
        if resolved in loaded:
            return
        loaded.add(resolved)
        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            _LOG.debug("failed to read skill manifest for projections path=%s", manifest_path, exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        entries = payload.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            return
        try:
            get_ctx().projections.load_entries(entries)
            _LOG.info("loaded skill data_projections path=%s entries=%d", manifest_path, len(entries))
        except Exception:
            _LOG.debug("failed to load skill data_projections path=%s", manifest_path, exc_info=True)

    @staticmethod
    def _find_skill_manifest(handler: Path) -> Optional[Path]:
        for parent in handler.parents:
            for name in ("skill.yaml", "resolved.manifest.json"):
                candidate = parent / name
                if candidate.exists():
                    return candidate
        return None

    def _discover_runtime_handlers(self, root: Path) -> Iterable[Tuple[Path, Optional[str]]]:
        runtime_root = root / ".runtime"
        if not runtime_root.exists():
            return []

        handlers: list[Tuple[Path, Optional[str]]] = []
        for skill_dir in runtime_root.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            version = self._read_text(skill_dir / "current_version")
            if not version:
                continue
            version_dir = skill_dir / version
            slot_dir = self._resolve_slot(version_dir)
            if not slot_dir:
                continue
            # Handlers live under slots/<slot>/src; avoid scanning vendor/runtime trees.
            src_root = slot_dir / "src"
            if not src_root.exists():
                continue
            # Skip service skills (they are started by ServiceSkillSupervisor).
            manifest_path = slot_dir / "resolved.manifest.json"
            if self._is_service_manifest(manifest_path):
                continue
            for handler in src_root.rglob("handlers/main.py"):
                handlers.append((handler, skill_name))
        return handlers

    def _discover_workspace_handlers(self, root: Path, loaded: set[str]) -> Iterable[Tuple[Path, Optional[str]]]:
        if not root.exists():
            return []
        handlers: list[Tuple[Path, Optional[str]]] = []
        for skill_dir in root.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith((".", "_")):
                continue
            # Skip service skills (they are started by ServiceSkillSupervisor).
            if self._is_service_manifest(skill_dir / "skill.yaml"):
                continue
            # Skip runtime-bundled skills.
            if skill_dir.name in loaded:
                continue
            handler = skill_dir / "handlers" / "main.py"
            if handler.exists():
                handlers.append((handler, skill_dir.name))
        return handlers

    def _discover_repo_workspace_handlers(self, root: Path, loaded: set[str]) -> Iterable[Tuple[Path, Optional[str]]]:
        repo_root = self._repo_workspace_skills_root()
        if repo_root is None or not repo_root.exists():
            return []

        try:
            ctx = get_ctx()
            ws_root = ctx.paths.skills_workspace_dir()
            ws_root = Path(ws_root() if callable(ws_root) else ws_root)
        except Exception:
            ws_root = root

        handlers: list[Tuple[Path, Optional[str]]] = []
        for skill_dir in repo_root.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith((".", "_")):
                continue
            if skill_dir.name in loaded:
                continue
            # A real node-local workspace copy takes precedence over repo fallback.
            if (ws_root / skill_dir.name).exists():
                continue
            if self._is_service_manifest(skill_dir / "skill.yaml"):
                continue
            handler = skill_dir / "handlers" / "main.py"
            if handler.exists():
                handlers.append((handler, skill_dir.name))
        return handlers

    @staticmethod
    def _is_service_manifest(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return False
        runtime = content.get("runtime") or {}
        if isinstance(runtime, dict) and runtime.get("kind") == "service":
            return True
        return False

    @staticmethod
    def _resolve_slot(version_dir: Path) -> Optional[Path]:
        current = version_dir / "slots" / "current"
        if current.exists():
            try:
                resolved = current.resolve()
                if resolved.exists():
                    return resolved
            except OSError:
                pass
        active_file = version_dir / "active"
        active = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else ""
        if not active:
            return None
        slot_dir = version_dir / "slots" / active
        return slot_dir if slot_dir.exists() else None

    @staticmethod
    def _read_text(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Workspace/runtime sync helpers (DEBUG only)
    # ------------------------------------------------------------------
    def _sync_runtime_from_workspace_if_debug(self, runtime_root: Path) -> None:
        """
        In DEBUG-like modes keep runtime slots in sync with workspace
        sources for owner skills by calling SkillManager.runtime_update(...).

        This is called on every skills loader refresh (e.g. api --reload)
        so edits in workspace are reflected in the active runtime slot
        without manual reinstall.

        The guard is intentionally loose for local/dev runs:
          - if ADAOS_LOG_LEVEL is unset -> treat as DEBUG (sync enabled),
          - if ADAOS_LOG_LEVEL is set and not DEBUG -> skip sync.
        """
        level = (os.getenv("ADAOS_LOG_LEVEL") or "").upper()
        # In local/dev setups ADAOS_LOG_LEVEL is often unset; enable sync
        # by default there, but honour explicit non-DEBUG settings.
        if level and level != "DEBUG":
            return

        try:
            ctx = get_ctx()
            ws_root = ctx.paths.skills_workspace_dir()
            ws_root = Path(ws_root() if callable(ws_root) else ws_root)
        except Exception:
            return
        if not ws_root.exists():
            return
        mgr = self._build_skill_manager(ctx)

        for entry in ws_root.iterdir():
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            try:
                name = entry.name
                result = mgr.runtime_update(name, space="workspace")
            except Exception as exc:
                _LOG.debug("runtime_update failed for %s: %s", name, exc)
                continue
            if not result.get("ok"):
                continue
            files = result.get("files") or []
            tools = result.get("tools_added") or []
            if files or tools:
                _LOG.info(
                    "runtime_update applied for workspace skill '%s' (files=%d, tools_added=%d)",
                    name,
                    len(files),
                    len(tools),
                )

    def _sync_runtime_from_repo_workspace_if_missing(self, runtime_root: Path) -> None:
        repo_ws_root = self._repo_workspace_skills_root()
        if repo_ws_root is None or not repo_ws_root.exists():
            return

        try:
            ctx = get_ctx()
            ws_root = ctx.paths.skills_workspace_dir()
            ws_root = Path(ws_root() if callable(ws_root) else ws_root)
        except Exception:
            return

        runtime_state_root = runtime_root / ".runtime"
        if not runtime_state_root.exists():
            return

        mgr = self._build_skill_manager(ctx)
        for runtime_skill_root in runtime_state_root.iterdir():
            if not runtime_skill_root.is_dir():
                continue
            name = runtime_skill_root.name
            if name.startswith((".", "_")):
                continue
            if (ws_root / name).exists():
                continue
            if not (repo_ws_root / name).exists():
                continue
            try:
                result = mgr.runtime_update(name, space="workspace")
            except Exception as exc:
                _LOG.debug("repo workspace runtime_update failed for %s: %s", name, exc)
                continue
            if not result.get("ok"):
                continue
            files = result.get("files") or []
            tools = result.get("tools_added") or []
            if files or tools:
                _LOG.info(
                    "runtime_update applied from repo workspace for skill '%s' (files=%d, tools_added=%d)",
                    name,
                    len(files),
                    len(tools),
                )

    @staticmethod
    def _repo_workspace_skills_root() -> Optional[Path]:
        try:
            ctx = get_ctx()
            repo_root_attr = getattr(ctx.paths, "repo_root", None)
            repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
            if not repo_root:
                return None
            candidate = Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills"
            if candidate.exists():
                return candidate
        except Exception:
            return None
        return None

    @staticmethod
    def _build_skill_manager(ctx: Any) -> SkillManager:
        from adaos.adapters.db import SqliteSkillRegistry  # pylint: disable=import-outside-toplevel

        return SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
            settings=ctx.settings,
        )
