from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from string import Formatter
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency in some environments
    psutil = None  # type: ignore

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from adaos.apps.api.auth import require_token
from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import _advertise_base, _uvicorn_loop_mode
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import (
    activate_slot,
    active_slot,
    active_slot_manifest,
    choose_inactive_slot,
    read_slot_manifest,
    rollback_to_previous_slot,
    slot_status as core_slot_status,
    validate_slot_structure,
)
from adaos.services.core_update import clear_plan as clear_core_update_plan
from adaos.services.core_update import manifest_requires_root_promotion
from adaos.services.core_update import prepare_pending_update
from adaos.services.core_update import promote_root_from_slot
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_plan as read_core_update_plan
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.core_update import rollback_installed_skill_runtimes
from adaos.services.core_update import write_plan as write_core_update_plan
from adaos.services.core_update import write_status as write_core_update_status
from adaos.services.realtime_sidecar import (
    realtime_sidecar_enabled,
    realtime_sidecar_listener_snapshot,
    restart_realtime_sidecar_subprocess,
    start_realtime_sidecar_subprocess,
    stop_realtime_sidecar_subprocess,
)
from adaos.services.runtime_paths import current_base_dir


_SKIP_PENDING_UPDATE_ENV = "ADAOS_SKIP_PENDING_CORE_UPDATE"
_LOG = logging.getLogger("adaos.supervisor")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS supervisor")
    parser.add_argument("--host", default="127.0.0.1", help="Managed runtime host")
    parser.add_argument("--port", type=int, default=8777, help="Managed runtime port")
    parser.add_argument("--token", default=None)
    return parser.parse_known_args()[0]


def _resolved_token(raw_token: str | None = None) -> str | None:
    token = str(raw_token or os.getenv("ADAOS_TOKEN") or "").strip()
    if token:
        return token
    try:
        return str(get_ctx().config.token or "").strip() or None
    except Exception:
        return None


