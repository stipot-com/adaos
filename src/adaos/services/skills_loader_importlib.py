from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from adaos.ports.skills_loader import SkillsLoaderPort

_LOG = logging.getLogger("adaos.services.skills_loader")


class ImportlibSkillsLoader(SkillsLoaderPort):
    async def import_all_handlers(self, skills_root: Any) -> None:
        root = Path(skills_root() if callable(skills_root) else skills_root)
        for handler, skill_name in self._discover_runtime_handlers(root):
            self._load_handler(handler)
            if skill_name:
                _LOG.info("imported skill handler skill=%s path=%s", skill_name, handler)
            else:
                _LOG.info("imported skill handler path=%s", handler)

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
            for handler in slot_dir.rglob("handlers/main.py"):
                handlers.append((handler, skill_name))
        return handlers

    def _discover_workspace_handlers(self, root: Path, loaded: set[str]) -> Iterable[Tuple[Path, Optional[str]]]:
        return []

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
