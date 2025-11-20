from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import yaml

from adaos.services.agent_context import get_ctx


def scenario_root(scenario_id: str) -> Path:
    """
    Resolve the filesystem root for a scenario, e.g.:
      <base_dir>/.adaos/workspace/scenarios/<scenario_id>/
    """
    ctx = get_ctx()
    root = ctx.paths.scenarios_dir() / scenario_id
    return root


def read_manifest(scenario_id: str) -> Dict[str, Any]:
    """
    Read scenario.yaml for a given scenario id. Returns {} if missing.
    """
    root = scenario_root(scenario_id)
    path = root / "scenario.yaml"
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return {}
    return data


def read_content(scenario_id: str) -> Dict[str, Any]:
    """
    Read scenario.json for a given scenario id. Returns {} if missing/invalid.
    """
    root = scenario_root(scenario_id)
    path = root / "scenario.json"
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


__all__ = ["scenario_root", "read_manifest", "read_content"]
