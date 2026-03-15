from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from adaos.services.agent_context import get_ctx

_log = logging.getLogger("adaos.nlu.teacher.store")


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = getattr(ctx.paths, "state_dir", None)
    base = raw() if callable(raw) else raw
    return Path(base or ".adaos/state").resolve()


def teacher_state_path(webspace_id: str) -> Path:
    # Keep this under the AdaOS state dir so it survives YJS reload/reset.
    # We treat it as "teacher skill memory" even though it is managed by the hub.
    root = _state_dir() / "skills" / "nlu_teacher"
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in webspace_id if ch.isalnum() or ch in ("_", "-", ".")) or "default"
    return root / f"{safe}.json"


def load_teacher_state(*, webspace_id: str) -> dict[str, Any]:
    path = teacher_state_path(webspace_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("failed to read teacher state path=%s", path, exc_info=True)
        return {}


def save_teacher_state(*, webspace_id: str, teacher: Mapping[str, Any]) -> None:
    path = teacher_state_path(webspace_id)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(teacher, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        _log.warning("failed to write teacher state path=%s", path, exc_info=True)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

