from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen

import yaml

from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit

_log = logging.getLogger("adaos.skill.service")


@dataclass(slots=True)
class ServiceSpec:
    skill: str
    host: str
    port: int
    command: list[str]
    workdir: Path
    env_mode: str
    python_selector: str | None
    venv_dir: Path | None
    dependencies: list[str]
    requirements_file: Path | None
    health_path: str
    health_timeout_ms: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _read_skill_manifest(skill_root: Path) -> dict:
    skill_yaml = skill_root / "skill.yaml"
    if not skill_yaml.exists():
        return {}
    try:
        return yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        _log.debug("failed to read skill.yaml at %s", skill_yaml, exc_info=True)
        return {}


def _resolve_service_spec(skill_name: str, skill_root: Path, manifest: Mapping[str, Any]) -> ServiceSpec | None:
    runtime = manifest.get("runtime") or {}
    if not isinstance(runtime, Mapping):
        runtime = {}
    kind = runtime.get("kind") or "module"
    if kind != "service":
        return None

    service = manifest.get("service") or {}
    if not isinstance(service, Mapping):
        service = {}

    host = str(service.get("host") or "127.0.0.1")
    port = int(service.get("port") or 0)
    if port <= 0:
        return None

    cmd_raw = service.get("command") or []
    if not isinstance(cmd_raw, list) or not all(isinstance(x, str) and x.strip() for x in cmd_raw):
        return None
    command = [str(x) for x in cmd_raw]

    workdir_raw = service.get("workdir")
    workdir = (skill_root / workdir_raw).resolve() if isinstance(workdir_raw, str) and workdir_raw else skill_root

    env_cfg = runtime.get("env") or {}
    if not isinstance(env_cfg, Mapping):
        env_cfg = {}
    env_mode = str(env_cfg.get("mode") or "global")
    python_selector = env_cfg.get("python") if isinstance(env_cfg.get("python"), str) else None
    venv_dir_raw = env_cfg.get("venv_dir") if isinstance(env_cfg.get("venv_dir"), str) else None
    venv_dir = Path(venv_dir_raw).resolve() if venv_dir_raw else None

    deps: list[str] = []
    dep_list = manifest.get("dependencies") or []
    if isinstance(dep_list, list):
        deps = [str(d) for d in dep_list if isinstance(d, str) and d.strip()]

    requirements_file = None
    req_in = skill_root / "requirements.in"
    if req_in.exists():
        requirements_file = req_in

    health = service.get("healthcheck") or {}
    if not isinstance(health, Mapping):
        health = {}
    health_path = str(health.get("path") or "/health")
    health_timeout_ms = int(health.get("timeout_ms") or 1000)

    return ServiceSpec(
        skill=skill_name,
        host=host,
        port=port,
        command=command,
        workdir=workdir,
        env_mode=env_mode,
        python_selector=python_selector,
        venv_dir=venv_dir,
        dependencies=deps,
        requirements_file=requirements_file,
        health_path=health_path,
        health_timeout_ms=health_timeout_ms,
    )


def _http_get(url: str, *, timeout_ms: int) -> tuple[int, str]:
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout_ms / 1000.0) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return int(resp.status), body


