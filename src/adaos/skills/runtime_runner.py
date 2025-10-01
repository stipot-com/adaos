"""Runtime execution helpers for skill tool shims."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Mapping


def execute_tool(skill_dir: Path, *, module: str | None, attr: str, payload: Mapping[str, Any]) -> int:
    """Execute a tool callable inside the skill package.

    The function ensures that ``skill_dir`` is present on ``sys.path`` before
    loading the module.  The callable is expected to accept a single mapping
    argument and return a JSON serialisable object.  The result is printed to
    stdout.
    """

    import sys

    skill_path = Path(skill_dir).resolve()
    if str(skill_path) not in sys.path:
        sys.path.insert(0, str(skill_path))

    module_name = module or "handlers.main"
    mod = importlib.import_module(module_name)
    func = getattr(mod, attr)
    result = func(**payload) if _supports_keyword_only(func) else func(payload)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _supports_keyword_only(func) -> bool:
    try:
        import inspect

        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        return params and all(p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY) for p in params)
    except Exception:
        return False

