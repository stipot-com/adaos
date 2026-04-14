from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import yaml

from adaos.services.agent_context import get_ctx

_log = logging.getLogger("adaos.scenarios.loader")
_CONTENT_CACHE: Dict[Tuple[str, str], Tuple[Tuple[str, int, int], Dict[str, Any]]] = {}
_MANIFEST_CACHE: Dict[Tuple[str, str], Tuple[Tuple[str, int, int], Dict[str, Any]]] = {}


def _file_stamp(path: Path) -> Tuple[str, int, int]:
    stat = path.stat()
    return (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))


def _read_cached_mapping_file(
    *,
    cache: Dict[Tuple[str, str], Tuple[Tuple[str, int, int], Dict[str, Any]]],
    key: Tuple[str, str],
    path: Path,
    reader: Callable[[str], Any],
    encoding: str,
) -> Dict[str, Any]:
    stamp = _file_stamp(path)
    cached = cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        raw = path.read_text(encoding=encoding)
        data = reader(raw) or {}
    except Exception:
        data = {}

    if not isinstance(data, dict):
        data = {}
    cache[key] = (stamp, data)
    return data


def _scenario_root_for_space(scenario_id: str, space: str) -> Path:
    """
    Internal helper that resolves scenario root for the given space
    ("workspace" or "dev").
    """
    ctx = get_ctx()
    if space == "dev":
        base = ctx.paths.dev_scenarios_dir()
    else:
        base = ctx.paths.scenarios_dir()
    return base / scenario_id


def _repo_workspace_scenario_root(scenario_id: str) -> Path | None:
    try:
        ctx = get_ctx()
        repo_root_attr = getattr(ctx.paths, "repo_root", None)
        repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
        if not repo_root:
            return None
        return Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "scenarios" / scenario_id
    except Exception:
        return None


def _candidate_roots(scenario_id: str, space: str) -> tuple[Path, ...]:
    primary = _scenario_root_for_space(scenario_id, space)
    fallback = _repo_workspace_scenario_root(scenario_id)
    roots = [primary]
    if fallback is not None and fallback != primary:
        roots.append(fallback)
    return tuple(roots)


def scenario_root(scenario_id: str) -> Path:
    """
    Resolve the filesystem root for a scenario in the main workspace,
    e.g. ``<base_dir>/.adaos/workspace/scenarios/<scenario_id>/``.

    For dev scenarios use :func:`scenario_root_for_space` instead.
    """
    return _scenario_root_for_space(scenario_id, "workspace")


def scenario_root_for_space(scenario_id: str, space: str) -> Path:
    """
    Resolve the filesystem root for a scenario in the requested space:

      - "workspace" (default) — regular installed scenarios,
      - "dev"                — dev workspace scenarios.
    """
    if space not in ("workspace", "dev"):
        space = "workspace"
    return _scenario_root_for_space(scenario_id, space)


def read_manifest(scenario_id: str, *, space: str = "workspace") -> Dict[str, Any]:
    """
    Read scenario.yaml for a given scenario id. Returns {} if missing.

    When ``space="dev"`` the loader looks under ``dev_scenarios_dir``.
    """
    key = (str(scenario_id), str(space))
    for root in _candidate_roots(scenario_id, space):
        path = root / "scenario.yaml"
        if not path.exists():
            continue
        data = _read_cached_mapping_file(
            cache=_MANIFEST_CACHE,
            key=key,
            path=path,
            reader=yaml.safe_load,
            encoding="utf-8",
        )
        if data:
            return data
        return {}
    _MANIFEST_CACHE.pop(key, None)
    return {}


def read_content(scenario_id: str, *, space: str = "workspace") -> Dict[str, Any]:
    """
    Read scenario.json for a given scenario id. Returns {} if missing/invalid.

    When ``space="dev"`` the loader looks under ``dev_scenarios_dir``.
    """
    key = (str(scenario_id), str(space))
    for root in _candidate_roots(scenario_id, space):
        path = root / "scenario.json"
        if not path.exists():
            continue
        _log.debug("reading scenario '%s' content from %s", scenario_id, path)
        return _read_cached_mapping_file(
            cache=_CONTENT_CACHE,
            key=key,
            path=path,
            reader=json.loads,
            encoding="utf-8-sig",
        )
    _log.debug("scenario '%s' has no scenario.json in any candidate roots", scenario_id)
    _CONTENT_CACHE.pop(key, None)
    return {}


def scenario_exists(scenario_id: str, *, space: str = "workspace") -> bool:
    """
    Cheap existence probe used by pointer-first scenario switching.

    This avoids loading/parsing full ``scenario.json`` when the caller only
    needs to confirm that a scenario is present in the requested source space.
    """
    for root in _candidate_roots(scenario_id, space):
        if (root / "scenario.json").exists() or (root / "scenario.yaml").exists():
            return True
    return False


def invalidate_cache(*, scenario_id: str | None = None, space: str | None = None) -> None:
    """
    Invalidate in-memory scenario.json cache. This is required for workflows
    like desktop.webspace.reload which expect updated UI/NLU definitions to be
    picked up without restarting the hub process.
    """
    keys = list(_CONTENT_CACHE.keys())
    for key in keys:
        sid, sp = key
        if scenario_id is not None and sid != str(scenario_id):
            continue
        if space is not None and sp != str(space):
            continue
        _CONTENT_CACHE.pop(key, None)
    keys = list(_MANIFEST_CACHE.keys())
    for key in keys:
        sid, sp = key
        if scenario_id is not None and sid != str(scenario_id):
            continue
        if space is not None and sp != str(space):
            continue
        _MANIFEST_CACHE.pop(key, None)


__all__ = ["scenario_root", "read_manifest", "read_content", "scenario_exists", "invalidate_cache"]
