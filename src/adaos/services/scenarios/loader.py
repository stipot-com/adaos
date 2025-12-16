from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from adaos.services.agent_context import get_ctx

_log = logging.getLogger("adaos.scenarios.loader")
_CONTENT_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}


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
    root = scenario_root_for_space(scenario_id, space)
    path = root / "scenario.yaml"
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return {}
    return data


def read_content(scenario_id: str, *, space: str = "workspace") -> Dict[str, Any]:
    """
    Read scenario.json for a given scenario id. Returns {} if missing/invalid.

    When ``space="dev"`` the loader looks under ``dev_scenarios_dir``.
    """
    key = (str(scenario_id), str(space))
    cached = _CONTENT_CACHE.get(key)
    if cached is not None:
        return cached

    root = scenario_root_for_space(scenario_id, space)
    path = root / "scenario.json"
    if not path.exists():
        _log.debug("scenario '%s' has no scenario.json at %s", scenario_id, path)
        _CONTENT_CACHE[key] = {}
        return {}
    _log.debug("reading scenario '%s' content from %s", scenario_id, path)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        _CONTENT_CACHE[key] = {}
        return {}
    if not isinstance(data, dict):
        _CONTENT_CACHE[key] = {}
        return {}
    _CONTENT_CACHE[key] = data
    return data


__all__ = ["scenario_root", "read_manifest", "read_content"]
