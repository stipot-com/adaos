from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from string import Formatter
from typing import Any

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from adaos.apps.api.auth import require_token
from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import _advertise_base, _uvicorn_loop_mode
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot, active_slot_manifest, slot_status as core_slot_status, validate_slot_structure
from adaos.services.core_update import clear_plan as clear_core_update_plan
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_plan as read_core_update_plan
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.core_update import write_plan as write_core_update_plan
from adaos.services.core_update import write_status as write_core_update_status
from adaos.services.runtime_paths import current_base_dir


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


def _terminal_update_states() -> set[str]:
    return {"failed", "validated", "succeeded", "rolled_back", "expired", "cancelled", "idle"}


def _read_update_attempt() -> dict[str, Any] | None:
    payload = _read_json(_supervisor_update_attempt_path())
    return payload if isinstance(payload, dict) else None


def _write_update_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged.setdefault("updated_at", time.time())
    _write_json(_supervisor_update_attempt_path(), merged)
    return merged


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

    failed_status = write_core_update_status(
        {
            "state": "failed",
            "phase": str(status.get("phase") or "restart_timeout"),
            "action": str(status.get("action") or attempt.get("action") or "update"),
            "target_rev": str(status.get("target_rev") or attempt.get("target_rev") or ""),
            "target_version": str(status.get("target_version") or attempt.get("target_version") or ""),
            "reason": str(status.get("reason") or attempt.get("reason") or "supervisor.timeout"),
            "message": f"supervisor timed out waiting for runtime to finish {status.get('state') or 'update transition'}",
            "supervisor_timeout_sec": timeout_sec,
            "supervisor_timeout_at": now,
            "supervisor_previous_status": status,
        }
    )
    with contextlib.suppress(Exception):
        clear_core_update_plan()
    payload["status"] = failed_status
    payload["attempt"] = _complete_update_attempt(state="failed", status=failed_status, reason="restart/apply timeout")
    payload["_served_by"] = "supervisor_timeout_recovery"
    return payload


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

    @property
    def runtime_base_url(self) -> str:
        return f"http://{self.runtime_host}:{self.runtime_port}"

    def _runtime_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["ADAOS_SUPERVISOR_ENABLED"] = "1"
        env["ADAOS_SUPERVISOR_URL"] = _supervisor_base_url()
        env["ADAOS_SUPERVISOR_HOST"] = _supervisor_host()
        env["ADAOS_SUPERVISOR_PORT"] = str(_supervisor_port())
        if self.token:
            env["ADAOS_TOKEN"] = self.token
        return env

    def _runtime_launch_spec(self) -> tuple[list[str] | None, str | None, dict[str, str], str | None]:
        env = self._runtime_env()
        manifest = active_slot_manifest()
        slot = active_slot()
        if isinstance(manifest, dict):
            manifest_env = manifest.get("env")
            if isinstance(manifest_env, dict):
                for key, value in manifest_env.items():
                    env[str(key)] = str(value)
            if slot:
                env["ADAOS_ACTIVE_CORE_SLOT"] = slot
                env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = str(core_slot_status().get("slots", {}).get(slot, {}).get("path") or "")
            values = {
                "host": self.runtime_host,
                "port": str(self.runtime_port),
                "token": str(self.token or ""),
                "slot": str(slot or ""),
                "slot_dir": str(core_slot_status().get("slots", {}).get(slot or "", {}).get("path") or ""),
                "base_dir": str(current_base_dir()),
                "python": os.sys.executable,
            }
            argv_raw = manifest.get("argv")
            if isinstance(argv_raw, list):
                argv = [_format_slot_value(str(item), values) for item in argv_raw if str(item).strip()]
                if argv:
                    cwd = str(manifest.get("cwd") or "").strip() or None
                    return argv, None, env, cwd
            command = str(manifest.get("command") or "").strip()
            if command:
                cwd = str(manifest.get("cwd") or "").strip() or None
                return None, _format_slot_value(command, values), env, cwd
        return (
            [
                sys.executable,
                "-m",
                "adaos.apps.autostart_runner",
                "--host",
                self.runtime_host,
                "--port",
                str(self.runtime_port),
            ],
            None,
            env,
            None,
        )

    def _runtime_state_payload(self) -> dict[str, Any]:
        proc = self._proc
        current_slot = active_slot()
        active_manifest = active_slot_manifest()
        slot_structure = validate_slot_structure(current_slot) if current_slot else None
        managed_pid = None
        managed_alive = False
        managed_cmdline: list[str] = []
        managed_executable = None
        managed_cwd = None
        if proc is not None:
            try:
                managed_pid = int(proc.pid or 0) or None
                managed_alive = proc.poll() is None
                raw_args = proc.args if isinstance(proc.args, (list, tuple)) else [str(proc.args or "")]
                managed_cmdline = [str(item) for item in raw_args if str(item or "").strip()]
                managed_executable = managed_cmdline[0] if managed_cmdline else None
                managed_cwd = str(proc.cwd) if getattr(proc, "cwd", None) else os.getcwd()
            except Exception:
                managed_pid = None
                managed_alive = False
                managed_cmdline = []
                managed_executable = None
                managed_cwd = None
        listener_running = bool(managed_alive) and _listener_running(self.runtime_host, self.runtime_port)
        api_ready = listener_running and _runtime_api_ready(self.runtime_base_url, token=self.token)
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
        return {
            "ok": True,
            "supervisor_pid": os.getpid(),
            "supervisor_url": _supervisor_base_url(),
            "runtime_url": self.runtime_base_url,
            "runtime_host": self.runtime_host,
            "runtime_port": self.runtime_port,
            "active_slot": current_slot,
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
            "active_manifest": active_manifest,
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

    async def _spawn_runtime_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        argv, command, env, cwd = self._runtime_launch_spec()
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
        self._last_start_at = time.time()
        self._last_error = None
        self._persist_runtime_state()

    async def ensure_started(self) -> None:
        async with self._lock:
            self._stopping = False
            self._desired_running = True
            await self._spawn_runtime_locked()

    async def _terminate_proc_locked(self, *, graceful: bool, reason: str) -> None:
        proc = self._proc
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
                    self.runtime_base_url + "/api/admin/shutdown",
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
            self._persist_runtime_state()

    async def monitor_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
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
            self._persist_runtime_state()
            if self._stopping or not self._desired_running:
                continue
            async with self._lock:
                if self._proc is None and self._desired_running and not self._stopping:
                    await asyncio.sleep(1.0)
                    await self._spawn_runtime_locked()

    async def start(self) -> None:
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
        await self.stop(reason="supervisor.shutdown")

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
        payload = _local_update_payload()
        payload["runtime"] = self.status()
        payload["_served_by"] = "supervisor_fallback"
        return _reconcile_update_status(payload)

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
                    timeout=5.0,
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
            except Exception as exc:
                self._last_error = f"shutdown request failed: {type(exc).__name__}: {exc}"
                self._persist_runtime_state()
                raise HTTPException(status_code=503, detail=f"runtime shutdown API unavailable: {type(exc).__name__}: {exc}") from exc

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
            await self._request_runtime_shutdown(
                reason=reason,
                drain_timeout_sec=drain_timeout_sec,
                signal_delay_sec=signal_delay_sec,
            )
        except asyncio.CancelledError:
            clear_core_update_plan()
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
    ) -> dict[str, Any]:
        existing = self._update_task
        if existing is not None and not existing.done():
            return {"ok": True, "accepted": False, "status": read_core_update_status()}

        current_status = read_core_update_status()
        if str(current_status.get("state") or "").strip().lower() in {"restarting", "applying"}:
            return {"ok": True, "accepted": False, "status": current_status}

        clear_core_update_plan()
        status = write_core_update_status(
            {
                "state": "countdown",
                "phase": "countdown",
                "action": action,
                "target_rev": target_rev,
                "target_version": target_version,
                "reason": reason,
                "countdown_sec": float(countdown_sec),
                "drain_timeout_sec": float(drain_timeout_sec),
                "signal_delay_sec": float(signal_delay_sec),
                "started_at": time.time(),
                "scheduled_for": time.time() + float(countdown_sec),
            }
        )
        _write_update_attempt(
            _build_attempt_payload(
                action=action,
                request={
                    "target_rev": target_rev,
                    "target_version": target_version,
                    "reason": reason,
                    "countdown_sec": countdown_sec,
                    "drain_timeout_sec": drain_timeout_sec,
                    "signal_delay_sec": signal_delay_sec,
                },
                status=status,
                accepted=True,
            )
        )
        self._update_task = asyncio.create_task(
            self._countdown_update_worker(
                action=action,
                target_rev=target_rev,
                target_version=target_version,
                reason=reason,
                countdown_sec=float(countdown_sec),
                drain_timeout_sec=float(drain_timeout_sec),
                signal_delay_sec=float(signal_delay_sec),
            ),
            name=f"adaos-supervisor-core-update-{action}",
        )
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

    async def cancel_update(self, *, reason: str) -> dict[str, Any]:
        task = self._update_task
        clear_core_update_plan()
        if task is None or task.done():
            status = write_core_update_status(
                {
                    "state": "cancelled",
                    "phase": "countdown",
                    "message": "no pending countdown task",
                    "reason": reason,
                }
            )
            _complete_update_attempt(state="cancelled", status=status, reason=reason)
            self._update_task = None
            return {"ok": True, "accepted": False, "status": status, "_served_by": "supervisor"}

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._update_task = None
        status = write_core_update_status(
            {
                "state": "cancelled",
                "phase": "countdown",
                "action": str((read_core_update_status() or {}).get("action") or "update"),
                "message": "core update cancelled by request",
                "reason": reason,
                "drain_timeout_sec": float((read_core_update_status() or {}).get("drain_timeout_sec") or 10.0),
                "signal_delay_sec": float((read_core_update_status() or {}).get("signal_delay_sec") or 0.25),
            }
        )
        _complete_update_attempt(state="cancelled", status=status, reason=reason)
        return {"ok": True, "accepted": True, "status": status, "_served_by": "supervisor"}

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


@app.post("/api/supervisor/runtime/restart", dependencies=[Depends(require_token)])
async def supervisor_runtime_restart() -> dict[str, Any]:
    status = await _manager().restart_runtime()
    return {"ok": True, "runtime": status}


@app.get("/api/supervisor/update/status", dependencies=[Depends(require_token)])
async def supervisor_update_status() -> dict[str, Any]:
    return _manager().supervisor_update_status()


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
