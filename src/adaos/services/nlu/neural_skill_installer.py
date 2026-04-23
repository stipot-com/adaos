from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

from adaos.services.agent_context import get_ctx


_SKILL_NAME = "neural_nlu_service_skill"
_PACKAGE = "adaos.interpreter_data"
_RESOURCE_DIR = "neural_nlu_service_skill"


def ensure_neural_service_skill_installed() -> Path | None:
    """
    Ensure default neural service-skill exists in workspace skills directory.

    Returns target path when created (or already present), otherwise None.
    """
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    target = skills_root / _SKILL_NAME
    if target.exists():
        return target

    try:
        src_dir = resources.files(_PACKAGE) / _RESOURCE_DIR
    except Exception:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with resources.as_file(src_dir) as src:
            shutil.copytree(src, target)
    except Exception:
        return None
    return target