def _supervisor_host() -> str:
    return str(os.getenv("ADAOS_SUPERVISOR_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _supervisor_port() -> int:
    try:
        return int(str(os.getenv("ADAOS_SUPERVISOR_PORT") or "8776").strip() or "8776")
    except Exception:
        return 8776


def _supervisor_base_url() -> str:
    return f"http://{_supervisor_host()}:{_supervisor_port()}"


def _supervisor_state_dir() -> Path:
    path = (current_base_dir() / "state" / "supervisor").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _supervisor_runtime_state_path() -> Path:
    return (_supervisor_state_dir() / "runtime.json").resolve()


def _supervisor_update_attempt_path() -> Path:
    return (_supervisor_state_dir() / "update_attempt.json").resolve()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _local_update_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "status": read_core_update_status(),
        "last_result": read_core_update_last_result(),
        "plan": read_core_update_plan(),
        "slots": core_slot_status(),
        "active_manifest": active_slot_manifest(),
        "_local_fallback": True,
    }


def _update_attempt_timeout_sec() -> float:
    try:
        return max(10.0, float(str(os.getenv("ADAOS_SUPERVISOR_UPDATE_TIMEOUT_SEC") or "180").strip()))
    except Exception:
        return 180.0


def _min_update_period_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_MIN_UPDATE_PERIOD_SEC") or "300").strip()))
    except Exception:
        return 300.0


def _slot_runtime_ports(primary_port: int) -> dict[str, int]:
    fallback_a = int(primary_port)
    fallback_b = fallback_a + 1
    try:
        slot_a = int(str(os.getenv("ADAOS_SUPERVISOR_SLOT_A_PORT") or fallback_a).strip() or fallback_a)
    except Exception:
        slot_a = fallback_a
    try:
        slot_b = int(str(os.getenv("ADAOS_SUPERVISOR_SLOT_B_PORT") or fallback_b).strip() or fallback_b)
    except Exception:
        slot_b = fallback_b
    if slot_a <= 0:
        slot_a = fallback_a
    if slot_b <= 0:
        slot_b = fallback_b
    return {"A": slot_a, "B": slot_b}


def _slot_runtime_port(slot: str | None, primary_port: int) -> int:
    slot_name = str(slot or "").strip().upper()
    return int(_slot_runtime_ports(primary_port).get(slot_name, int(primary_port)))


def _warm_switch_enabled() -> bool:
    raw = os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _warm_switch_min_available_bytes() -> int:
    try:
        return max(0, int(float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_MIN_AVAILABLE_MB") or "256").strip()) * 1024 * 1024))
    except Exception:
        return 256 * 1024 * 1024


def _warm_switch_min_candidate_bytes() -> int:
    try:
        return max(0, int(float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_MIN_CANDIDATE_MB") or "192").strip()) * 1024 * 1024))
    except Exception:
        return 192 * 1024 * 1024


def _warm_switch_rss_multiplier() -> float:
    try:
        return max(1.0, float(str(os.getenv("ADAOS_SUPERVISOR_WARM_SWITCH_RSS_MULTIPLIER") or "1.15").strip()))
    except Exception:
        return 1.15


def _warm_switch_candidate_ready_timeout_sec() -> float:
    try:
        return max(0.0, float(str(os.getenv("ADAOS_SUPERVISOR_CANDIDATE_READY_TIMEOUT_SEC") or "12").strip()))
    except Exception:
        return 12.0


def _terminal_update_states() -> set[str]:
    return {"failed", "validated", "succeeded", "rolled_back", "expired", "cancelled", "idle"}


def _new_runtime_instance_id(*, slot: str | None, transition_role: str) -> str:
    slot_token = str(slot or "x").strip().lower() or "x"
    role_token = str(transition_role or "active").strip().lower() or "active"
    return f"rt-{slot_token}-{role_token[:1]}-{uuid.uuid4().hex[:8]}"


def _read_update_attempt() -> dict[str, Any] | None:
    payload = _read_json(_supervisor_update_attempt_path())
    return payload if isinstance(payload, dict) else None


def _write_update_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged.setdefault("updated_at", time.time())
    _write_json(_supervisor_update_attempt_path(), merged)
    return merged


def _epoch(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _status_updated_at(payload: dict[str, Any]) -> float:
    for key in ("updated_at", "validated_at", "finished_at", "started_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return value
    return 0.0


def _attempt_transition_at(payload: dict[str, Any]) -> float:
    for key in ("transitioned_at", "scheduled_for", "requested_at", "updated_at", "created_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0.0:
            return value
    return 0.0


def _is_terminal_update_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("state") or "").strip().lower() in _terminal_update_states()


def _is_root_restart_pending_attempt(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("state") or "").strip().lower() == "awaiting_root_restart"


def _is_root_restart_completed_status(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("phase") or "").strip().lower()
    return state == "succeeded" and phase == "validate" and float(payload.get("root_restart_completed_at") or 0.0) > 0.0


def _is_transition_in_progress(status: dict[str, Any] | None, attempt: dict[str, Any] | None) -> bool:
    status_map = status if isinstance(status, dict) else {}
    attempt_map = attempt if isinstance(attempt, dict) else {}
    state = str(status_map.get("state") or "").strip().lower()
    phase = str(status_map.get("phase") or "").strip().lower()
    attempt_state = str(attempt_map.get("state") or "").strip().lower()
    if attempt_state in {"active", "awaiting_root_restart"}:
        return True
    if state in {"preparing", "countdown", "draining", "stopping", "restarting", "applying"}:
        return True
    if state == "validated" and phase == "root_promotion_pending":
        return True
    if state == "succeeded" and phase == "root_promoted":
        return True
    return False


def _transition_request_payload(
    *,
    action: str,
    target_rev: str,
    target_version: str,
    reason: str,
    countdown_sec: float,
    drain_timeout_sec: float,
    signal_delay_sec: float,
    requested_at: float | None = None,
) -> dict[str, Any]:
    return {
        "action": str(action or "update"),
        "target_rev": str(target_rev or ""),
        "target_version": str(target_version or ""),
        "reason": str(reason or ""),
        "countdown_sec": float(countdown_sec),
        "drain_timeout_sec": float(drain_timeout_sec),
        "signal_delay_sec": float(signal_delay_sec),
        "requested_at": float(requested_at or time.time()),
    }


def _request_from_attempt(attempt: dict[str, Any] | None) -> dict[str, Any]:
    data = attempt if isinstance(attempt, dict) else {}
    return _transition_request_payload(
        action=str(data.get("action") or "update"),
        target_rev=str(data.get("target_rev") or ""),
        target_version=str(data.get("target_version") or ""),
        reason=str(data.get("reason") or ""),
        countdown_sec=float(data.get("countdown_sec") or 0.0),
        drain_timeout_sec=float(data.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(data.get("signal_delay_sec") or 0.25),
        requested_at=_epoch(data.get("requested_at")) or time.time(),
    )


def _subsequent_transition_request(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    data = attempt if isinstance(attempt, dict) else {}
    queued = data.get("subsequent_transition_request")
    return dict(queued) if isinstance(queued, dict) and queued else None


def _last_update_completion_at(status: dict[str, Any] | None, attempt: dict[str, Any] | None) -> float:
    attempt_map = attempt if isinstance(attempt, dict) else {}
    if str(attempt_map.get("action") or "").strip().lower() == "update":
        completed_at = _epoch(attempt_map.get("completed_at"))
        if completed_at > 0.0:
            return completed_at
        updated_at = _epoch(attempt_map.get("updated_at"))
        if updated_at > 0.0 and str(attempt_map.get("state") or "").strip().lower() in {"completed", "failed", "cancelled"}:
            return updated_at
    status_map = status if isinstance(status, dict) else {}
    if str(status_map.get("action") or "").strip().lower() != "update":
        return 0.0
    if not _is_terminal_update_status(status_map):
        return 0.0
    return max(
        _epoch(status_map.get("root_restart_completed_at")),
        _epoch(status_map.get("finished_at")),
        _status_updated_at(status_map),
    )


def _build_attempt_payload(*, action: str, request: dict[str, Any], status: dict[str, Any] | None, accepted: bool) -> dict[str, Any]:
    now = time.time()
    current_status = dict(status or {})
    countdown_sec = float(request.get("countdown_sec") or current_status.get("countdown_sec") or 0.0)
    scheduled_for = float(current_status.get("scheduled_for") or (now + countdown_sec))
    return {
        "state": "active" if accepted else "rejected",
        "action": str(action or current_status.get("action") or "update"),
        "requested_at": now,
        "transitioned_at": scheduled_for if accepted else now,
        "countdown_sec": countdown_sec,
        "drain_timeout_sec": float(request.get("drain_timeout_sec") or current_status.get("drain_timeout_sec") or 0.0),
        "signal_delay_sec": float(request.get("signal_delay_sec") or current_status.get("signal_delay_sec") or 0.0),
        "target_rev": str(request.get("target_rev") or current_status.get("target_rev") or ""),
        "target_version": str(request.get("target_version") or current_status.get("target_version") or ""),
        "reason": str(request.get("reason") or current_status.get("reason") or ""),
        "accepted": bool(accepted),
        "scheduled_for": scheduled_for if accepted else None,
        "min_update_period_sec": float(request.get("min_update_period_sec") or current_status.get("min_update_period_sec") or 0.0),
        "candidate_prewarm_state": str(
            request.get("candidate_prewarm_state") or current_status.get("candidate_prewarm_state") or ""
        ).strip()
        or None,
        "candidate_prewarm_message": str(
            request.get("candidate_prewarm_message") or current_status.get("candidate_prewarm_message") or ""
        ).strip()
        or None,
        "last_status": current_status,
        "updated_at": now,
    }


def _complete_update_attempt(*, state: str, status: dict[str, Any] | None, reason: str | None = None) -> dict[str, Any]:
    now = time.time()
    current = _read_update_attempt() or {}
    payload = dict(current)
    payload["state"] = str(state or "completed")
    payload["completed_at"] = now
    payload["updated_at"] = now
    if reason:
        payload["completion_reason"] = str(reason)
    if isinstance(status, dict):
        payload["last_status"] = dict(status)
    return _write_update_attempt(payload)


def _reconcile_update_status(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    attempt = _read_update_attempt()
    if not isinstance(attempt, dict):
        return payload

    payload["attempt"] = dict(attempt)
    if _is_root_restart_pending_attempt(attempt):
        if _is_root_restart_completed_status(status):
            payload["attempt"] = _complete_update_attempt(
                state="completed",
                status=status,
                reason="root restart completed",
            )
        return payload

    if str(attempt.get("state") or "").strip().lower() != "active":
        return payload

    if _is_terminal_update_status(status):
        payload["attempt"] = _complete_update_attempt(state="completed", status=status, reason="terminal core update status")
        return payload

    now = time.time()
    timeout_sec = _update_attempt_timeout_sec()
    status_age = max(0.0, now - _status_updated_at(status)) if _status_updated_at(status) > 0.0 else 0.0
    transition_age = max(0.0, now - _attempt_transition_at(attempt)) if _attempt_transition_at(attempt) > 0.0 else 0.0
    if max(status_age, transition_age) < timeout_sec:
        return payload

    action = str(status.get("action") or attempt.get("action") or "update")
    failed_payload: dict[str, Any] = {
        "state": "failed",
        "phase": str(status.get("phase") or "restart_timeout"),
        "action": action,
        "target_rev": str(status.get("target_rev") or attempt.get("target_rev") or ""),
        "target_version": str(status.get("target_version") or attempt.get("target_version") or ""),
        "reason": str(status.get("reason") or attempt.get("reason") or "supervisor.timeout"),
        "message": f"supervisor timed out waiting for runtime to finish {status.get('state') or 'update transition'}",
        "supervisor_timeout_sec": timeout_sec,
        "supervisor_timeout_at": now,
        "supervisor_previous_status": status,
    }
    if action == "update":
        restored = rollback_to_previous_slot()
        skill_runtime_rollback = rollback_installed_skill_runtimes() if restored else {}
        if restored:
            failed_payload["restored_slot"] = restored
            failed_payload["rollback"] = {"ok": True, "slot": restored}
            failed_payload["message"] += f"; rolled back to slot {restored}"
        if skill_runtime_rollback:
            failed_payload["skill_runtime_rollback"] = skill_runtime_rollback
            if restored and not bool(skill_runtime_rollback.get("ok")):
                failed_payload["message"] += " | some skill runtime rollbacks failed"
    failed_status = write_core_update_status(failed_payload)
    with contextlib.suppress(Exception):
        clear_core_update_plan()
    payload["status"] = failed_status
    payload["attempt"] = _complete_update_attempt(state="failed", status=failed_status, reason="restart/apply timeout")
    payload["_served_by"] = "supervisor_timeout_recovery"
    return payload


def _public_update_status_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    status = source.get("status") if isinstance(source.get("status"), dict) else {}
    runtime = source.get("runtime") if isinstance(source.get("runtime"), dict) else {}
    attempt = source.get("attempt") if isinstance(source.get("attempt"), dict) else {}
    bootstrap_update = runtime.get("bootstrap_update") if isinstance(runtime.get("bootstrap_update"), dict) else {}
    public_status = {
        "action": str(status.get("action") or "").strip().lower() or None,
        "state": str(status.get("state") or "").strip().lower() or "unknown",
        "phase": str(status.get("phase") or "").strip().lower() or "",
        "message": str(status.get("message") or "").strip(),
        "target_rev": str(status.get("target_rev") or "").strip(),
        "target_version": str(status.get("target_version") or "").strip(),
        "planned_reason": str(status.get("planned_reason") or "").strip() or None,
        "min_update_period_sec": status.get("min_update_period_sec"),
        "scheduled_for": status.get("scheduled_for"),
        "subsequent_transition": bool(status.get("subsequent_transition")),
        "subsequent_transition_requested_at": status.get("subsequent_transition_requested_at"),
        "candidate_prewarm_state": str(status.get("candidate_prewarm_state") or "").strip() or None,
        "candidate_prewarm_message": str(status.get("candidate_prewarm_message") or "").strip() or None,
        "candidate_prewarm_ready_at": status.get("candidate_prewarm_ready_at"),
        "updated_at": status.get("updated_at"),
    }
    return {
        "ok": True,
        "status": public_status,
        "attempt": {
            "action": str(attempt.get("action") or "").strip().lower() or None,
            "state": str(attempt.get("state") or "").strip().lower() or None,
            "awaiting_restart": bool(attempt.get("awaiting_restart")),
            "planned_reason": str(attempt.get("planned_reason") or "").strip() or None,
            "scheduled_for": attempt.get("scheduled_for"),
            "subsequent_transition": bool(attempt.get("subsequent_transition")),
            "subsequent_transition_requested_at": attempt.get("subsequent_transition_requested_at"),
            "candidate_prewarm_state": str(attempt.get("candidate_prewarm_state") or "").strip() or None,
            "candidate_prewarm_message": str(attempt.get("candidate_prewarm_message") or "").strip() or None,
            "updated_at": attempt.get("updated_at"),
        },
        "runtime": {
            "active_slot": str(runtime.get("active_slot") or "").strip() or None,
            "runtime_state": str(runtime.get("runtime_state") or "").strip() or None,
            "runtime_url": str(runtime.get("runtime_url") or "").strip() or None,
            "runtime_port": runtime.get("runtime_port"),
            "runtime_instance_id": str(runtime.get("runtime_instance_id") or "").strip() or None,
            "transition_role": str(runtime.get("transition_role") or "").strip() or None,
            "listener_running": bool(runtime.get("listener_running")),
            "runtime_api_ready": bool(runtime.get("runtime_api_ready")),
            "candidate_slot": str(runtime.get("candidate_slot") or "").strip() or None,
            "candidate_runtime_url": str(runtime.get("candidate_runtime_url") or "").strip() or None,
            "candidate_runtime_port": runtime.get("candidate_runtime_port"),
            "candidate_runtime_instance_id": str(runtime.get("candidate_runtime_instance_id") or "").strip() or None,
            "candidate_transition_role": str(runtime.get("candidate_transition_role") or "").strip() or None,
            "candidate_listener_running": bool(runtime.get("candidate_listener_running")),
            "candidate_runtime_api_ready": bool(runtime.get("candidate_runtime_api_ready")),
            "candidate_runtime_state": str(runtime.get("candidate_runtime_state") or "").strip() or None,
            "transition_mode": str(runtime.get("transition_mode") or "").strip() or None,
            "warm_switch_supported": runtime.get("warm_switch_supported"),
            "warm_switch_allowed": runtime.get("warm_switch_allowed"),
            "warm_switch_reason": str(runtime.get("warm_switch_reason") or "").strip() or None,
            "slot_ports": runtime.get("slot_ports") if isinstance(runtime.get("slot_ports"), dict) else {},
            "root_promotion_required": bool(
                runtime.get("root_promotion_required")
                or bootstrap_update.get("required")
            ),
        },
        "_served_by": str(source.get("_served_by") or "").strip() or "unknown",
    }


def _listener_running(host: str, port: int, *, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((str(host or "127.0.0.1"), int(port)), timeout=max(0.05, float(timeout))):
            return True
    except Exception:
        return False


def _runtime_api_ready(base_url: str, *, token: str | None, timeout: float = 0.75) -> bool:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = token
    try:
        response = requests.get(f"{base_url}/api/ping", headers=headers, timeout=max(0.1, float(timeout)))
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return False
    return bool(isinstance(payload, dict) and payload.get("ok") is True)


def _runtime_shutdown_request_timeout(*, drain_timeout_sec: float, signal_delay_sec: float) -> float:
    return max(5.0, float(drain_timeout_sec) + float(signal_delay_sec) + 2.0)


def _proc_details(proc: subprocess.Popen[Any] | None, *, cwd_hint: str | None = None) -> dict[str, Any]:
    managed_pid = None
    managed_alive = False
    managed_cmdline: list[str] = []
    managed_executable = None
    managed_cwd = None
    if proc is None:
        return {
            "managed_pid": None,
            "managed_alive": False,
            "managed_cmdline": [],
            "managed_executable": None,
            "managed_cwd": None,
        }
    try:
        managed_pid = int(proc.pid or 0) or None
        managed_alive = proc.poll() is None
        raw_args = proc.args if isinstance(proc.args, (list, tuple)) else [str(proc.args or "")]
        managed_cmdline = [str(item) for item in raw_args if str(item or "").strip()]
        managed_executable = managed_cmdline[0] if managed_cmdline else None
        managed_cwd = str(cwd_hint or getattr(proc, "cwd", None) or "").strip() or None
    except Exception:
        managed_pid = None
        managed_alive = False
        managed_cmdline = []
        managed_executable = None
        managed_cwd = None
    return {
        "managed_pid": managed_pid,
        "managed_alive": managed_alive,
        "managed_cmdline": managed_cmdline,
        "managed_executable": managed_executable,
        "managed_cwd": managed_cwd,
    }


def _format_slot_value(template: str, values: dict[str, str]) -> str:
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    payload = dict(values)
    for field in fields:
        payload.setdefault(field, "")
    return template.format(**payload)


class SupervisorManager:
    def __init__(self, *, runtime_host: str, runtime_port: int, token: str | None) -> None:
        self.runtime_host = str(runtime_host or "127.0.0.1").strip() or "127.0.0.1"
        self.runtime_port = int(runtime_port)
        self.token = str(token or "").strip() or None
        self._proc: subprocess.Popen[Any] | None = None
        self._candidate_proc: subprocess.Popen[Any] | None = None
        self._sidecar_proc: subprocess.Popen[Any] | None = None
        self._desired_running = True
        self._stopping = False
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[Any] | None = None
        self._restart_count = 0
        self._last_start_at: float | None = None
        self._last_exit_at: float | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._update_task: asyncio.Task[Any] | None = None
        self._update_task_cancel_mode: str | None = None
        self._managed_runtime_instance_id: str | None = None
        self._managed_transition_role: str | None = None
        self._managed_runtime_cwd: str | None = None
        self._candidate_slot: str | None = None
        self._candidate_runtime_instance_id: str | None = None
        self._candidate_transition_role: str | None = None
        self._candidate_runtime_cwd: str | None = None

    def _sidecar_role(self) -> str | None:
        try:
            return str(get_ctx().config.role or "").strip().lower() or None
        except Exception:
            return None

    def _sidecar_status_payload(self) -> dict[str, Any]:
        role = self._sidecar_role()
        return {
            "enabled": bool(realtime_sidecar_enabled(role=role)),
            "role": role,
            "process": realtime_sidecar_listener_snapshot(self._sidecar_proc),
        }

    def _runtime_request_json(
        self,
        *,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        session = requests.Session()
        try:
            try:
                session.trust_env = False
            except Exception:
                pass
            response = session.request(
                str(method or "GET").upper(),
                self.runtime_base_url + str(path or ""),
                headers=headers,
                json=payload,
                timeout=float(timeout),
            )
            if int(response.status_code or 0) >= 400:
                try:
                    detail: Any = response.json()
                except Exception:
                    detail = (response.text or f"runtime returned HTTP {response.status_code}").strip()[:500]
                if isinstance(detail, dict) and set(detail.keys()) == {"detail"}:
                    detail = detail["detail"]
                raise HTTPException(status_code=int(response.status_code), detail=detail)
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("runtime returned a non-object payload")
            return body
        finally:
            with contextlib.suppress(Exception):
                session.close()

    def _runtime_sidecar_runtime_payload(self) -> dict[str, Any]:
        try:
            payload = self._runtime_request_json(path="/api/node/reliability", timeout=2.0)
        except Exception:
            return {}
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        return dict(runtime.get("sidecar_runtime")) if isinstance(runtime.get("sidecar_runtime"), dict) else {}

    @property
    def active_runtime_port(self) -> int:
        return self.slot_runtime_port(active_slot())

    @property
    def runtime_base_url(self) -> str:
        return self.slot_runtime_base_url(active_slot())

    def slot_runtime_port(self, slot: str | None) -> int:
        return _slot_runtime_port(slot, self.runtime_port)

    def slot_runtime_base_url(self, slot: str | None) -> str:
        return f"http://{self.runtime_host}:{self.slot_runtime_port(slot)}"

    def slot_runtime_urls(self) -> dict[str, str]:
        ports = _slot_runtime_ports(self.runtime_port)
        return {slot_name: f"http://{self.runtime_host}:{port}" for slot_name, port in ports.items()}

    def _candidate_transition_slot(
        self,
        *,
        current_slot: str | None,
        update_status: dict[str, Any] | None,
        update_attempt: dict[str, Any] | None,
    ) -> str | None:
        for source in (update_status or {}, update_attempt or {}):
            target_slot = str(source.get("target_slot") or "").strip().upper()
            if target_slot in {"A", "B"}:
                return target_slot
        state = str((update_status or {}).get("state") or "").strip().lower()
        attempt_state = str((update_attempt or {}).get("state") or "").strip().lower()
        transition_active = state in {"planned", "preparing", "countdown", "draining", "stopping", "restarting", "applying", "validated"} or attempt_state in {"planned", "active"}
        if not transition_active and attempt_state == "awaiting_root_restart":
            transition_active = _subsequent_transition_request(update_attempt) is not None
        if transition_active:
            target_slot = choose_inactive_slot()
            if target_slot and target_slot != str(current_slot or "").strip().upper():
                return target_slot
        return None

    def _warm_switch_state(
        self,
        *,
        current_slot: str | None,
        update_status: dict[str, Any] | None,
        update_attempt: dict[str, Any] | None,
        managed_pid: int | None,
    ) -> dict[str, Any]:
        candidate_slot = self._candidate_transition_slot(
            current_slot=current_slot,
            update_status=update_status,
            update_attempt=update_attempt,
        )
        slot_ports = _slot_runtime_ports(self.runtime_port)
        active_port = self.slot_runtime_port(current_slot)
        candidate_port = self.slot_runtime_port(candidate_slot)
        supported = bool(candidate_slot) and candidate_port != active_port
        enabled = _warm_switch_enabled()
        allowed = False
        reason = "warm switch is disabled"
        available_bytes = None
        estimated_candidate_bytes = None
        reserve_bytes = _warm_switch_min_available_bytes()
        current_rss_bytes = None
        if not candidate_slot:
            reason = "no transition candidate slot"
        elif not supported:
            reason = "candidate runtime uses the same port as the active slot"
        elif not enabled:
            reason = "warm switch is disabled"
        elif psutil is None:
            reason = "psutil unavailable; cannot evaluate memory gate"
        else:
            try:
                vm = psutil.virtual_memory()
                available_bytes = int(getattr(vm, "available", 0) or 0)
            except Exception:
                available_bytes = None
            if managed_pid:
                with contextlib.suppress(Exception):
                    current_rss_bytes = int(psutil.Process(int(managed_pid)).memory_info().rss)
            estimated_candidate_bytes = max(
                _warm_switch_min_candidate_bytes(),
                int(float(current_rss_bytes or 0) * _warm_switch_rss_multiplier()),
            )
            if available_bytes is None or available_bytes <= 0:
                reason = "available memory is unknown"
            elif available_bytes < estimated_candidate_bytes + reserve_bytes:
                reason = "insufficient memory for warm switch; using stop-and-switch"
            else:
                allowed = True
                reason = "warm switch admitted"
        transition_mode = None
        if candidate_slot:
            transition_mode = "warm_switch" if supported and enabled and allowed else "stop_and_switch"
        return {
            "candidate_slot": candidate_slot,
            "candidate_runtime_port": candidate_port if candidate_slot else None,
            "candidate_runtime_url": self.slot_runtime_base_url(candidate_slot) if candidate_slot else None,
            "candidate_transition_role": "candidate" if candidate_slot else None,
            "transition_mode": transition_mode,
            "warm_switch_enabled": enabled,
            "warm_switch_supported": supported,
            "warm_switch_allowed": allowed if candidate_slot else None,
            "warm_switch_reason": reason if candidate_slot else None,
            "warm_switch_memory": {
                "available_bytes": available_bytes,
                "current_rss_bytes": current_rss_bytes,
                "estimated_candidate_bytes": estimated_candidate_bytes,
                "reserve_bytes": reserve_bytes,
            },
            "slot_ports": slot_ports,
            "slot_urls": self.slot_runtime_urls(),
        }

    def _runtime_env(
        self,
        *,
        slot: str | None,
        slot_dir: str,
        slot_port: int,
        transition_role: str,
        runtime_instance_id: str,
        skip_pending_update: bool = False,
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["ADAOS_SUPERVISOR_ENABLED"] = "1"
        env["ADAOS_SUPERVISOR_URL"] = _supervisor_base_url()
        env["ADAOS_SUPERVISOR_HOST"] = _supervisor_host()
        env["ADAOS_SUPERVISOR_PORT"] = str(_supervisor_port())
        env["ADAOS_RUNTIME_INSTANCE_ID"] = str(runtime_instance_id)
        env["ADAOS_RUNTIME_TRANSITION_ROLE"] = str(transition_role or "active")
        env["ADAOS_RUNTIME_HOST"] = self.runtime_host
        env["ADAOS_RUNTIME_PORT"] = str(slot_port)
        if self.token:
            env["ADAOS_TOKEN"] = self.token
        if slot:
            env["ADAOS_ACTIVE_CORE_SLOT"] = slot
            env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = slot_dir
        if skip_pending_update:
            env[_SKIP_PENDING_UPDATE_ENV] = "1"
        return env

    def _runtime_launch_spec(
        self,
        *,
        slot: str | None = None,
        transition_role: str = "active",
        runtime_instance_id: str | None = None,
        skip_pending_update: bool = False,
    ) -> tuple[list[str] | None, str | None, dict[str, str], str | None, str, str]:
        resolved_slot = str(slot or active_slot() or "").strip().upper() or None
        manifest = read_slot_manifest(resolved_slot) if slot else active_slot_manifest()
        slot_port = self.slot_runtime_port(resolved_slot)
        slot_dir = str(core_slot_status().get("slots", {}).get(resolved_slot or "", {}).get("path") or "")
        resolved_runtime_instance_id = str(
            runtime_instance_id or _new_runtime_instance_id(slot=resolved_slot, transition_role=transition_role)
        )
        env = self._runtime_env(
            slot=resolved_slot,
            slot_dir=slot_dir,
            slot_port=slot_port,
            transition_role=transition_role,
            runtime_instance_id=resolved_runtime_instance_id,
            skip_pending_update=skip_pending_update,
        )
        if isinstance(manifest, dict):
            manifest_env = manifest.get("env")
            if isinstance(manifest_env, dict):
                for key, value in manifest_env.items():
                    env[str(key)] = str(value)
            if resolved_slot:
                env["ADAOS_ACTIVE_CORE_SLOT"] = resolved_slot
                env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = slot_dir
            values = {
                "host": self.runtime_host,
                "port": str(slot_port),
                "token": str(self.token or ""),
                "slot": str(resolved_slot or ""),
                "slot_dir": slot_dir,
                "base_dir": str(current_base_dir()),
                "python": os.sys.executable,
                "runtime_instance_id": resolved_runtime_instance_id,
                "transition_role": str(transition_role or "active"),
            }
            argv_raw = manifest.get("argv")
            if isinstance(argv_raw, list):
                argv = [_format_slot_value(str(item), values) for item in argv_raw if str(item).strip()]
                if argv:
                    cwd = str(manifest.get("cwd") or "").strip() or None
                    return argv, None, env, cwd, resolved_runtime_instance_id, str(transition_role or "active")
            command = str(manifest.get("command") or "").strip()
            if command:
                cwd = str(manifest.get("cwd") or "").strip() or None
                return None, _format_slot_value(command, values), env, cwd, resolved_runtime_instance_id, str(
                    transition_role or "active"
                )
        return (
            [
                sys.executable,
                "-m",
                "adaos.apps.autostart_runner",
                "--host",
                self.runtime_host,
                "--port",
                str(slot_port),
            ],
            None,
            env,
            None,
            resolved_runtime_instance_id,
            str(transition_role or "active"),
        )

    def _runtime_state_payload(self) -> dict[str, Any]:
        proc = self._proc
        slot_snapshot = core_slot_status()
        current_slot = str(slot_snapshot.get("active_slot") or active_slot() or "").strip().upper() or None
        previous_slot = str(slot_snapshot.get("previous_slot") or "").strip().upper() or None
        active_manifest = active_slot_manifest()
        update_status = read_core_update_status()
        update_attempt = _read_update_attempt()
        root_promotion_required, bootstrap_update = manifest_requires_root_promotion(active_manifest)
        slot_structure = validate_slot_structure(current_slot) if current_slot else None
        active_runtime_port = self.slot_runtime_port(current_slot)
        active_runtime_url = self.slot_runtime_base_url(current_slot)
        managed = _proc_details(proc, cwd_hint=self._managed_runtime_cwd)
        managed_pid = managed["managed_pid"]
        managed_alive = bool(managed["managed_alive"])
        managed_cmdline = managed["managed_cmdline"]
        managed_executable = managed["managed_executable"]
        managed_cwd = managed["managed_cwd"]
        listener_running = bool(managed_alive) and _listener_running(self.runtime_host, active_runtime_port)
        api_ready = listener_running and _runtime_api_ready(active_runtime_url, token=self.token)
        runtime_state = "stopped"
        if self._stopping:
            runtime_state = "stopping"
        elif managed_alive and api_ready:
            runtime_state = "ready"
        elif managed_alive and listener_running:
            runtime_state = "starting"
        elif managed_alive:
            runtime_state = "spawned"
        expected_executable = None
        expected_cwd = None
        managed_matches_active_slot = None
        if isinstance(active_manifest, dict):
            argv = active_manifest.get("argv")
            if isinstance(argv, list) and argv:
                expected_executable = str(argv[0] or "").strip() or None
            expected_cwd = str(active_manifest.get("cwd") or "").strip() or None
        if expected_executable or expected_cwd:
            managed_matches_active_slot = True
            if expected_executable and str(managed_executable or "").strip() != expected_executable:
                managed_matches_active_slot = False
            if expected_cwd and str(managed_cwd or "").strip() != expected_cwd:
                managed_matches_active_slot = False
        warm_switch = self._warm_switch_state(
            current_slot=current_slot,
            update_status=update_status,
            update_attempt=update_attempt,
            managed_pid=managed_pid,
        )
        candidate_slot = str(self._candidate_slot or warm_switch.get("candidate_slot") or "").strip().upper() or None
        candidate_manifest = read_slot_manifest(candidate_slot) if candidate_slot else None
        candidate_runtime_port = self.slot_runtime_port(candidate_slot) if candidate_slot else None
        candidate_runtime_url = self.slot_runtime_base_url(candidate_slot) if candidate_slot else None
        candidate_managed = _proc_details(self._candidate_proc, cwd_hint=self._candidate_runtime_cwd)
        candidate_managed_pid = candidate_managed["managed_pid"]
        candidate_managed_alive = bool(candidate_managed["managed_alive"])
        candidate_managed_cmdline = candidate_managed["managed_cmdline"]
        candidate_managed_executable = candidate_managed["managed_executable"]
        candidate_managed_cwd = candidate_managed["managed_cwd"]
        candidate_listener_running = bool(candidate_managed_alive and candidate_runtime_port) and _listener_running(
            self.runtime_host,
            int(candidate_runtime_port or 0),
        )
        candidate_runtime_api_ready = bool(candidate_listener_running and candidate_runtime_url) and _runtime_api_ready(
            str(candidate_runtime_url),
            token=self.token,
        )
        candidate_runtime_state = None
        if candidate_slot:
            candidate_runtime_state = "stopped"
            if candidate_managed_alive and candidate_runtime_api_ready:
                candidate_runtime_state = "ready"
            elif candidate_managed_alive and candidate_listener_running:
                candidate_runtime_state = "starting"
            elif candidate_managed_alive:
                candidate_runtime_state = "spawned"
        candidate_expected_executable = None
        candidate_expected_cwd = None
        candidate_matches_candidate_slot = None
        if isinstance(candidate_manifest, dict):
            argv = candidate_manifest.get("argv")
            if isinstance(argv, list) and argv:
                candidate_expected_executable = str(argv[0] or "").strip() or None
            candidate_expected_cwd = str(candidate_manifest.get("cwd") or "").strip() or None
        if candidate_slot and (candidate_expected_executable or candidate_expected_cwd):
            candidate_matches_candidate_slot = True
            if (
                candidate_expected_executable
                and str(candidate_managed_executable or "").strip() != candidate_expected_executable
            ):
                candidate_matches_candidate_slot = False
            if candidate_expected_cwd and str(candidate_managed_cwd or "").strip() != candidate_expected_cwd:
                candidate_matches_candidate_slot = False
        return {
            "ok": True,
            "supervisor_pid": os.getpid(),
            "supervisor_url": _supervisor_base_url(),
            "sidecar": self._sidecar_status_payload(),
            "runtime_url": active_runtime_url,
            "runtime_host": self.runtime_host,
            "runtime_port": active_runtime_port,
            "runtime_instance_id": self._managed_runtime_instance_id,
            "transition_role": self._managed_transition_role if self._managed_runtime_instance_id else None,
            "active_slot": current_slot,
            "previous_slot": previous_slot,
            "desired_running": bool(self._desired_running),
            "stopping": bool(self._stopping),
            "managed_pid": managed_pid,
            "managed_alive": managed_alive,
            "listener_running": listener_running,
            "runtime_api_ready": api_ready,
            "runtime_state": runtime_state,
            "managed_cmdline": managed_cmdline,
            "managed_executable": managed_executable,
            "managed_cwd": managed_cwd,
            "expected_managed_executable": expected_executable,
            "expected_managed_cwd": expected_cwd,
            "managed_matches_active_slot": managed_matches_active_slot,
            **warm_switch,
            "candidate_slot": candidate_slot,
            "candidate_runtime_url": candidate_runtime_url,
            "candidate_runtime_port": candidate_runtime_port,
            "candidate_runtime_instance_id": self._candidate_runtime_instance_id,
            "candidate_transition_role": (
                self._candidate_transition_role
                if self._candidate_runtime_instance_id
                else str(warm_switch.get("candidate_transition_role") or "").strip() or None
            ),
            "candidate_managed_pid": candidate_managed_pid,
            "candidate_managed_alive": candidate_managed_alive,
            "candidate_listener_running": candidate_listener_running,
            "candidate_runtime_api_ready": candidate_runtime_api_ready,
            "candidate_runtime_state": candidate_runtime_state,
            "candidate_managed_cmdline": candidate_managed_cmdline,
            "candidate_managed_executable": candidate_managed_executable,
            "candidate_managed_cwd": candidate_managed_cwd,
            "candidate_expected_managed_executable": candidate_expected_executable,
            "candidate_expected_managed_cwd": candidate_expected_cwd,
            "candidate_matches_candidate_slot": candidate_matches_candidate_slot,
            "active_manifest": active_manifest,
            "root_promotion_required": root_promotion_required,
            "bootstrap_update": bootstrap_update,
            "slot_structure": slot_structure,
            "restart_count": int(self._restart_count),
            "last_start_at": self._last_start_at,
            "last_exit_at": self._last_exit_at,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
            "updated_at": time.time(),
        }

    def _persist_runtime_state(self) -> None:
        with contextlib.suppress(Exception):
            _write_json(_supervisor_runtime_state_path(), self._runtime_state_payload())

    def _local_supervisor_update_status_payload(self) -> dict[str, Any]:
        payload = _local_update_payload()
        payload["runtime"] = self.status()
        payload["_served_by"] = "supervisor_fallback"
        return _reconcile_update_status(payload)

    async def _spawn_runtime_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        argv, command, env, cwd, runtime_instance_id, transition_role = self._runtime_launch_spec()
        proc = subprocess.Popen(
            argv or command or [],
            shell=bool(command),
            cwd=cwd or os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=(os.name != "nt"),
            creationflags=(int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0),
        )
        self._proc = proc
        self._managed_runtime_instance_id = runtime_instance_id
        self._managed_transition_role = transition_role
        self._managed_runtime_cwd = str(cwd or os.getcwd())
        self._last_start_at = time.time()
        self._last_error = None
        self._persist_runtime_state()

    async def _spawn_sidecar_locked(self) -> None:
        proc = self._sidecar_proc
        if proc is not None and proc.poll() is None:
            return
        self._sidecar_proc = await start_realtime_sidecar_subprocess(role=self._sidecar_role())
        self._persist_runtime_state()

    async def _spawn_candidate_runtime_locked(self, *, slot: str) -> None:
        resolved_slot = str(slot or "").strip().upper()
        if not resolved_slot:
            raise RuntimeError("candidate slot is required")
        if resolved_slot == str(active_slot() or "").strip().upper():
            raise RuntimeError("candidate slot must differ from the active slot")
        existing = self._candidate_proc
        if (
            existing is not None
            and existing.poll() is None
            and str(self._candidate_slot or "").strip().upper() == resolved_slot
        ):
            return
        if existing is not None and existing.poll() is None:
            await self._terminate_candidate_proc_locked(graceful=True, reason="supervisor.candidate.replace")
        argv, command, env, cwd, runtime_instance_id, transition_role = self._runtime_launch_spec(
            slot=resolved_slot,
            transition_role="candidate",
            skip_pending_update=True,
        )
        proc = subprocess.Popen(
            argv or command or [],
            shell=bool(command),
            cwd=cwd or os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=(os.name != "nt"),
            creationflags=(int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0),
        )
        self._candidate_proc = proc
        self._candidate_slot = resolved_slot
        self._candidate_runtime_instance_id = runtime_instance_id
        self._candidate_transition_role = transition_role
        self._candidate_runtime_cwd = str(cwd or os.getcwd())
        self._persist_runtime_state()

    async def ensure_started(self) -> None:
        async with self._lock:
            self._stopping = False
            self._desired_running = True
            await self._spawn_runtime_locked()

    async def ensure_sidecar_started(self) -> dict[str, Any]:
        async with self._lock:
            await self._spawn_sidecar_locked()
            self._persist_runtime_state()
            return self._sidecar_status_payload()

    async def _terminate_proc_locked(
        self,
        *,
        proc: subprocess.Popen[Any] | None = None,
        base_url: str | None = None,
        graceful: bool,
        reason: str,
    ) -> None:
        proc = proc or self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            return
        if graceful:
            try:
                headers = {"Content-Type": "application/json"}
                if self.token:
                    headers["X-AdaOS-Token"] = self.token
                requests.post(
                    str(base_url or self.runtime_base_url) + "/api/admin/shutdown",
                    headers=headers,
                    json={"reason": reason, "drain_timeout_sec": 5.0, "signal_delay_sec": 0.25},
                    timeout=3.0,
                )
            except Exception:
                pass
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if proc.poll() is not None:
                    return
                await asyncio.sleep(0.2)
        with contextlib.suppress(Exception):
            proc.terminate()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.1)
        with contextlib.suppress(Exception):
            proc.kill()

    async def _terminate_candidate_proc_locked(self, *, graceful: bool, reason: str) -> None:
        candidate_slot = str(self._candidate_slot or "").strip().upper() or None
        candidate_base_url = self.slot_runtime_base_url(candidate_slot) if candidate_slot else None
        await self._terminate_proc_locked(
            proc=self._candidate_proc,
            base_url=candidate_base_url,
            graceful=graceful,
            reason=reason,
        )
        self._candidate_proc = None
        self._candidate_slot = None
        self._candidate_runtime_instance_id = None
        self._candidate_transition_role = None
        self._candidate_runtime_cwd = None
        self._persist_runtime_state()

    async def restart_runtime(self, *, reason: str = "supervisor.restart") -> dict[str, Any]:
        async with self._lock:
            self._desired_running = True
            await self._terminate_proc_locked(graceful=True, reason=reason)
            await self._spawn_runtime_locked()
            self._restart_count += 1
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def stop(self, *, reason: str = "supervisor.stop") -> None:
        async with self._lock:
            self._desired_running = False
            self._stopping = True
            await self._terminate_proc_locked(graceful=True, reason=reason)
            await self._terminate_candidate_proc_locked(graceful=True, reason=f"{reason}.candidate")
            self._persist_runtime_state()

    async def stop_sidecar(self, *, reason: str = "supervisor.sidecar.stop") -> dict[str, Any]:
        del reason
        async with self._lock:
            await stop_realtime_sidecar_subprocess(self._sidecar_proc)
            self._sidecar_proc = None
            self._persist_runtime_state()
            return self._sidecar_status_payload()

    def sidecar_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "runtime": self._runtime_sidecar_runtime_payload(),
            "process": realtime_sidecar_listener_snapshot(self._sidecar_proc),
        }

    async def restart_sidecar(self, *, reconnect_hub_root: bool = False) -> dict[str, Any]:
        async with self._lock:
            new_proc, restart_result = await restart_realtime_sidecar_subprocess(
                proc=self._sidecar_proc,
                role=self._sidecar_role(),
            )
            self._sidecar_proc = new_proc
            self._persist_runtime_state()
        reconnect_result: dict[str, Any] | None = None
        if reconnect_hub_root and str(self._sidecar_role() or "").strip().lower() == "hub":
            try:
                reconnect_result = self._runtime_request_json(
                    path="/api/node/hub-root/reconnect",
                    method="POST",
                    payload={},
                    timeout=5.0,
                )
            except Exception as exc:
                reconnect_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "restart": restart_result,
            "reconnect": reconnect_result,
            "runtime": self._runtime_sidecar_runtime_payload(),
            "process": realtime_sidecar_listener_snapshot(self._sidecar_proc),
        }

    async def start_candidate_runtime(self, *, slot: str | None = None) -> dict[str, Any]:
        resolved_slot = str(slot or choose_inactive_slot() or "").strip().upper()
        if resolved_slot not in {"A", "B"}:
            raise HTTPException(status_code=409, detail="candidate slot is unavailable")
        current_slot = str(active_slot() or "").strip().upper()
        if resolved_slot == current_slot:
            raise HTTPException(status_code=409, detail="candidate slot must differ from the active slot")
        if self.slot_runtime_port(resolved_slot) == self.slot_runtime_port(current_slot):
            raise HTTPException(status_code=409, detail="candidate slot uses the same runtime port as the active slot")
        structure = validate_slot_structure(resolved_slot)
        if not bool(structure.get("ok")):
            raise HTTPException(status_code=409, detail=f"candidate slot {resolved_slot} is not launchable")
        async with self._lock:
            await self._spawn_candidate_runtime_locked(slot=resolved_slot)
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def stop_candidate_runtime(self, *, reason: str = "supervisor.candidate.stop") -> dict[str, Any]:
        async with self._lock:
            await self._terminate_candidate_proc_locked(graceful=True, reason=reason)
            self._persist_runtime_state()
            return self._runtime_state_payload()

    async def _candidate_prewarm(self, *, target_slot: str | None) -> dict[str, Any]:
        resolved_target = str(target_slot or "").strip().upper()
        if not resolved_target:
            return {
                "attempted": False,
                "state": "skipped",
                "message": "candidate prewarm skipped: target slot is unavailable",
            }

        runtime_snapshot = self.status()
        candidate_slot = str(runtime_snapshot.get("candidate_slot") or "").strip().upper()
        transition_mode = str(runtime_snapshot.get("transition_mode") or "").strip().lower()
        warm_switch_allowed = bool(runtime_snapshot.get("warm_switch_allowed"))
        warm_switch_reason = str(runtime_snapshot.get("warm_switch_reason") or "").strip()
        if candidate_slot != resolved_target or transition_mode != "warm_switch" or not warm_switch_allowed:
            return {
                "attempted": False,
                "state": "skipped",
                "message": warm_switch_reason or "candidate prewarm skipped: warm switch is not admitted",
                "runtime": runtime_snapshot,
            }

        await self.start_candidate_runtime(slot=resolved_target)
        timeout_sec = _warm_switch_candidate_ready_timeout_sec()
        deadline = time.time() + timeout_sec
        snapshot = self.status()
        while timeout_sec > 0.0 and time.time() < deadline:
            snapshot = self.status()
            if str(snapshot.get("candidate_slot") or "").strip().upper() != resolved_target:
                break
            if bool(snapshot.get("candidate_runtime_api_ready")):
                return {
                    "attempted": True,
                    "state": "ready",
                    "message": (
                        f"passive candidate runtime is ready on {snapshot.get('candidate_runtime_url')}"
                    ),
                    "ready_at": time.time(),
                    "runtime": snapshot,
                }
            await asyncio.sleep(0.25)

        snapshot = self.status()
        candidate_alive = bool(snapshot.get("candidate_managed_alive"))
        candidate_ready = bool(snapshot.get("candidate_runtime_api_ready"))
        candidate_url = str(snapshot.get("candidate_runtime_url") or "").strip()
        if candidate_ready:
            return {
                "attempted": True,
                "state": "ready",
                "message": f"passive candidate runtime is ready on {candidate_url}",
                "ready_at": time.time(),
                "runtime": snapshot,
            }
        if candidate_alive:
            return {
                "attempted": True,
                "state": "starting",
                "message": (
                    f"passive candidate runtime is still warming on {candidate_url or resolved_target}"
                ),
                "runtime": snapshot,
            }
        return {
            "attempted": True,
            "state": "failed",
            "message": "candidate prewarm failed before the runtime became ready",
            "runtime": snapshot,
        }

    async def _cleanup_candidate_runtime(self, *, reason: str, slot: str | None = None) -> dict[str, Any]:
        resolved_slot = str(slot or "").strip().upper() or None
        async with self._lock:
            current_slot = str(self._candidate_slot or "").strip().upper() or None
            if self._candidate_proc is None or (resolved_slot and current_slot != resolved_slot):
                self._persist_runtime_state()
                return {
                    "ok": True,
                    "stopped": False,
                    "slot": current_slot,
                }
            await self._terminate_candidate_proc_locked(graceful=True, reason=reason)
            self._persist_runtime_state()
            return {
                "ok": True,
                "stopped": True,
                "slot": current_slot,
            }

    async def _promote_candidate_runtime(self, *, slot: str, reason: str) -> dict[str, Any]:
        resolved_slot = str(slot or "").strip().upper()
        current_candidate_slot = str(self._candidate_slot or "").strip().upper()
        candidate_proc = self._candidate_proc
        if resolved_slot not in {"A", "B"}:
            raise RuntimeError("candidate slot is unavailable for fast cutover")
        if current_candidate_slot != resolved_slot:
            raise RuntimeError("candidate runtime slot does not match the prepared target slot")
        if candidate_proc is None or candidate_proc.poll() is not None:
            raise RuntimeError("candidate runtime is not running for fast cutover")

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        candidate_base_url = self.slot_runtime_base_url(resolved_slot)
        response = requests.post(
            candidate_base_url + "/api/admin/runtime/promote-active",
            headers=headers,
            json={"reason": reason, "reconnect_hub_root": True},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("candidate promotion returned a non-object payload")
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        promoted_role = str(runtime.get("transition_role") or "").strip().lower()
        if promoted_role != "active":
            raise RuntimeError("candidate runtime did not report active role after promotion")
        promoted_instance_id = str(runtime.get("runtime_instance_id") or self._candidate_runtime_instance_id or "").strip() or None
        async with self._lock:
            proc = self._candidate_proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError("candidate runtime exited before supervisor adopted it")
            self._proc = proc
            self._managed_runtime_instance_id = promoted_instance_id
            self._managed_transition_role = "active"
            self._managed_runtime_cwd = self._candidate_runtime_cwd
            self._candidate_proc = None
            self._candidate_slot = None
            self._candidate_runtime_instance_id = None
            self._candidate_transition_role = None
            self._candidate_runtime_cwd = None
            self._last_start_at = time.time()
            self._last_error = None
            self._restart_count += 1
            self._persist_runtime_state()
        return payload

    async def monitor_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            sidecar_proc = self._sidecar_proc
            if sidecar_proc is not None and sidecar_proc.poll() is not None:
                self._sidecar_proc = None
                self._persist_runtime_state()
            if realtime_sidecar_enabled(role=self._sidecar_role()) and not self._stopping:
                sidecar_snapshot = realtime_sidecar_listener_snapshot(self._sidecar_proc)
                if self._sidecar_proc is None and not bool(sidecar_snapshot.get("listener_running")):
                    try:
                        async with self._lock:
                            if self._sidecar_proc is None and not self._stopping:
                                await self._spawn_sidecar_locked()
                    except Exception:
                        _LOG.warning("failed to restart adaos-realtime sidecar", exc_info=True)
            await self._maybe_resume_or_continue_transition()
            candidate_proc = self._candidate_proc
            if candidate_proc is not None:
                candidate_rc = candidate_proc.poll()
                if candidate_rc is not None:
                    self._candidate_proc = None
                    self._candidate_slot = None
                    self._candidate_runtime_instance_id = None
                    self._candidate_transition_role = None
                    self._candidate_runtime_cwd = None
                    self._persist_runtime_state()
            proc = self._proc
            if proc is None:
                if self._desired_running and not self._stopping:
                    async with self._lock:
                        if self._proc is None and self._desired_running and not self._stopping:
                            await self._spawn_runtime_locked()
                continue
            rc = proc.poll()
            if rc is None:
                continue
            self._last_exit_code = int(rc)
            self._last_exit_at = time.time()
            self._proc = None
            self._managed_runtime_instance_id = None
            self._managed_transition_role = None
            self._managed_runtime_cwd = None
            self._persist_runtime_state()
            if self._stopping or not self._desired_running:
                continue
            async with self._lock:
                if self._proc is None and self._desired_running and not self._stopping:
                    await asyncio.sleep(1.0)
                    await self._spawn_runtime_locked()

    async def start(self) -> None:
        try:
            await self.ensure_sidecar_started()
        except Exception:
            _LOG.warning("failed to start adaos-realtime sidecar", exc_info=True)
        await self.ensure_started()
        self._monitor_task = asyncio.create_task(self.monitor_forever(), name="adaos-supervisor-monitor")

    async def close(self) -> None:
        self._stopping = True
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(BaseException):
                await self._update_task
            self._update_task = None
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(BaseException):
                await self._monitor_task
        async with self._lock:
            await self._terminate_candidate_proc_locked(graceful=True, reason="supervisor.shutdown.candidate")
        await self.stop(reason="supervisor.shutdown")
        try:
            await self.stop_sidecar(reason="supervisor.shutdown.sidecar")
        except Exception:
            _LOG.warning("failed to stop adaos-realtime sidecar", exc_info=True)

    def status(self) -> dict[str, Any]:
        payload = self._runtime_state_payload()
        payload["persisted_state"] = _read_json(_supervisor_runtime_state_path())
        payload["update_attempt"] = _read_update_attempt()
        payload["update_task_running"] = bool(self._update_task is not None and not self._update_task.done())
        return payload

    def supervisor_update_status(self) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        try:
            response = requests.get(
                self.runtime_base_url + "/api/admin/update/status",
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                payload.setdefault("runtime", self.status())
                payload["_served_by"] = "runtime"
                return _reconcile_update_status(payload)
        except Exception:
            pass
        return self._local_supervisor_update_status_payload()

    def public_update_status(self) -> dict[str, Any]:
        return _public_update_status_payload(self._local_supervisor_update_status_payload())

    async def _request_runtime_shutdown(self, *, reason: str, drain_timeout_sec: float, signal_delay_sec: float) -> dict[str, Any]:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._desired_running = True
                self._persist_runtime_state()
                return {"ok": True, "accepted": False, "reason": "runtime not running"}
            try:
                headers = {"Content-Type": "application/json"}
                if self.token:
                    headers["X-AdaOS-Token"] = self.token
                response = requests.post(
                    self.runtime_base_url + "/api/admin/shutdown",
                    headers=headers,
                    json={
                        "reason": reason,
                        "drain_timeout_sec": float(drain_timeout_sec),
                        "signal_delay_sec": float(signal_delay_sec),
                    },
                    timeout=_runtime_shutdown_request_timeout(
                        drain_timeout_sec=drain_timeout_sec,
                        signal_delay_sec=signal_delay_sec,
                    ),
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
            except Exception as exc:
                self._last_error = f"shutdown request failed: {type(exc).__name__}: {exc}"
                self._persist_runtime_state()
                raise HTTPException(status_code=503, detail=f"runtime shutdown API unavailable: {type(exc).__name__}: {exc}") from exc

    async def _ensure_runtime_stopped_for_update(
        self,
        *,
        drain_timeout_sec: float,
        signal_delay_sec: float,
        reason: str,
    ) -> dict[str, Any]:
        graceful_deadline = time.time() + max(3.0, float(drain_timeout_sec) + float(signal_delay_sec) + 3.0)
        while time.time() < graceful_deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return {"ok": True, "forced": False, "reason": reason}
            await asyncio.sleep(0.2)

        forced = False
        async with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                forced = True
                with contextlib.suppress(Exception):
                    proc.terminate()
                kill_deadline = time.time() + 5.0
                while time.time() < kill_deadline:
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    final_deadline = time.time() + 5.0
                    while time.time() < final_deadline:
                        if proc.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                if proc.poll() is None:
                    raise RuntimeError(f"runtime process did not exit after forced stop: {reason}")
                self._last_error = f"forced runtime stop after shutdown timeout: {reason}"
                self._persist_runtime_state()
        return {"ok": True, "forced": forced, "reason": reason}

    def _begin_countdown_transition(self, request: dict[str, Any], *, countdown_sec: float | None = None) -> dict[str, Any]:
        countdown_value = max(0.0, float(request.get("countdown_sec") if countdown_sec is None else countdown_sec))
        status = write_core_update_status(
            {
                "state": "countdown",
                "phase": "countdown",
                "action": str(request.get("action") or "update"),
                "target_rev": str(request.get("target_rev") or ""),
                "target_version": str(request.get("target_version") or ""),
                "reason": str(request.get("reason") or ""),
                "countdown_sec": countdown_value,
                "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
                "started_at": time.time(),
                "scheduled_for": time.time() + countdown_value,
            }
        )
        request_payload = dict(request)
        request_payload["countdown_sec"] = countdown_value
        _write_update_attempt(
            _build_attempt_payload(
                action=str(request_payload.get("action") or "update"),
                request=request_payload,
                status=status,
                accepted=True,
            )
        )
        self._update_task_cancel_mode = None
        self._update_task = asyncio.create_task(
            self._countdown_update_worker(
                action=str(request_payload.get("action") or "update"),
                target_rev=str(request_payload.get("target_rev") or ""),
                target_version=str(request_payload.get("target_version") or ""),
                reason=str(request_payload.get("reason") or ""),
                countdown_sec=countdown_value,
                drain_timeout_sec=float(request_payload.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(request_payload.get("signal_delay_sec") or 0.25),
            ),
            name=f"adaos-supervisor-core-update-{request_payload.get('action') or 'update'}",
        )
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    def _begin_prepare_transition(self, request: dict[str, Any]) -> dict[str, Any]:
        started_at = time.time()
        status = write_core_update_status(
            {
                "state": "preparing",
                "phase": "prepare",
                "action": str(request.get("action") or "update"),
                "target_rev": str(request.get("target_rev") or ""),
                "target_version": str(request.get("target_version") or ""),
                "reason": str(request.get("reason") or ""),
                "countdown_sec": float(request.get("countdown_sec") or 0.0),
                "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
                "started_at": started_at,
                "message": "preparing inactive slot before restart",
            }
        )
        attempt_payload = dict(request)
        attempt_payload.update(
            {
                "state": "active",
                "accepted": True,
                "requested_at": _epoch(request.get("requested_at")) or started_at,
                "prepare_started_at": started_at,
                "last_status": status,
                "updated_at": started_at,
            }
        )
        _write_update_attempt(attempt_payload)
        self._update_task_cancel_mode = None
        self._update_task = asyncio.create_task(
            self._prepare_and_countdown_update_worker(
                action=str(request.get("action") or "update"),
                target_rev=str(request.get("target_rev") or ""),
                target_version=str(request.get("target_version") or ""),
                reason=str(request.get("reason") or ""),
                countdown_sec=float(request.get("countdown_sec") or 0.0),
                drain_timeout_sec=float(request.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(request.get("signal_delay_sec") or 0.25),
            ),
            name=f"adaos-supervisor-core-update-prepare-{request.get('action') or 'update'}",
        )
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    def _schedule_planned_transition(
        self,
        request: dict[str, Any],
        *,
        scheduled_for: float,
        planned_reason: str,
        message: str,
    ) -> dict[str, Any]:
        due_at = max(time.time(), float(scheduled_for))
        status = write_core_update_status(
            {
                "state": "planned",
                "phase": "scheduled",
                "action": str(request.get("action") or "update"),
                "target_rev": str(request.get("target_rev") or ""),
                "target_version": str(request.get("target_version") or ""),
                "reason": str(request.get("reason") or ""),
                "countdown_sec": float(request.get("countdown_sec") or 0.0),
                "drain_timeout_sec": float(request.get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float(request.get("signal_delay_sec") or 0.25),
                "min_update_period_sec": _min_update_period_sec(),
                "planned_reason": planned_reason,
                "scheduled_for": due_at,
                "message": message,
            }
        )
        payload = dict(request)
        payload.update(
            {
                "state": "planned",
                "accepted": True,
                "scheduled_for": due_at,
                "planned_reason": planned_reason,
                "min_update_period_sec": _min_update_period_sec(),
                "last_status": status,
                "updated_at": time.time(),
            }
        )
        _write_update_attempt(payload)
        return {"ok": True, "accepted": True, "planned": True, "status": status, "_served_by": "supervisor"}

    def _queue_subsequent_transition(
        self,
        *,
        request: dict[str, Any],
        current_status: dict[str, Any] | None,
        current_attempt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = _epoch(request.get("requested_at")) or time.time()
        queued = dict(request)
        attempt = dict(current_attempt or {})
        if not attempt:
            attempt = {
                "state": "active",
                "action": str((current_status or {}).get("action") or request.get("action") or "update"),
                "requested_at": now,
                "updated_at": now,
                "last_status": dict(current_status or {}),
            }
        previous = _subsequent_transition_request(attempt)
        if previous:
            queued["first_requested_at"] = _epoch(previous.get("first_requested_at")) or _epoch(previous.get("requested_at")) or now
        attempt["subsequent_transition"] = True
        attempt["subsequent_transition_requested_at"] = now
        attempt["subsequent_transition_request"] = queued
        attempt["updated_at"] = now
        _write_update_attempt(attempt)

        status_payload = dict(current_status or read_core_update_status() or {})
        status_payload["subsequent_transition"] = True
        status_payload["subsequent_transition_requested_at"] = now
        status_payload["subsequent_transition_action"] = str(queued.get("action") or "update")
        status_payload["subsequent_transition_target_rev"] = str(queued.get("target_rev") or "")
        status_payload["subsequent_transition_target_version"] = str(queued.get("target_version") or "")
        status_payload["updated_at"] = time.time()
        status = write_core_update_status(status_payload)
        return {
            "ok": True,
            "accepted": True,
            "deferred": True,
            "subsequent_transition": True,
            "status": status,
            "_served_by": "supervisor",
        }

    async def _maybe_resume_or_continue_transition(self) -> None:
        payload = _reconcile_update_status(
            {
                "ok": True,
                "status": read_core_update_status(),
                "runtime": self.status(),
                "_served_by": "supervisor_monitor",
            }
        )
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else _read_update_attempt() or {}
        if self._candidate_proc is not None and not _is_transition_in_progress(status, attempt):
            await self._cleanup_candidate_runtime(reason="supervisor.candidate.idle_cleanup")
            payload = _reconcile_update_status(
                {
                    "ok": True,
                    "status": read_core_update_status(),
                    "runtime": self.status(),
                    "_served_by": "supervisor_monitor",
                }
            )
            status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
            attempt = payload.get("attempt") if isinstance(payload.get("attempt"), dict) else _read_update_attempt() or {}
        if self._update_task is not None and not self._update_task.done():
            return

        attempt_state = str(attempt.get("state") or "").strip().lower()
        now = time.time()
        if attempt_state == "planned":
            scheduled_for = _epoch(attempt.get("scheduled_for") or status.get("scheduled_for"))
            if scheduled_for > 0.0 and scheduled_for <= now:
                self._begin_countdown_transition(_request_from_attempt(attempt))
            return

        if attempt_state == "active" and str(status.get("state") or "").strip().lower() == "preparing":
            self._begin_prepare_transition(_request_from_attempt(attempt))
            return

        if attempt_state == "active" and str(status.get("state") or "").strip().lower() == "countdown":
            scheduled_for = _epoch(status.get("scheduled_for") or attempt.get("scheduled_for"))
            remaining = max(0.0, scheduled_for - now) if scheduled_for > 0.0 else float(attempt.get("countdown_sec") or 0.0)
            self._begin_countdown_transition(_request_from_attempt(attempt), countdown_sec=remaining)
            return

        queued = _subsequent_transition_request(attempt)
        if queued and _is_terminal_update_status(status):
            await self._cleanup_candidate_runtime(reason="supervisor.candidate.before_subsequent_transition")
            await self.start_update(
                action=str(queued.get("action") or "update"),
                target_rev=str(queued.get("target_rev") or ""),
                target_version=str(queued.get("target_version") or ""),
                reason=str(queued.get("reason") or "subsequent.transition"),
                countdown_sec=float(queued.get("countdown_sec") or 0.0),
                drain_timeout_sec=float(queued.get("drain_timeout_sec") or 10.0),
                signal_delay_sec=float(queued.get("signal_delay_sec") or 0.25),
                bypass_min_period=True,
            )

    async def _countdown_update_worker(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
    ) -> None:
        started_at = time.time()
        write_core_update_status(
            {
                "state": "countdown",
                "phase": "countdown",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "countdown_sec": countdown_sec,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
                "started_at": started_at,
                "scheduled_for": started_at + countdown_sec,
            }
        )
        try:
            await asyncio.sleep(max(0.0, float(countdown_sec)))
            plan = {
                "state": "pending_restart",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "created_at": time.time(),
                "expires_at": time.time() + 1800.0,
            }
            write_core_update_plan(plan)
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "message": "countdown completed; pending update written",
                }
            )
            shutdown_request_error: Exception | None = None
            try:
                await self._request_runtime_shutdown(
                    reason=reason,
                    drain_timeout_sec=drain_timeout_sec,
                    signal_delay_sec=signal_delay_sec,
                )
            except Exception as exc:
                shutdown_request_error = exc
            stop_result = await self._ensure_runtime_stopped_for_update(
                drain_timeout_sec=drain_timeout_sec,
                signal_delay_sec=signal_delay_sec,
                reason=reason,
            )
            if shutdown_request_error or bool(stop_result.get("forced")):
                write_core_update_status(
                    {
                        "state": "restarting",
                        "phase": "shutdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "message": (
                            "runtime shutdown API was unavailable; supervisor continued with direct process stop"
                            if shutdown_request_error and bool(stop_result.get("forced"))
                            else "runtime shutdown API response was unavailable; runtime still stopped during grace window"
                            if shutdown_request_error
                            else "runtime shutdown exceeded grace period; supervisor forced process stop"
                        ),
                        "forced_shutdown": bool(stop_result.get("forced")),
                        "shutdown_request_error_type": (
                            type(shutdown_request_error).__name__ if shutdown_request_error is not None else None
                        ),
                        "shutdown_request_error": str(shutdown_request_error) if shutdown_request_error is not None else None,
                    }
                )
        except asyncio.CancelledError:
            clear_core_update_plan()
            cancel_mode = str(self._update_task_cancel_mode or "").strip().lower()
            self._update_task_cancel_mode = None
            if cancel_mode != "rescheduled":
                status = write_core_update_status(
                    {
                        "state": "cancelled",
                        "phase": "countdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "message": "core update cancelled",
                    }
                )
                _complete_update_attempt(state="cancelled", status=status, reason=reason)
            raise
        except Exception as exc:
            clear_core_update_plan()
            status = write_core_update_status(
                {
                    "state": "failed",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "message": "failed to request runtime shutdown for pending core update",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "updated_at": time.time(),
                }
            )
            _complete_update_attempt(
                state="failed",
                status=status,
                reason=f"shutdown request failed: {type(exc).__name__}",
            )
        finally:
            self._update_task_cancel_mode = None
            if self._update_task is not None and self._update_task.done():
                self._update_task = None

    async def _prepare_and_countdown_update_worker(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
    ) -> None:
        cancel_phase = "prepare"
        failure_phase = "prepare"
        target_slot = ""
        manifest: dict[str, Any] | None = None
        candidate_prewarm_state = "skipped"
        candidate_prewarm_message = ""
        candidate_prewarm_ready_at = None
        candidate_launch_state = "skipped"
        candidate_launch_message = ""
        used_candidate_cutover = False
        prepare_result = prepare_pending_update(
            {
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
            }
        )
        try:
            if str(prepare_result.get("state") or "").strip().lower() != "prepared":
                status = write_core_update_status(
                    {
                        **dict(prepare_result),
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                    }
                )
                _complete_update_attempt(
                    state="failed",
                    status=status,
                    reason=str(prepare_result.get("message") or "prepare failed"),
                )
                return

            prepared_plan = prepare_result.get("plan") if isinstance(prepare_result.get("plan"), dict) else {}
            target_slot = str(
                prepare_result.get("target_slot")
                or prepared_plan.get("target_slot")
                or choose_inactive_slot()
                or ""
            ).strip().upper()
            manifest = prepare_result.get("manifest") if isinstance(prepare_result.get("manifest"), dict) else None
            try:
                candidate_prewarm = await self._candidate_prewarm(target_slot=target_slot)
            except Exception as exc:
                candidate_prewarm = {
                    "attempted": True,
                    "state": "failed",
                    "message": f"candidate prewarm failed: {type(exc).__name__}: {exc}",
                }
            candidate_prewarm_state = str(candidate_prewarm.get("state") or "").strip().lower() or "skipped"
            candidate_prewarm_message = str(candidate_prewarm.get("message") or "").strip()
            candidate_prewarm_ready_at = candidate_prewarm.get("ready_at")
            countdown_started_at = time.time()
            status = write_core_update_status(
                {
                    "state": "countdown",
                    "phase": "countdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "countdown_sec": countdown_sec,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "started_at": countdown_started_at,
                    "scheduled_for": countdown_started_at + countdown_sec,
                    "prepared_at": float(prepare_result.get("finished_at") or countdown_started_at),
                    "target_slot": target_slot,
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": (
                        (
                            f"slot {target_slot} prepared; passive candidate ready; countdown started"
                            if candidate_prewarm_state == "ready"
                            else f"slot {target_slot} prepared; passive candidate warming; countdown started"
                            if candidate_prewarm_state == "starting"
                            else f"slot {target_slot} prepared; passive candidate prewarm failed; countdown started"
                            if candidate_prewarm_state == "failed"
                            else f"slot {target_slot} prepared; countdown started"
                        )
                        if target_slot
                        else "inactive slot prepared; countdown started"
                    ),
                    "manifest": manifest,
                }
            )
            _write_update_attempt(
                _build_attempt_payload(
                    action=action,
                    request={
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "countdown_sec": countdown_sec,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message,
                    },
                    status=status,
                    accepted=True,
                )
            )
            cancel_phase = "countdown"
            await asyncio.sleep(max(0.0, float(countdown_sec)))

            plan = {
                "state": "prepared_restart",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "target_slot": target_slot,
                "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                "created_at": time.time(),
                "expires_at": time.time() + 1800.0,
            }
            write_core_update_plan(plan)
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "shutdown",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "target_slot": target_slot,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": "countdown completed; prepared restart written",
                    "manifest": manifest,
                }
            )
            failure_phase = "shutdown"
            shutdown_request_error: Exception | None = None
            try:
                await self._request_runtime_shutdown(
                    reason=reason,
                    drain_timeout_sec=drain_timeout_sec,
                    signal_delay_sec=signal_delay_sec,
                )
            except Exception as exc:
                shutdown_request_error = exc
            async with self._lock:
                self._desired_running = False
                self._persist_runtime_state()
            stop_result = await self._ensure_runtime_stopped_for_update(
                drain_timeout_sec=drain_timeout_sec,
                signal_delay_sec=signal_delay_sec,
                reason=reason,
            )
            if shutdown_request_error or bool(stop_result.get("forced")):
                write_core_update_status(
                    {
                        "state": "restarting",
                        "phase": "shutdown",
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "target_slot": target_slot,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message or None,
                        "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                        "message": (
                            "runtime shutdown API was unavailable; supervisor continued with direct process stop"
                            if shutdown_request_error and bool(stop_result.get("forced"))
                            else "runtime shutdown API response was unavailable; runtime still stopped during grace window"
                            if shutdown_request_error
                            else "runtime shutdown exceeded grace period; supervisor forced process stop"
                        ),
                        "forced_shutdown": bool(stop_result.get("forced")),
                        "shutdown_request_error_type": (
                            type(shutdown_request_error).__name__ if shutdown_request_error is not None else None
                        ),
                        "shutdown_request_error": str(shutdown_request_error) if shutdown_request_error is not None else None,
                        "manifest": manifest,
                    }
                )
            activate_slot(target_slot)
            candidate_cleanup: dict[str, Any] | None = None
            candidate_launch_state = candidate_prewarm_state
            candidate_launch_message = candidate_prewarm_message
            if candidate_prewarm_state == "ready":
                try:
                    await self._promote_candidate_runtime(
                        slot=target_slot,
                        reason="supervisor.fast_cutover",
                    )
                    used_candidate_cutover = True
                    candidate_launch_state = "promoted_to_active"
                    candidate_launch_message = (
                        "passive candidate runtime promoted to active via warm-switch cutover"
                    )
                except Exception as exc:
                    candidate_cleanup = await self._cleanup_candidate_runtime(
                        reason="supervisor.candidate.cutover_fallback",
                        slot=target_slot,
                    )
                    candidate_launch_state = "cutover_fallback"
                    candidate_launch_message = (
                        f"warm-switch cutover fallback: {type(exc).__name__}: {exc}"
                    )
            elif candidate_prewarm_state != "skipped":
                candidate_cleanup = await self._cleanup_candidate_runtime(
                    reason="supervisor.candidate.stop_before_active_launch",
                    slot=target_slot,
                )
                if bool((candidate_cleanup or {}).get("stopped")):
                    candidate_launch_state = "stopped_for_launch"
                    candidate_launch_message = "passive candidate runtime stopped before active launch"
            failure_phase = "launch"
            write_core_update_status(
                {
                    "state": "restarting",
                    "phase": "launch",
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "target_slot": target_slot,
                    "prepared_at": float(prepare_result.get("finished_at") or time.time()),
                    "candidate_prewarm_state": candidate_launch_state,
                    "candidate_prewarm_message": candidate_launch_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": (
                        f"prepared slot {target_slot} activated via warm-switch cutover; awaiting validation"
                        if used_candidate_cutover and target_slot
                        else "prepared slot activated via warm-switch cutover; awaiting validation"
                        if used_candidate_cutover
                        else f"prepared slot {target_slot} activated; awaiting runtime launch"
                        if target_slot
                        else "prepared slot activated; awaiting runtime launch"
                    ),
                    "manifest": manifest,
                }
            )
            async with self._lock:
                self._desired_running = True
                self._persist_runtime_state()
        except asyncio.CancelledError:
            clear_core_update_plan()
            await self._cleanup_candidate_runtime(
                reason="supervisor.candidate.cancelled_transition",
                slot=target_slot or None,
            )
            cancel_mode = str(self._update_task_cancel_mode or "").strip().lower()
            self._update_task_cancel_mode = None
            if cancel_mode != "rescheduled":
                status = write_core_update_status(
                    {
                        "state": "cancelled",
                        "phase": cancel_phase,
                        "action": action,
                        "target_rev": target_rev,
                        "target_version": target_version,
                        "reason": reason,
                        "drain_timeout_sec": drain_timeout_sec,
                        "signal_delay_sec": signal_delay_sec,
                        "candidate_prewarm_state": candidate_prewarm_state,
                        "candidate_prewarm_message": candidate_prewarm_message or None,
                        "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                        "message": "core update cancelled",
                    }
                )
                _complete_update_attempt(state="cancelled", status=status, reason=reason)
            raise
        except Exception as exc:
            clear_core_update_plan()
            await self._cleanup_candidate_runtime(
                reason="supervisor.candidate.failed_transition",
                slot=target_slot or None,
            )
            async with self._lock:
                self._desired_running = True
                self._persist_runtime_state()
            status = write_core_update_status(
                {
                    "state": "failed",
                    "phase": failure_phase,
                    "action": action,
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                    "candidate_prewarm_state": candidate_prewarm_state,
                    "candidate_prewarm_message": candidate_prewarm_message or None,
                    "candidate_prewarm_ready_at": candidate_prewarm_ready_at,
                    "message": "prepared core update transition failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "updated_at": time.time(),
                }
            )
            _complete_update_attempt(
                state="failed",
                status=status,
                reason=f"prepared transition failed: {type(exc).__name__}",
            )
        finally:
            self._update_task_cancel_mode = None
            if self._update_task is not None and self._update_task.done():
                self._update_task = None

    async def start_update(
        self,
        *,
        action: str,
        target_rev: str,
        target_version: str,
        reason: str,
        countdown_sec: float,
        drain_timeout_sec: float,
        signal_delay_sec: float,
        bypass_min_period: bool = False,
    ) -> dict[str, Any]:
        request = _transition_request_payload(
            action=action,
            target_rev=target_rev,
            target_version=target_version,
            reason=reason,
            countdown_sec=countdown_sec,
            drain_timeout_sec=drain_timeout_sec,
            signal_delay_sec=signal_delay_sec,
        )
        current_status = read_core_update_status()
        current_attempt = _read_update_attempt()
        if str((current_attempt or {}).get("state") or "").strip().lower() == "planned" and action == "update":
            scheduled_for = _epoch((current_attempt or {}).get("scheduled_for") or current_status.get("scheduled_for")) or time.time()
            return self._schedule_planned_transition(
                request=request,
                scheduled_for=scheduled_for,
                planned_reason=str((current_attempt or {}).get("planned_reason") or "minimum_update_period"),
                message="planned core update refreshed while waiting for scheduled window",
            )

        if _is_transition_in_progress(current_status, current_attempt):
            return self._queue_subsequent_transition(
                request=request,
                current_status=current_status,
                current_attempt=current_attempt,
            )

        if action == "update" and not bypass_min_period:
            min_period_sec = _min_update_period_sec()
            last_completed_at = _last_update_completion_at(current_status, current_attempt)
            next_allowed_at = last_completed_at + min_period_sec
            if min_period_sec > 0.0 and last_completed_at > 0.0 and next_allowed_at > time.time():
                return self._schedule_planned_transition(
                    request=request,
                    scheduled_for=next_allowed_at,
                    planned_reason="minimum_update_period",
                    message="core update deferred until minimum update interval elapses",
                )

        clear_core_update_plan()
        if action == "update":
            return self._begin_prepare_transition(request)
        return self._begin_countdown_transition(request)

    async def cancel_update(self, *, reason: str) -> dict[str, Any]:
        task = self._update_task
        clear_core_update_plan()
        current_attempt = _read_update_attempt() or {}
        current_status = read_core_update_status()
        if str(current_attempt.get("state") or "").strip().lower() == "planned":
            status = write_core_update_status(
                {
                    "state": "cancelled",
                    "phase": "scheduled",
                    "action": str(current_status.get("action") or current_attempt.get("action") or "update"),
                    "message": "planned core update cancelled by request",
                    "reason": reason,
                }
            )
            _complete_update_attempt(state="cancelled", status=status, reason=reason)
            return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

        if task is None or task.done():
            current_phase = str(current_status.get("phase") or "").strip().lower() or "countdown"
            status = write_core_update_status(
                {
                    "state": "cancelled",
                    "phase": current_phase,
                    "message": "no pending countdown task",
                    "reason": reason,
                }
            )
            _complete_update_attempt(state="cancelled", status=status, reason=reason)
            self._update_task = None
            return {"ok": True, "accepted": False, "status": status, "_served_by": "supervisor"}

        self._update_task_cancel_mode = "cancelled"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._update_task = None
        current_phase = str((read_core_update_status() or {}).get("phase") or "").strip().lower() or "countdown"
        status = write_core_update_status(
            {
                "state": "cancelled",
                "phase": current_phase,
                "action": str((read_core_update_status() or {}).get("action") or "update"),
                "message": "core update cancelled by request",
                "reason": reason,
                "drain_timeout_sec": float((read_core_update_status() or {}).get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float((read_core_update_status() or {}).get("signal_delay_sec") or 0.25),
            }
        )
        _complete_update_attempt(state="cancelled", status=status, reason=reason)
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    async def defer_update(self, *, delay_sec: float, reason: str) -> dict[str, Any]:
        delay_value = max(0.0, float(delay_sec))
        current_attempt = _read_update_attempt() or {}
        current_status = read_core_update_status()
        attempt_state = str(current_attempt.get("state") or "").strip().lower()
        status_state = str(current_status.get("state") or "").strip().lower()
        if attempt_state not in {"planned", "active"} and status_state not in {"planned", "countdown"}:
            raise HTTPException(status_code=409, detail="defer requires a planned update or active countdown")

        if self._update_task is not None and not self._update_task.done():
            self._update_task_cancel_mode = "rescheduled"
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None

        request = _request_from_attempt(current_attempt or current_status)
        scheduled_for = time.time() + delay_value
        return self._schedule_planned_transition(
            request=request,
            scheduled_for=scheduled_for,
            planned_reason="operator_defer",
            message="core update deferred by request",
        )

    async def promote_root(self, *, reason: str) -> dict[str, Any]:
        current_status = read_core_update_status()
        state = str(current_status.get("state") or "").strip().lower()
        phase = str(current_status.get("phase") or "").strip().lower()
        if state not in {"validated", "succeeded"} and phase != "root_promotion_pending":
            raise HTTPException(status_code=409, detail="root promotion requires a validated slot runtime")
        manifest = active_slot_manifest()
        root_promotion_required, bootstrap_update = manifest_requires_root_promotion(manifest)
        if not root_promotion_required:
            status = write_core_update_status(
                {
                    "state": "succeeded",
                    "phase": "validate",
                    "message": "no root promotion required for the active slot",
                    "target_slot": str((manifest or {}).get("slot") or active_slot() or ""),
                    "manifest": manifest,
                    "root_promotion_required": False,
                    "bootstrap_update": bootstrap_update,
                    "finished_at": time.time(),
                }
            )
            _complete_update_attempt(state="completed", status=status, reason=reason)
            return {"ok": True, "accepted": False, "status": status, "_served_by": "supervisor"}
        promotion = promote_root_from_slot(slot=str((manifest or {}).get("slot") or active_slot() or ""))
        status = write_core_update_status(
            {
                "state": "succeeded",
                "phase": "root_promoted",
                "message": "root bootstrap files promoted from validated slot; restart adaos.service to activate",
                "target_slot": str((manifest or {}).get("slot") or active_slot() or ""),
                "manifest": manifest,
                "root_promotion_required": False,
                "bootstrap_update": bootstrap_update,
                "root_promotion": promotion,
                "promotion_reason": reason,
                "finished_at": time.time(),
            }
        )
        previous_attempt = _read_update_attempt() or {}
        now = time.time()
        awaiting_attempt = dict(previous_attempt)
        awaiting_attempt.update(
            {
                "state": "awaiting_root_restart",
                "action": str(previous_attempt.get("action") or "update"),
                "accepted": True,
                "awaiting_restart": True,
                "restart_required": True,
                "requested_at": _epoch(previous_attempt.get("requested_at")) or now,
                "transitioned_at": now,
                "updated_at": now,
                "completion_reason": "",
                "last_status": status,
            }
        )
        _write_update_attempt(awaiting_attempt)
        return {"ok": True, "accepted": True, "status": status, "root_promotion": promotion, "_served_by": "supervisor"}

    def proxy_update_post(self, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-AdaOS-Token"] = self.token
        try:
            response = requests.post(
                self.runtime_base_url + path,
                headers=headers,
                json=body,
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            status = payload.get("status") if isinstance(payload, dict) and isinstance(payload.get("status"), dict) else {}
            accepted = bool(payload.get("accepted", True)) if isinstance(payload, dict) else True
            if path.endswith("/update/start"):
                _write_update_attempt(
                    _build_attempt_payload(action="update", request=body, status=status, accepted=accepted)
                )
            elif path.endswith("/update/rollback"):
                _write_update_attempt(
                    _build_attempt_payload(action="rollback", request=body, status=status, accepted=accepted)
                )
            elif path.endswith("/update/cancel"):
                _complete_update_attempt(state="cancelled", status=status, reason=str(body.get("reason") or "cancelled"))
            if isinstance(payload, dict):
                payload["_served_by"] = "runtime"
                return payload
            return {"ok": True, "response": payload, "_served_by": "runtime"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"runtime admin API unavailable: {type(exc).__name__}: {exc}") from exc


init_ctx()
app = FastAPI(title="AdaOS Supervisor")


@app.on_event("startup")
async def _startup() -> None:
    args = _parse_args()
    manager = SupervisorManager(runtime_host=args.host, runtime_port=args.port, token=_resolved_token(args.token))
    app.state.manager = manager
    await manager.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    manager = getattr(app.state, "manager", None)
    if manager is not None:
        await manager.close()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-AdaOS-Token", "Authorization"],
    allow_credentials=False,
)


def _manager() -> SupervisorManager:
    manager = getattr(app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="supervisor is not initialized")
    return manager


@app.get("/api/ping")
async def ping() -> dict[str, Any]:
    return {"ok": True, "ts": time.time(), "service": "adaos-supervisor"}


@app.get("/api/supervisor/status", dependencies=[Depends(require_token)])
async def supervisor_status() -> dict[str, Any]:
    return _manager().status()


@app.get("/api/supervisor/sidecar/status", dependencies=[Depends(require_token)])
async def supervisor_sidecar_status() -> dict[str, Any]:
    return _manager().sidecar_status()


@app.post("/api/supervisor/runtime/restart", dependencies=[Depends(require_token)])
async def supervisor_runtime_restart() -> dict[str, Any]:
    status = await _manager().restart_runtime()
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/runtime/candidate/start", dependencies=[Depends(require_token)])
async def supervisor_runtime_candidate_start(payload: dict[str, Any]) -> dict[str, Any]:
    status = await _manager().start_candidate_runtime(slot=str(payload.get("slot") or "").strip().upper() or None)
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/runtime/candidate/stop", dependencies=[Depends(require_token)])
async def supervisor_runtime_candidate_stop(payload: dict[str, Any]) -> dict[str, Any]:
    status = await _manager().stop_candidate_runtime(
        reason=str(payload.get("reason") or "supervisor.candidate.stop")
    )
    return {"ok": True, "runtime": status}


@app.post("/api/supervisor/sidecar/restart", dependencies=[Depends(require_token)])
async def supervisor_sidecar_restart(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().restart_sidecar(reconnect_hub_root=bool(payload.get("reconnect_hub_root")))


@app.get("/api/supervisor/update/status", dependencies=[Depends(require_token)])
async def supervisor_update_status() -> dict[str, Any]:
    return _manager().supervisor_update_status()


@app.get("/api/supervisor/public/update-status")
async def supervisor_public_update_status() -> dict[str, Any]:
    return _manager().public_update_status()


@app.post("/api/supervisor/update/start", dependencies=[Depends(require_token)])
async def supervisor_update_start(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().start_update(
        action="update",
        target_rev=str(payload.get("target_rev") or ""),
        target_version=str(payload.get("target_version") or ""),
        reason=str(payload.get("reason") or "core.update"),
        countdown_sec=float(payload.get("countdown_sec") or 60.0),
        drain_timeout_sec=float(payload.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(payload.get("signal_delay_sec") or 0.25),
    )


@app.post("/api/supervisor/update/cancel", dependencies=[Depends(require_token)])
async def supervisor_update_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().cancel_update(reason=str(payload.get("reason") or "user.cancelled"))


@app.post("/api/supervisor/update/defer", dependencies=[Depends(require_token)])
async def supervisor_update_defer(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().defer_update(
        delay_sec=float(payload.get("delay_sec") or payload.get("countdown_sec") or 300.0),
        reason=str(payload.get("reason") or "user.deferred"),
    )


@app.post("/api/supervisor/update/rollback", dependencies=[Depends(require_token)])
async def supervisor_update_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().start_update(
        action="rollback",
        target_rev="",
        target_version="",
        reason=str(payload.get("reason") or "core.rollback"),
        countdown_sec=float(payload.get("countdown_sec") or 0.0),
        drain_timeout_sec=float(payload.get("drain_timeout_sec") or 10.0),
        signal_delay_sec=float(payload.get("signal_delay_sec") or 0.25),
    )


@app.post("/api/supervisor/update/promote-root", dependencies=[Depends(require_token)])
async def supervisor_update_promote_root(payload: dict[str, Any]) -> dict[str, Any]:
    return await _manager().promote_root(reason=str(payload.get("reason") or "core.root_promotion"))


def main() -> None:
    args = _parse_args()
    if args.token:
        os.environ["ADAOS_TOKEN"] = str(args.token)
    os.environ["ADAOS_SUPERVISOR_ENABLED"] = "1"
    os.environ["ADAOS_SUPERVISOR_URL"] = _supervisor_base_url()
    uvicorn.run(
        app,
        host=_supervisor_host(),
        port=_supervisor_port(),
        loop=_uvicorn_loop_mode(),
        reload=False,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
