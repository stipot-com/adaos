"""Utilities for executing local skill code from the AdaOS services layer.

The functions defined here are thin abstractions that encapsulate the
implementation previously living inside the CLI commands.  Moving them to the
service level makes them reusable from other entry points (tests, HTTP API,
etc.) while keeping the CLI focused on argument parsing and formatting the
output for the user.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from inspect import isawaitable
from pathlib import Path
from typing import Any, Mapping, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment

_SLOT_NAMES = ("A", "B")


def _ensure_sys_paths(skill_name: str, slot_root: Path) -> None:
    """Ensure the active slot paths are positioned at the front of ``sys.path``."""

    src_path = slot_root / "src"
    vendor_path = slot_root / "vendor"
    suffixes = {
        f"/{skill_name}/slots/current/src",
        f"/{skill_name}/slots/A/src",
        f"/{skill_name}/slots/B/src",
        f"/{skill_name}/slots/current/vendor",
        f"/{skill_name}/slots/A/vendor",
        f"/{skill_name}/slots/B/vendor",
    }

    def _is_outdated(entry: str) -> bool:
        normalized = entry.replace("\\", "/")
        return any(normalized.endswith(suffix) for suffix in suffixes)

    sys.path[:] = [p for p in sys.path if not _is_outdated(p)]

    paths_to_add = []
    if vendor_path.is_dir():
        paths_to_add.append(str(vendor_path))
    if src_path.is_dir():
        paths_to_add.append(str(src_path))

    for candidate in reversed(paths_to_add):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


def _clear_skill_modules(skill_name: str) -> None:
    prefix = f"skills.{skill_name}"
    for name in list(sys.modules.keys()):
        if name == prefix or name.startswith(prefix + "."):
            sys.modules.pop(name, None)


class SkillRuntimeError(RuntimeError):
    """Base error for problems while interacting with skill code."""


class SkillDirectoryNotFoundError(SkillRuntimeError):
    """Raised when a skill directory cannot be located on disk."""


class SkillDirectoryAmbiguousError(SkillRuntimeError):
    """Raised when multiple directories match the requested skill name."""


class SkillHandlerError(SkillRuntimeError):
    """Base class for handler-related problems."""


class SkillHandlerNotFoundError(SkillHandlerError):
    """Raised when ``handlers/main.py`` is missing."""


class SkillHandlerImportError(SkillHandlerError):
    """Raised when the handler module cannot be imported."""


class SkillHandlerMissingFunctionError(SkillHandlerError):
    """Raised when the handler module does not expose ``handle``."""


class SkillPrepError(SkillRuntimeError):
    """Raised when the preparation stage cannot be executed."""


class SkillPrepScriptNotFoundError(SkillPrepError):
    """Raised when ``prep/prepare.py`` is missing."""


class SkillPrepImportError(SkillPrepError):
    """Raised when the preparation script cannot be imported."""


class SkillPrepMissingFunctionError(SkillPrepError):
    """Raised when ``run_prep`` is not defined in ``prepare.py``."""


def _runtime_env(skill_name: str, agent_ctx: AgentContext) -> SkillRuntimeEnvironment:
    skills_root = Path(agent_ctx.paths.skills_dir())
    return SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill_name)


def _runtime_env_dev(skill_name: str, agent_ctx: AgentContext) -> SkillRuntimeEnvironment:
    skills_root = Path(agent_ctx.paths.dev_skills_dir())
    return SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill_name)


def resolve_active_version(skill_name: str, *, ctx: Optional[AgentContext] = None) -> str:
    """Return the active version for ``skill_name``.

    The version is resolved from the runtime environment metadata.  If the
    runtime environment is not prepared yet a ``SkillDirectoryNotFoundError`` is
    raised to mirror the previous behaviour of ``find_skill_dir``.
    """

    agent_ctx = ctx or get_ctx()
    env = _runtime_env(skill_name, agent_ctx)
    version = env.resolve_active_version()
    if not version:
        raise SkillDirectoryNotFoundError(
            f"Skill '{skill_name}' has no active runtime version under {env.runtime_root}"
        )
    return version


def _ensure_current_slot(env: SkillRuntimeEnvironment, version: str) -> Path:
    """Ensure the ``slots/current`` link exists and return its path."""

    current_link = env.ensure_current_link(version)
    if not current_link.exists():
        raise SkillDirectoryNotFoundError(
            f"Active slot not found for version {version} in {env.slots_root(version)}"
        )
    return current_link


def find_skill_slot(
    skill_name: str,
    *,
    ctx: Optional[AgentContext] = None,
    version: Optional[str] = None,
) -> Path:
    """Return the path to ``slots/current`` for ``skill_name``.

    The caller may provide an explicit ``version`` to bypass the version
    resolution step.  The returned path is always the logical ``slots/current``
    directory (which is a symlink on platforms that support it).
    """

    agent_ctx = ctx or get_ctx()
    env = _runtime_env(skill_name, agent_ctx)
    skill_version = version or resolve_active_version(skill_name, ctx=agent_ctx)
    current_link = _ensure_current_slot(env, skill_version)
    if not current_link.exists():
        raise SkillDirectoryNotFoundError(
            f"Active slot not found for skill '{skill_name}' (version {skill_version})"
        )
    return current_link


def find_skill_dir(skill_name: str, *, ctx: Optional[AgentContext] = None) -> Path:
    """Compatibility shim returning the skill package directory.

    Historically :func:`find_skill_dir` returned the skill sources that were
    executed in-process.  With the namespaced runtime layout the equivalent is
    ``slots/current/src/skills/<skill_name>``.  The helper is retained for the
    existing public API but callers are encouraged to migrate to
    :func:`find_skill_slot` instead.
    """

    slot_path = find_skill_slot(skill_name, ctx=ctx)
    skill_dir = slot_path / "src" / "skills" / skill_name
    if not skill_dir.is_dir():
        raise SkillDirectoryNotFoundError(
            f"Skill package for '{skill_name}' not found at {skill_dir}"
        )
    return skill_dir


async def run_skill_handler(
    skill_name: str,
    topic: str,
    payload: Mapping[str, Any],
    *,
    ctx: Optional[AgentContext] = None,
) -> Any:
    """Execute the ``handle`` function of a skill handler.

    Args:
        skill_name: Name of the skill to execute.
        topic: Event topic/intention passed to the handler.
        payload: JSON-like mapping that represents the payload.
        ctx: Optional context override.

    Returns:
        Whatever value the handler returns.

    Raises:
        SkillDirectoryNotFoundError: If the skill cannot be located.
        SkillDirectoryAmbiguousError: If multiple directories match the skill.
        SkillHandlerImportError: If the handler file is missing or invalid.
    """

    agent_ctx = ctx or get_ctx()
    version = resolve_active_version(skill_name, ctx=agent_ctx)
    slot_path = find_skill_slot(skill_name, ctx=agent_ctx, version=version)
    src_path = slot_path / "src"
    if not src_path.is_dir():
        raise SkillDirectoryNotFoundError(
            f"Active slot for '{skill_name}' is missing src directory: {src_path}"
        )

    skill_dir = src_path / "skills" / skill_name
    if not skill_dir.is_dir():
        raise SkillDirectoryNotFoundError(
            f"Skill package for '{skill_name}' not found: {skill_dir}"
        )

    _ensure_sys_paths(skill_name, slot_path)
    _clear_skill_modules(skill_name)
    module_name = f"skills.{skill_name}.handlers.main"
    try:
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise SkillHandlerImportError(f"Failed to import handler for {skill_name}: {exc}") from exc

    handle_fn = getattr(module, "handle", None)
    if handle_fn is None:
        raise SkillHandlerMissingFunctionError(
            f"'handle' not found in {module_name}"
        )

    skill_ctx_port = agent_ctx.skill_ctx
    previous = skill_ctx_port.get()
    if not skill_ctx_port.set(skill_name, skill_dir):
        raise SkillRuntimeError(f"failed to establish context for skill '{skill_name}'")
    try:
        result = handle_fn(topic, payload)
        if isawaitable(result):
            result = await result
        return result
    finally:
        if previous is None:
            skill_ctx_port.clear()
        else:
            skill_ctx_port.set(previous.name, previous.path)


def run_skill_handler_sync(
    skill_name: str,
    topic: str,
    payload: Mapping[str, Any],
    *,
    ctx: Optional[AgentContext] = None,
) -> Any:
    """Synchronously execute :func:`run_skill_handler`.

    This helper provides a convenient wrapper that can be used from synchronous
    contexts (like CLI commands).  It automatically manages the event loop by
    delegating to :func:`asyncio.run` when needed.  When executed inside an
    already running loop an explicit ``RuntimeError`` is raised to prevent
    accidental nested event loops.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_skill_handler(skill_name, topic, payload, ctx=ctx))
    raise RuntimeError("run_skill_handler_sync() cannot be used inside an active event loop")


