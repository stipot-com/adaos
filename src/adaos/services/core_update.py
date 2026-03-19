from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from string import Formatter
from typing import Any


def _base_dir() -> Path:
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".adaos").resolve()


def _state_root() -> Path:
    root = _base_dir() / "state" / "core_update"
    root.mkdir(parents=True, exist_ok=True)
    return root


def plan_path() -> Path:
    return _state_root() / "plan.json"


def status_path() -> Path:
    return _state_root() / "status.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_plan() -> dict[str, Any] | None:
    plan = _read_json(plan_path())
    if not isinstance(plan, dict):
        return None
    try:
        expires_at = float(plan.get("expires_at") or 0.0)
    except Exception:
        expires_at = 0.0
    if expires_at and time.time() > expires_at:
        clear_plan()
        write_status(
            {
                "state": "expired",
                "message": "pending update expired before autostart runner picked it up",
                "updated_at": time.time(),
            }
        )
        return None
    return plan


def write_plan(payload: dict[str, Any]) -> None:
    _write_json(plan_path(), payload)


def clear_plan() -> None:
    try:
        plan_path().unlink(missing_ok=True)
    except Exception:
        pass


def read_status() -> dict[str, Any]:
    return _read_json(status_path()) or {"state": "idle", "updated_at": time.time()}


def write_status(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged.setdefault("updated_at", time.time())
    _write_json(status_path(), merged)
    return merged


def _repo_root() -> Path | None:
    try:
        return Path(__file__).resolve().parents[3]
    except Exception:
        return None


def _format_update_command(template: str, plan: dict[str, Any]) -> str:
    values = {
        "target_rev": str(plan.get("target_rev") or ""),
        "target_version": str(plan.get("target_version") or ""),
        "reason": str(plan.get("reason") or ""),
        "base_dir": str(_base_dir()),
        "python": sys.executable,
        "repo_root": str(_repo_root() or ""),
    }
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    for field in fields:
        values.setdefault(field, "")
    return template.format(**values)


def configured_update_command(plan: dict[str, Any]) -> str | None:
    cmd = str(os.getenv("ADAOS_CORE_UPDATE_CMD") or "").strip()
    if not cmd:
        return None
    try:
        return _format_update_command(cmd, plan)
    except Exception:
        return cmd


def execute_pending_update(plan: dict[str, Any]) -> dict[str, Any]:
    command = configured_update_command(plan)
    started_at = time.time()
    if not command:
        return write_status(
            {
                "state": "failed",
                "phase": "apply",
                "message": "ADAOS_CORE_UPDATE_CMD is not configured",
                "started_at": started_at,
                "finished_at": time.time(),
                "plan": plan,
            }
        )

    write_status(
        {
            "state": "applying",
            "phase": "apply",
            "message": "running configured core update command",
            "command": command,
            "started_at": started_at,
            "plan": plan,
        }
    )
    completed = subprocess.run(command, shell=True, capture_output=True, text=True)
    payload = {
        "state": "succeeded" if completed.returncode == 0 else "failed",
        "phase": "apply",
        "message": "core update command completed" if completed.returncode == 0 else "core update command failed",
        "command": command,
        "started_at": started_at,
        "finished_at": time.time(),
        "returncode": int(completed.returncode),
        "stdout": (completed.stdout or "")[-8000:],
        "stderr": (completed.stderr or "")[-8000:],
        "plan": plan,
    }
    return write_status(payload)
