from __future__ import annotations

import os
from pathlib import Path


def _ctx_path(attr: str) -> Path | None:
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        getter = getattr(ctx.paths, attr)
        raw = getter() if callable(getter) else getter
        return Path(raw).expanduser().resolve()
    except Exception:
        return None


def current_base_dir() -> Path:
    base_dir = _ctx_path("base_dir")
    if base_dir is not None:
        return base_dir
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        from adaos.services.settings import Settings

        return Path(Settings.from_sources().base_dir).expanduser().resolve()
    except Exception:
        return (Path.home() / ".adaos").resolve()


def current_state_dir() -> Path:
    state_dir = _ctx_path("state_dir")
    if state_dir is not None:
        return state_dir
    return (current_base_dir() / "state").resolve()


def current_logs_dir() -> Path:
    logs_dir = _ctx_path("logs_dir")
    if logs_dir is not None:
        return logs_dir
    return (current_base_dir() / "logs").resolve()


def current_repo_root() -> Path | None:
    raw = str(os.getenv("ADAOS_ROOT_REPO_ROOT") or os.getenv("ADAOS_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    repo_root = _ctx_path("repo_root")
    if repo_root is not None:
        return repo_root
    package_dir = _ctx_path("package_path")
    if package_dir is not None:
        try:
            return package_dir.parents[1].resolve()
        except Exception:
            return None
    return None
