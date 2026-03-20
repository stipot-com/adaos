"""Persistent skill-local JSON store backed by the runtime skill env file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.errors import SdkRuntimeNotInitialized

__all__ = [
    "get_env",
    "set_env",
    "delete_env",
    "read_env",
    "write_env",
    "skill_env_path",
]


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _current_skill_dir() -> Path:
    ctx = require_ctx("sdk.data.skill_env")
    current = ctx.skill_ctx.get()
    if current is None or getattr(current, "path", None) is None:
        raise SdkRuntimeNotInitialized("sdk.data.skill_env", "current skill is not set")
    return Path(current.path)


def _current_ctx_and_skill():
    try:
        ctx = require_ctx("sdk.data.skill_env")
    except Exception:
        return None, None
    current = ctx.skill_ctx.get()
    if current is None or getattr(current, "path", None) is None:
        return ctx, None
    return ctx, current


def _runtime_env_path_from_skill_dir(skill_dir: Path) -> Path | None:
    resolved = skill_dir.expanduser().resolve()
    parts = resolved.parts
    try:
        idx = parts.index(".runtime")
    except ValueError:
        return None
    if len(parts) <= idx + 1:
        return None
    runtime_root = Path(*parts[: idx + 2])
    return runtime_root / "data" / "db" / "skill_env.json"


def _runtime_env_path_from_ctx() -> Path | None:
    ctx, current = _current_ctx_and_skill()
    if ctx is None or current is None:
        return None

    current_dir = Path(current.path)
    direct = _runtime_env_path_from_skill_dir(current_dir)
    if direct is not None:
        return direct

    current_name = str(getattr(current, "name", "") or "").strip()
    if not current_name:
        return None

    for attr_name in ("skills_dir", "dev_skills_dir"):
        attr = getattr(ctx.paths, attr_name, None)
        if attr is None:
            continue
        root = Path(attr() if callable(attr) else attr)
        runtime_root = root / ".runtime" / current_name
        if runtime_root.exists():
            return runtime_root / "data" / "db" / "skill_env.json"
    return None


def skill_env_path() -> Path:
    path = _runtime_env_path_from_ctx()
    if path is None:
        override = os.getenv("ADAOS_SKILL_ENV_PATH") or os.getenv("ADAOS_SKILL_MEMORY_PATH")
        if override:
            path = Path(override)
        else:
            current_dir = _current_skill_dir()
            path = _runtime_env_path_from_skill_dir(current_dir) or (current_dir / ".skill_env.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _legacy_paths(target: Path) -> list[Path]:
    candidates: list[Path] = []
    current_dir: Path | None = None
    try:
        current_dir = _current_skill_dir()
    except Exception:
        current_dir = None

    local_legacy = target.with_name(".skill_memory.json")
    if local_legacy != target:
        candidates.append(local_legacy)
    if target.parent.name == "db":
        for legacy in (
            target.parents[1] / ".skill_memory.json",
            target.parents[1] / ".skill_env.json",
            target.parent / ".skill_env.json",
            target.parents[1] / "files" / ".skill_env.json",
        ):
            if legacy != target and legacy not in candidates:
                candidates.append(legacy)
    if current_dir is not None:
        for legacy in (current_dir / ".skill_memory.json", current_dir / ".skill_env.json"):
            if legacy != target and legacy not in candidates:
                candidates.append(legacy)
    return candidates


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_env() -> dict[str, Any]:
    target = skill_env_path()
    merged = _read_json_object(target) if target.exists() else {}
    changed = not target.exists()
    for legacy in _legacy_paths(target):
        if not legacy.exists() or not legacy.is_file():
            continue
        payload = _read_json_object(legacy)
        if not payload:
            continue
        merged = _deep_merge(payload, merged)
        changed = True
    if changed and merged:
        _write_json_object(target, merged)
    return merged


def write_env(payload: Mapping[str, Any]) -> None:
    _write_json_object(skill_env_path(), payload)


def get_env(key: str, default: Any | None = None) -> Any:
    return read_env().get(key, default)


def set_env(key: str, value: Any) -> None:
    payload = read_env()
    payload[key] = value
    write_env(payload)


def delete_env(key: str) -> None:
    payload = read_env()
    if key in payload:
        payload.pop(key, None)
        write_env(payload)
