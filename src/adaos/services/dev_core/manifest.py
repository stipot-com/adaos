"""Manifest helpers for dev artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

from adaos.services.agent_context import get_ctx
from adaos.services.fs.safe_io import write_text_atomic

from .types import Kind


def _manifest_name(kind: Kind) -> str:
    return "skill.yaml" if kind == "skill" else "scenario.yaml"


def manifest_path(root: str, kind: Kind) -> Path:
    return Path(root) / _manifest_name(kind)


def read(root: str, kind: Kind) -> Dict:
    path = manifest_path(root, kind)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data


def write(root: str, kind: Kind, data: Dict) -> None:
    path = manifest_path(root, kind)
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    ctx = get_ctx()
    write_text_atomic(str(path), rendered, ctx.fs)


def update_name_and_prototype(root: str, kind: Kind, name: str, prototype: str | None) -> Dict:
    data = read(root, kind)
    data["name"] = name
    if prototype:
        data["prototype"] = prototype
    else:
        data.pop("prototype", None)
    write(root, kind, data)
    return data
