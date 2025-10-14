"""Template helpers for developer workflows."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Optional

from . import manifest, paths
from .errors import ETemplateNotFound
from .types import Kind


def _installed_candidates(kind: Kind) -> Dict[str, Path]:
    root = Path(paths.installed_root(kind))
    if not root.exists():
        return {}
    return {item.name: item for item in root.iterdir() if item.is_dir()}


def _builtin_candidates(kind: Kind) -> Dict[str, Path]:
    root = Path(paths.builtin_templates_root(kind))
    if not root.exists():
        return {}
    return {item.name: item for item in root.iterdir() if item.is_dir()}


def resolve_source(kind: Kind, template_or_installed: Optional[str]) -> Dict[str, Optional[str]]:
    """Resolve the template source.

    Returns a mapping containing ``source`` (installed|builtin), ``path``, ``name``
    and optional ``version`` information extracted from the manifest if present.
    """

    if not template_or_installed:
        template_or_installed = "skill_default" if kind == "skill" else "scenario_default"

    installed = _installed_candidates(kind)
    builtin = _builtin_candidates(kind)

    if template_or_installed in installed:
        src = installed[template_or_installed]
        manifest_data = manifest.read(str(src), kind)
        version = manifest_data.get("version") if isinstance(manifest_data, dict) else None
        return {
            "source": "installed",
            "path": str(src),
            "name": template_or_installed,
            "version": version,
        }

    if template_or_installed in builtin:
        src = builtin[template_or_installed]
        return {
            "source": "builtin",
            "path": str(src),
            "name": template_or_installed,
            "version": None,
        }

    candidates = list(installed.keys()) + [name for name in builtin.keys() if name not in installed]
    raise ETemplateNotFound(template_or_installed, candidates=candidates)


def list_candidates(kind: Kind, limit: int = 20) -> List[str]:
    installed = _installed_candidates(kind)
    builtin = _builtin_candidates(kind)
    names: List[str] = []
    for name in sorted(installed):
        names.append(name)
        if len(names) >= limit:
            return names
    for name in sorted(builtin):
        if name in installed:
            continue
        names.append(name)
        if len(names) >= limit:
            break
    return names


def copy_to(src_path: str, dst_path: str) -> None:
    src = Path(src_path)
    dst = Path(dst_path)
    if not src.exists():
        raise FileNotFoundError(src_path)
    if dst.exists():
        raise FileExistsError(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