def run_skill_prep(
    skill_name: str,
    *,
    ctx: Optional[AgentContext] = None,
) -> Mapping[str, Any]:
    """Execute the ``prepare.py`` helper for a given skill.

    Args:
        skill_name: Name of the skill.
        ctx: Optional context override.

    Returns:
        The dictionary returned by ``run_prep`` inside ``prepare.py``.

    Raises:
        SkillPrepError: For any issue related to locating or executing the
            preparation script.
    """

    agent_ctx = ctx or get_ctx()
    env = _runtime_env(skill_name, agent_ctx)
    return _run_skill_prep_from_env(skill_name, agent_ctx, env)


def run_dev_skill_prep(
    skill_name: str,
    *,
    ctx: Optional[AgentContext] = None,
) -> Mapping[str, Any]:
    """Execute the ``prepare.py`` helper for a DEV skill."""

    agent_ctx = ctx or get_ctx()
    env = _runtime_env_dev(skill_name, agent_ctx)
    return _run_skill_prep_from_env(skill_name, agent_ctx, env)


def _run_skill_prep_from_env(
    skill_name: str,
    agent_ctx: AgentContext,
    env: SkillRuntimeEnvironment,
) -> Mapping[str, Any]:
    version = env.resolve_active_version()
    if not version:
        raise SkillDirectoryNotFoundError(
            f"Skill '{skill_name}' has no active runtime version under {env.runtime_root}"
        )

    env.prepare_version(version)
    slot_path = env.ensure_current_link(version)
    if not slot_path.exists():
        raise SkillDirectoryNotFoundError(
            f"Active slot not found for skill '{skill_name}' (version {version})"
        )

    skill_dir = slot_path / "src" / "skills" / skill_name
    if not skill_dir.is_dir():
        raise SkillDirectoryNotFoundError(
            f"Skill package for '{skill_name}' not found: {skill_dir}"
        )

    prep_script = skill_dir / "prep" / "prepare.py"

    if not prep_script.exists():
        raise SkillPrepScriptNotFoundError(
            f"Preparation script not found for skill '{skill_name}'"
        )

    spec = importlib.util.spec_from_file_location("adaos_skill_prep", prep_script)
    if spec is None or spec.loader is None:
        raise SkillPrepImportError(f"Unable to import preparation script: {prep_script}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    run_prep = getattr(module, "run_prep", None)
    if run_prep is None:
        raise SkillPrepMissingFunctionError(f"run_prep() is not defined in {prep_script}")

    skill_ctx_port = agent_ctx.skill_ctx
    previous = skill_ctx_port.get()
    if not skill_ctx_port.set(skill_name, skill_dir):
        raise SkillRuntimeError(f"failed to establish context for skill '{skill_name}'")
    try:
        return run_prep(skill_dir)
    finally:
        if previous is None:
            skill_ctx_port.clear()
        else:
            skill_ctx_port.set(previous.name, previous.path)


__all__ = [
    "SkillRuntimeError",
    "SkillDirectoryNotFoundError",
    "SkillDirectoryAmbiguousError",
    "SkillHandlerError",
    "SkillHandlerNotFoundError",
    "SkillHandlerImportError",
    "SkillHandlerMissingFunctionError",
    "SkillPrepError",
    "SkillPrepScriptNotFoundError",
    "SkillPrepImportError",
    "SkillPrepMissingFunctionError",
    "resolve_active_version",
    "find_skill_slot",
    "find_skill_dir",
    "run_skill_handler",
    "run_skill_handler_sync",
    "run_skill_prep",
    "run_dev_skill_prep",
]