class ServiceSkillSupervisor:
    def __init__(self) -> None:
        self._ctx = get_ctx()
        self._procs: dict[str, subprocess.Popen] = {}
        self._specs: dict[str, ServiceSpec] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ public
    def ensure_discovered(self) -> None:
        skills_root_raw = self._ctx.paths.skills_dir()
        skills_root = Path(skills_root_raw() if callable(skills_root_raw) else skills_root_raw)
        if not skills_root.exists():
            return

        for skill_dir in skills_root.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_")):
                continue
            manifest = _read_skill_manifest(skill_dir)
            spec = _resolve_service_spec(skill_dir.name, skill_dir, manifest)
            if not spec:
                continue
            self._specs[skill_dir.name] = spec

    def resolve_base_url(self, skill_name: str) -> str | None:
        spec = self._specs.get(skill_name)
        return spec.base_url if spec else None

    def list(self) -> list[str]:
        self.ensure_discovered()
        return sorted(self._specs.keys())

    def status(self, name: str, *, check_health: bool = False) -> dict[str, Any] | None:
        self.ensure_discovered()
        spec = self._specs.get(name)
        if not spec:
            return None

        proc = self._procs.get(name)
        running = bool(proc and proc.poll() is None)
        pid = int(proc.pid) if proc and proc.pid else None
        code = None if running else (proc.poll() if proc else None)

        payload: dict[str, Any] = {
            "name": name,
            "kind": "service",
            "running": running,
            "pid": pid,
            "exit_code": code,
            "base_url": spec.base_url,
            "host": spec.host,
            "port": spec.port,
            "env_mode": spec.env_mode,
            "python_selector": spec.python_selector,
            "venv_dir": str(spec.venv_dir) if spec.venv_dir else None,
            "health_path": spec.health_path,
        }

        if check_health:
            ok = False
            try:
                status_code, _ = _http_get(spec.base_url + spec.health_path, timeout_ms=spec.health_timeout_ms)
                ok = 200 <= status_code < 300
            except Exception:
                ok = False
            payload["health_ok"] = ok

        return payload

    async def start(self, name: str) -> None:
        self.ensure_discovered()
        spec = self._specs.get(name)
        if not spec:
            raise KeyError(name)
        await self.ensure_started(name, spec)
        if self._task is None:
            self._task = asyncio.create_task(self._watchdog_loop(), name="adaos-skill-service-watchdog")

    async def stop(self, name: str, *, timeout_s: float = 3.0) -> None:
        proc = self._procs.get(name)
        if not proc:
            return

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

            deadline = time.time() + timeout_s
            while time.time() < deadline and proc.poll() is None:
                await asyncio.sleep(0.05)

            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        self._procs.pop(name, None)
        emit(self._ctx.bus, "skill.service.stopped", {"skill": name, "pid": proc.pid}, source="skill.service")

    async def restart(self, name: str) -> None:
        await self.stop(name)
        await self.start(name)

    async def start_all(self) -> None:
        self.ensure_discovered()
        for name, spec in list(self._specs.items()):
            try:
                await self.ensure_started(name, spec)
            except Exception:
                _log.warning("failed to start service skill=%s", name, exc_info=True)

        if self._task is None:
            self._task = asyncio.create_task(self._watchdog_loop(), name="adaos-skill-service-watchdog")

    async def ensure_started(self, name: str, spec: ServiceSpec) -> None:
        proc = self._procs.get(name)
        if proc and proc.poll() is None:
            return

        python = self._select_python(spec)
        env = os.environ.copy()
        env["ADAOS_SERVICE_SKILL"] = name
        env["ADAOS_SERVICE_HOST"] = spec.host
        env["ADAOS_SERVICE_PORT"] = str(spec.port)

        cmd = self._build_command(python, spec.command)
        logs_dir = self._ctx.paths.logs_dir()
        logs_dir = Path(logs_dir() if callable(logs_dir) else logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"service.{name}.log"

        _log.info("starting service skill=%s cmd=%s cwd=%s", name, cmd, spec.workdir)
        with open(log_path, "a", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(spec.workdir),
                env=env,
                stdout=logf,
                stderr=logf,
            )
        self._procs[name] = proc
        emit(self._ctx.bus, "skill.service.started", {"skill": name, "pid": proc.pid}, source="skill.service")

        await self._wait_ready(spec)
        emit(self._ctx.bus, "skill.service.ready", {"skill": name, "pid": proc.pid}, source="skill.service")

    async def shutdown(self) -> None:
        for name, proc in list(self._procs.items()):
            try:
                proc.terminate()
            except Exception:
                pass
            self._procs.pop(name, None)
        if self._task:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    # ------------------------------------------------------------------ internals
    def _service_state_dir(self, skill: str) -> Path:
        state_raw = self._ctx.paths.state_dir()
        state_dir = Path(state_raw() if callable(state_raw) else state_raw)
        return state_dir / "services" / skill

    def _select_python(self, spec: ServiceSpec) -> Path:
        if spec.env_mode != "venv":
            return Path(sys.executable)

        venv_dir = spec.venv_dir or (self._service_state_dir(spec.skill) / "venv")
        python = venv_dir / "Scripts" / "python.exe"
        if python.exists():
            return python

        selector = spec.python_selector or "3.10"
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["py", f"-{selector}", "-m", "venv", str(venv_dir)], check=True)

        python = venv_dir / "Scripts" / "python.exe"
        self._install_deps(python, spec)
        return python

    def _install_deps(self, python: Path, spec: ServiceSpec) -> None:
        base = [str(python), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
        subprocess.run([*base, "pip"], check=False)
        if spec.requirements_file:
            subprocess.run([*base, "-r", str(spec.requirements_file)], check=True)
        if spec.dependencies:
            subprocess.run([*base, *spec.dependencies], check=True)

    @staticmethod
    def _build_command(python: Path, argv: list[str]) -> list[str]:
        if not argv:
            return [str(python)]
        first = argv[0].lower()
        if first == "python":
            return [str(python), *argv[1:]]
        if first.startswith("-"):
            return [str(python), *argv]
        return [str(python), *argv]

    async def _wait_ready(self, spec: ServiceSpec) -> None:
        deadline = time.time() + 10.0
        url = spec.base_url + spec.health_path
        while time.time() < deadline:
            try:
                code, body = _http_get(url, timeout_ms=spec.health_timeout_ms)
                if 200 <= code < 300:
                    # Best-effort sanity: ensure it's JSON-ish.
                    try:
                        json.loads(body)
                    except Exception:
                        pass
                    return
            except Exception:
                await asyncio.sleep(0.25)
        _log.warning("service skill=%s did not become ready in time (%s)", spec.skill, url)

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            for name, proc in list(self._procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                emit(self._ctx.bus, "skill.service.crashed", {"skill": name, "code": code}, source="skill.service")
                self._procs.pop(name, None)
                spec = self._specs.get(name)
                if not spec:
                    continue
                try:
                    await self.ensure_started(name, spec)
                except Exception:
                    _log.warning("failed to restart service skill=%s", name, exc_info=True)


_SUPERVISOR: ServiceSkillSupervisor | None = None


def get_service_supervisor() -> ServiceSkillSupervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        _SUPERVISOR = ServiceSkillSupervisor()
    return _SUPERVISOR
