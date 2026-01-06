from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager

_LOG = logging.getLogger("adaos.services.skills_loader")


class ImportlibSkillsLoader(SkillsLoaderPort):
    async def import_all_handlers(self, skills_root: Any) -> None:
        root = Path(skills_root() if callable(skills_root) else skills_root)
        self._sync_runtime_from_workspace_if_debug(root)
        loaded: set[str] = set()
        for handler, skill_name in self._discover_runtime_handlers(root):
            self._load_handler(handler)
            if skill_name:
                loaded.add(skill_name)
                _LOG.info("imported skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported skill handler path=%s", handler)

        # Dev/fast-path: load handlers straight from the workspace tree when a
        # skill does not have an installed runtime bundle under .runtime.
        for handler, skill_name in self._discover_workspace_handlers(root, loaded):
            self._load_handler(handler)
            if skill_name:
                loaded.add(skill_name)
                _LOG.info("imported workspace skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported workspace skill handler path=%s", handler)

    def _load_handler(self, handler: Path) -> None:
        mod_name = "adaos_skill_" + handler.parent.as_posix().replace("/", "_")
        spec = importlib.util.spec_from_file_location(mod_name, handler)
        module = importlib.util.module_from_spec(spec)  # noqa: F841
        assert spec and spec.loader
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        _LOG.info("imported skill handler module=%s path=%s", mod_name, handler)

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
            print("discover_log")
            for handler in src_root.rglob("handlers/main.py"):
                handlers.append((handler, skill_name))
        return handlers

    def _discover_workspace_handlers(self, root: Path, loaded: set[str]) -> Iterable[Tuple[Path, Optional[str]]]:
        handlers: list[Tuple[Path, Optional[str]]] = []
        for skill_dir in root.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith((".", "_")):
                continue
            # Skip runtime-bundled skills.
            if skill_dir.name in loaded:
                continue
            handler = skill_dir / "handlers" / "main.py"
            if handler.exists():
                handlers.append((handler, skill_dir.name))
        return handlers

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

        from adaos.adapters.db import SqliteSkillRegistry  # pylint: disable=import-outside-toplevel

        mgr = SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
            settings=ctx.settings,
        )

        for entry in ws_root.iterdir():
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            try:
                name = entry.name
                # First, ensure skill.yaml.tools reflects handlers so that
                # runtime manifests can be extended consistently.
                try:
                    mgr.sync_skill_yaml_tools_from_handlers(name, space="workspace")
                except Exception as exc:  # pragma: no cover - best-effort
                    _LOG.debug("sync_skill_yaml_tools_from_handlers failed for %s: %s", name, exc)
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
