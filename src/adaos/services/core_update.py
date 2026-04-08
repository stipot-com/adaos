from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from string import Formatter
from typing import Any

from adaos.domain import Event as DomainEvent
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import (
    activate_slot,
    active_slot,
    choose_inactive_slot,
    previous_slot,
    read_slot_manifest,
    rollback_to_previous_slot,
    slot_dir,
)
from adaos.services.runtime_paths import current_base_dir


def _base_dir() -> Path:
    return current_base_dir()


def _state_root() -> Path:
    root = _base_dir() / "state" / "core_update"
    root.mkdir(parents=True, exist_ok=True)
    return root


def plan_path() -> Path:
    return _state_root() / "plan.json"


def status_path() -> Path:
    return _state_root() / "status.json"


def last_result_path() -> Path:
    return _state_root() / "last_result.json"


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


def read_last_result() -> dict[str, Any] | None:
    payload = _read_json(last_result_path())
    return payload if isinstance(payload, dict) else None


def _is_terminal_status(payload: dict[str, Any]) -> bool:
    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("phase") or "").strip().lower()
    if state in {"failed", "validated", "succeeded", "rolled_back", "expired", "cancelled"}:
        return True
    return bool(state == "idle" and phase == "validate")


def write_status(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged.setdefault("updated_at", time.time())
    _write_json(status_path(), merged)
    if _is_terminal_status(merged):
        _write_json(last_result_path(), merged)
    try:
        get_ctx().bus.publish(
            DomainEvent(
                type="core.update.status",
                payload=dict(merged),
                source="core.update",
                ts=float(merged.get("updated_at") or time.time()),
            )
        )
    except Exception:
        pass
    return merged


def finalize_runtime_boot_status() -> dict[str, Any] | None:
    current = read_status()
    state = str(current.get("state") or "").strip().lower()
    phase = str(current.get("phase") or "").strip().lower()
    if state == "succeeded" and phase == "validate":
        return current
    if state not in {"restarting", "applying", "validated"} and not (
        state == "succeeded" and phase in {"", "apply", "launch", "shutdown"}
    ):
        return None

    now = time.time()
    slot = str(current.get("target_slot") or active_slot() or "").strip().upper()
    manifest = read_slot_manifest(slot) if slot else None
    payload = dict(current)
    payload["state"] = "succeeded"
    payload["phase"] = "validate"
    payload["message"] = (
        f"runtime boot validated on slot {slot}" if slot else "runtime boot validated"
    )
    payload["validated_at"] = now
    payload["finished_at"] = float(payload.get("finished_at") or now)
    if slot:
        payload["target_slot"] = slot
    if isinstance(manifest, dict) and manifest:
        payload["manifest"] = manifest
    return write_status(payload)


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


def rollback_installed_skill_runtimes() -> dict[str, Any]:
    try:
        from adaos.adapters.db import SqliteSkillRegistry
        from adaos.services.skill.manager import SkillManager
    except Exception as exc:
        return {
            "ok": False,
            "total": 0,
            "failed_total": 1,
            "rollback_total": 0,
            "skipped_total": 0,
            "skills": [],
            "error": f"skill rollback helpers unavailable: {exc}",
        }

    try:
        ctx = get_ctx()
        mgr = SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
        )
        reg = SqliteSkillRegistry(ctx.sql)
    except Exception as exc:
        return {
            "ok": False,
            "total": 0,
            "failed_total": 1,
            "rollback_total": 0,
            "skipped_total": 0,
            "skills": [],
            "error": f"skill rollback init failed: {exc}",
        }

    items: list[dict[str, Any]] = []
    for row in reg.list():
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name or not bool(getattr(row, "installed", True)):
            continue
        skill_name = str(name)
        entry: dict[str, Any] = {
            "skill": skill_name,
            "ok": True,
            "skipped": False,
        }
        try:
            entry["restored_slot"] = str(mgr.rollback_runtime(skill_name) or "")
        except Exception as exc:
            error_text = str(exc)
            lowered = error_text.lower()
            if (
                "no previous slot recorded" in lowered
                or "previous slot matches current" in lowered
                or "no active version" in lowered
            ):
                entry["skipped"] = True
                entry["reason"] = error_text
            else:
                entry["ok"] = False
                entry["error"] = error_text
        items.append(entry)

    failed_total = sum(1 for item in items if not bool(item.get("ok")))
    rollback_total = sum(1 for item in items if bool(item.get("restored_slot")))
    skipped_total = sum(1 for item in items if bool(item.get("skipped")))
    return {
        "ok": failed_total == 0,
        "total": len(items),
        "failed_total": failed_total,
        "rollback_total": rollback_total,
        "skipped_total": skipped_total,
        "skills": items,
    }


def _repo_current_branch(repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    if root is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return ""
    branch = str(completed.stdout or "").strip()
    return "" if branch.upper() == "HEAD" else branch


def _shared_dotenv_path() -> str:
    raw = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    if raw:
        return raw
    slot = active_slot()
    manifest = read_slot_manifest(slot) if slot else None
    env = manifest.get("env") if isinstance(manifest, dict) else None
    if not isinstance(env, dict):
        return ""
    return str(env.get("ADAOS_SHARED_DOTENV_PATH") or "").strip()


def _format_update_command(template: str, plan: dict[str, Any]) -> str:
    repo_root = _repo_root()
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
        "repo_root": str(repo_root or ""),
        "source_repo_root": str(repo_root or ""),
        "shared_dotenv_path": _shared_dotenv_path(),
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
        ' --source-repo-root "{source_repo_root}"'
        ' --shared-dotenv-path "{shared_dotenv_path}"'
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
    if not str(payload.get("target_rev") or "").strip():
        active_manifest = read_slot_manifest(payload["active_slot"]) if payload["active_slot"] else None
        resolved_rev = str(
            (active_manifest or {}).get("target_rev")
            or os.getenv("ADAOS_REV")
            or os.getenv("ADAOS_INIT_REV")
            or _repo_current_branch()
            or ""
        ).strip()
        payload["target_rev"] = resolved_rev
    return payload


def execute_pending_update(plan: dict[str, Any]) -> dict[str, Any]:
    action = str(plan.get("action") or "update").strip().lower()
    if action == "rollback":
        restored = rollback_to_previous_slot()
        skill_runtime_rollback = rollback_installed_skill_runtimes() if restored else {}
        if restored:
            payload = {
                "state": "rolled_back",
                "phase": "rollback",
                "message": f"rolled back to slot {restored}",
                "restored_slot": restored,
                "finished_at": time.time(),
                "plan": plan,
            }
            if skill_runtime_rollback:
                payload["skill_runtime_rollback"] = skill_runtime_rollback
                if not bool(skill_runtime_rollback.get("ok")):
                    payload["message"] += " | some skill runtime rollbacks failed"
            return write_status(payload)
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
