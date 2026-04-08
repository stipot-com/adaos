from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from adaos.apps.api.auth import require_token
from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import _advertise_base, _uvicorn_loop_mode
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status as core_slot_status
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_plan as read_core_update_plan
from adaos.services.core_update import read_status as read_core_update_status
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

    def _runtime_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "adaos.apps.autostart_runner",
            "--host",
            self.runtime_host,
            "--port",
            str(self.runtime_port),
        ]

    def _runtime_state_payload(self) -> dict[str, Any]:
        proc = self._proc
        managed_pid = None
        managed_alive = False
        if proc is not None:
            try:
                managed_pid = int(proc.pid or 0) or None
                managed_alive = proc.poll() is None
            except Exception:
                managed_pid = None
                managed_alive = False
        return {
            "ok": True,
            "supervisor_pid": os.getpid(),
            "supervisor_url": _supervisor_base_url(),
            "runtime_url": self.runtime_base_url,
            "runtime_host": self.runtime_host,
            "runtime_port": self.runtime_port,
            "desired_running": bool(self._desired_running),
            "stopping": bool(self._stopping),
            "managed_pid": managed_pid,
            "managed_alive": managed_alive,
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
        proc = subprocess.Popen(
            self._runtime_command(),
            cwd=os.getcwd(),
            env=self._runtime_env(),
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
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(BaseException):
                await self._monitor_task
        await self.stop(reason="supervisor.shutdown")

    def status(self) -> dict[str, Any]:
        payload = self._runtime_state_payload()
        payload["persisted_state"] = _read_json(_supervisor_runtime_state_path())
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
                return payload
        except Exception:
            pass
        payload = _local_update_payload()
        payload["runtime"] = self.status()
        payload["_served_by"] = "supervisor_fallback"
        return payload

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
    return _manager().proxy_update_post("/api/admin/update/start", body=payload)


@app.post("/api/supervisor/update/cancel", dependencies=[Depends(require_token)])
async def supervisor_update_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    return _manager().proxy_update_post("/api/admin/update/cancel", body=payload)


@app.post("/api/supervisor/update/rollback", dependencies=[Depends(require_token)])
async def supervisor_update_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    return _manager().proxy_update_post("/api/admin/update/rollback", body=payload)


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
