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
    # Ensure both the skill package root and its parent (which usually
    # contains the ``skills.<name>`` namespace) are visible on sys.path.
    for p in (skill_path, skill_path.parent):
        p_str = str(p)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)

    for extra in extra_paths or ():
        extra_path = Path(extra).resolve()
        if str(extra_path) not in sys.path:
            sys.path.insert(0, str(extra_path))

    module_name = module or "handlers.main"
    mod = importlib.import_module(module_name)
    try:
        func = getattr(mod, attr)
    except AttributeError as first_exc:
        # Fallback for cases where manifest.module is "handlers.main" but
        # the actual module lives under skills.<name>.handlers.main.
        if module_name == "handlers.main":
            skill_pkg = skill_path.name
            for candidate in (
                f"skills.{skill_pkg}.handlers.main",
                f"{skill_pkg}.handlers.main",
            ):
                try:
                    mod = importlib.import_module(candidate)
                    func = getattr(mod, attr)
                    break
                except Exception:
                    continue
            else:
                raise first_exc
        else:
            raise
    if not callable(func):
        raise TypeError(f"attribute '{attr}' from module '{module_name}' is not callable")

    mapping = dict(payload)
    meta = mapping.get("_meta")
    try:
        from adaos.sdk.io.context import io_meta  # pylint: disable=import-outside-toplevel
    except Exception:
        io_meta = None

    if io_meta is not None and isinstance(meta, Mapping):
        with io_meta(meta):
            if _should_expand_keywords(func):
                return func(**mapping)
            return func(mapping)

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
