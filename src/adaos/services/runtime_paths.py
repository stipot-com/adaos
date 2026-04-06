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
