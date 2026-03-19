from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from string import Formatter
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import activate_slot, active_slot, choose_inactive_slot, previous_slot, read_slot_manifest, rollback_to_previous_slot, slot_dir


def _base_dir() -> Path:
    try:
        ctx = get_ctx()
        base = ctx.paths.base_dir()
        base = base() if callable(base) else base
        return Path(base).expanduser().resolve()
    except Exception:
        pass
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
        ctx = get_ctx()
        package = ctx.paths.package_path()
        package = package() if callable(package) else package
        return Path(package).resolve().parents[1]
    except Exception:
        try:
            return Path(__file__).resolve().parents[3]
        except Exception:
            return None


def _format_update_command(template: str, plan: dict[str, Any]) -> str:
    values = {
        "target_rev": str(plan.get("target_rev") or ""),
        "target_version": str(plan.get("target_version") or ""),
        "target_slot": str(plan.get("target_slot") or ""),
        "inactive_slot": str(plan.get("inactive_slot") or ""),
        "inactive_slot_dir": str(plan.get("inactive_slot_dir") or ""),
        "active_slot": str(plan.get("active_slot") or ""),
        "active_slot_dir": str(plan.get("active_slot_dir") or ""),
        "reason": str(plan.get("reason") or ""),
        "base_dir": str(_base_dir()),
        "python": sys.executable,
        "repo_root": str(_repo_root() or ""),
    }
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    for field in fields:
        values.setdefault(field, "")
    return template.format(**values)


def _default_update_command_template() -> str:
    return (
        '"{python}" -m adaos.apps.core_update_apply'
        ' --target-rev "{target_rev}"'
        ' --target-version "{target_version}"'
        ' --slot "{target_slot}"'
        ' --slot-dir "{inactive_slot_dir}"'
        ' --base-dir "{base_dir}"'
        ' --repo-root "{repo_root}"'
    )


def configured_update_command(plan: dict[str, Any]) -> str | None:
    cmd = str(os.getenv("ADAOS_CORE_UPDATE_CMD") or "").strip()
    if not cmd:
        cmd = _default_update_command_template()
    try:
        return _format_update_command(cmd, plan)
    except Exception:
        return cmd


def _plan_with_slot_context(plan: dict[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    payload["active_slot"] = active_slot() or ""
    payload["previous_slot"] = previous_slot() or ""
    payload["target_slot"] = str(plan.get("target_slot") or choose_inactive_slot())
    payload["inactive_slot"] = payload["target_slot"]
    payload["inactive_slot_dir"] = str(slot_dir(payload["target_slot"]))
    if payload["active_slot"]:
        payload["active_slot_dir"] = str(slot_dir(payload["active_slot"]))
    else:
        payload["active_slot_dir"] = ""
    return payload


def execute_pending_update(plan: dict[str, Any]) -> dict[str, Any]:
    action = str(plan.get("action") or "update").strip().lower()
    if action == "rollback":
        restored = rollback_to_previous_slot()
        if restored:
            return write_status(
                {
                    "state": "rolled_back",
                    "phase": "rollback",
                    "message": f"rolled back to slot {restored}",
                    "restored_slot": restored,
                    "finished_at": time.time(),
                    "plan": plan,
                }
            )
        return write_status(
            {
                "state": "failed",
                "phase": "rollback",
                "message": "no previous slot available for rollback",
                "finished_at": time.time(),
                "plan": plan,
            }
        )

    slot_plan = _plan_with_slot_context(plan)
    command = configured_update_command(slot_plan)
    started_at = time.time()
    if not command:
        return write_status(
            {
                "state": "failed",
                "phase": "apply",
                "message": "ADAOS_CORE_UPDATE_CMD is not configured",
                "started_at": started_at,
                "finished_at": time.time(),
                "plan": slot_plan,
            }
        )

    write_status(
        {
            "state": "applying",
            "phase": "apply",
            "message": "running core update command",
            "command": command,
            "started_at": started_at,
            "plan": slot_plan,
        }
    )
    completed = subprocess.run(command, shell=True, capture_output=True, text=True)
    target_slot = str(slot_plan.get("target_slot") or "")
    manifest = read_slot_manifest(target_slot) if target_slot else None
    manifest_ready = isinstance(manifest, dict) and (
        isinstance(manifest.get("argv"), list) or str(manifest.get("command") or "").strip()
    )
    ok = completed.returncode == 0 and manifest_ready
    if ok and target_slot:
        activate_slot(target_slot)
    payload = {
        "state": "succeeded" if ok else "failed",
        "phase": "apply",
        "message": (
            f"core update command completed; activated slot {target_slot}"
            if ok
            else (
                "core update command completed but slot manifest is missing or incomplete"
                if completed.returncode == 0
                else "core update command failed"
            )
        ),
        "command": command,
        "started_at": started_at,
        "finished_at": time.time(),
        "returncode": int(completed.returncode),
        "stdout": (completed.stdout or "")[-8000:],
        "stderr": (completed.stderr or "")[-8000:],
        "target_slot": target_slot,
        "manifest": manifest,
        "plan": slot_plan,
    }
    return write_status(payload)
