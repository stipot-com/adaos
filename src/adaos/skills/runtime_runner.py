"""Runtime execution helpers for skill tool invocation."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Iterable, Mapping


def execute_tool(
    skill_dir: Path,
    *,
    module: str | None,
    attr: str,
    payload: Mapping[str, Any],
    extra_paths: Iterable[Path] | None = None,
) -> Any:
    """Execute a tool callable inside the skill package and return the result."""

    import sys

    skill_path = Path(skill_dir).resolve()
    if str(skill_path) not in sys.path:
        sys.path.insert(0, str(skill_path))

    for extra in extra_paths or ():
        extra_path = Path(extra).resolve()
        if str(extra_path) not in sys.path:
            sys.path.insert(0, str(extra_path))

    module_name = module or "handlers.main"
    mod = importlib.import_module(module_name)
    func = getattr(mod, attr)
    if not callable(func):
        raise TypeError(f"attribute '{attr}' from module '{module_name}' is not callable")

    mapping = dict(payload)
    if _should_expand_keywords(func):
        return func(**mapping)
    return func(mapping)


def _should_expand_keywords(func) -> bool:
    try:
        import inspect

        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if not params:
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return True
        if any(p.kind == inspect.Parameter.KEYWORD_ONLY for p in params):
            return True
        positional = [p for p in params if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD]
        if len(positional) <= 1:
            return False
        return True
    except Exception:
        return False

