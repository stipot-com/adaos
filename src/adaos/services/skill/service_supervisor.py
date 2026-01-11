from __future__ import annotations

import asyncio
from collections import deque
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
    skill_root: Path
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

    self_managed_enabled: bool
    crash_max_in_window: int
    crash_window_s: int
    crash_cooloff_s: int
    health_interval_s: int
    health_failures_before_issue: int
    hook_on_issue: str | None
    hook_on_self_heal: str | None
    hook_timeout_s: float

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

    self_managed = service.get("self_managed") or {}
    if not isinstance(self_managed, Mapping):
        self_managed = {}
    self_managed_enabled = bool(self_managed.get("enabled") is True)

    crash_cfg = self_managed.get("crash") or {}
    if not isinstance(crash_cfg, Mapping):
        crash_cfg = {}
    crash_max_in_window = int(crash_cfg.get("max_in_window") or 3)
    crash_window_s = int(crash_cfg.get("window_s") or 60)
    crash_cooloff_s = int(crash_cfg.get("cooloff_s") or 30)

    health_cfg = self_managed.get("health") or {}
    if not isinstance(health_cfg, Mapping):
        health_cfg = {}
    health_interval_s = int(health_cfg.get("interval_s") or 10)
    health_failures_before_issue = int(health_cfg.get("failures_before_issue") or 3)

    hooks_cfg = self_managed.get("hooks") or {}
    if not isinstance(hooks_cfg, Mapping):
        hooks_cfg = {}
    hook_on_issue = hooks_cfg.get("on_issue") if isinstance(hooks_cfg.get("on_issue"), str) else None
    hook_on_self_heal = hooks_cfg.get("on_self_heal") if isinstance(hooks_cfg.get("on_self_heal"), str) else None
    hook_timeout_s = float(hooks_cfg.get("timeout_s") or 10.0)

    return ServiceSpec(
        skill=skill_name,
        skill_root=skill_root,
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
        self_managed_enabled=self_managed_enabled,
        crash_max_in_window=max(1, crash_max_in_window),
        crash_window_s=max(1, crash_window_s),
        crash_cooloff_s=max(0, crash_cooloff_s),
        health_interval_s=max(1, health_interval_s),
        health_failures_before_issue=max(1, health_failures_before_issue),
        hook_on_issue=hook_on_issue.strip() if hook_on_issue and hook_on_issue.strip() else None,
        hook_on_self_heal=hook_on_self_heal.strip() if hook_on_self_heal and hook_on_self_heal.strip() else None,
        hook_timeout_s=max(0.1, hook_timeout_s),
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
        self._health_task: asyncio.Task | None = None

        self._issues_cache: dict[str, list[dict[str, Any]]] = {}
        self._crash_history: dict[str, deque[float]] = {}
        self._cooloff_until: dict[str, float] = {}
        self._health_failures: dict[str, int] = {}
        self._next_health_check_at: dict[str, float] = {}

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
            "self_managed": {
                "enabled": spec.self_managed_enabled,
                "crash": {
                    "max_in_window": spec.crash_max_in_window,
                    "window_s": spec.crash_window_s,
                    "cooloff_s": spec.crash_cooloff_s,
                },
                "health": {
                    "interval_s": spec.health_interval_s,
                    "failures_before_issue": spec.health_failures_before_issue,
                },
                "hooks": {
                    "on_issue": spec.hook_on_issue,
                    "on_self_heal": spec.hook_on_self_heal,
                    "timeout_s": spec.hook_timeout_s,
                },
            },
            "cooloff_until": self._cooloff_until.get(name),
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
        await self.ensure_started(name, spec, force=True)
        self._ensure_background_tasks()

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
                await self.ensure_started(name, spec, force=False)
            except Exception:
                _log.warning("failed to start service skill=%s", name, exc_info=True)

        self._ensure_background_tasks()

    def issues(self, name: str) -> list[dict[str, Any]]:
        self.ensure_discovered()
        if name not in self._specs:
            raise KeyError(name)
        return list(self._load_issues(name))

    async def inject_issue(self, name: str, *, issue_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.ensure_discovered()
        spec = self._specs.get(name)
        if not spec:
            raise KeyError(name)
        await self._record_issue(name, issue_type=issue_type, message=message, severity="manual", details=details or {})

    async def self_heal(self, name: str, *, reason: str, issue: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.ensure_discovered()
        spec = self._specs.get(name)
        if not spec:
            raise KeyError(name)
        if not spec.self_managed_enabled or not spec.hook_on_self_heal:
            return None
        return await self._run_hook(spec, spec.hook_on_self_heal, payload={"reason": reason, "issue": issue})

    async def ensure_started(self, name: str, spec: ServiceSpec, *, force: bool) -> None:
        proc = self._procs.get(name)
        if proc and proc.poll() is None:
            return

        now = time.time()
        cooloff_until = float(self._cooloff_until.get(name) or 0.0)
        if not force and now < cooloff_until:
            return

        python = self._select_python(spec)
        env = os.environ.copy()
        env["ADAOS_SERVICE_SKILL"] = name
        env["ADAOS_SERVICE_HOST"] = spec.host
        env["ADAOS_SERVICE_PORT"] = str(spec.port)
        env["PYTHONPATH"] = os.pathsep.join([str(spec.skill_root), env.get("PYTHONPATH", "")]).strip(os.pathsep)

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
        if self._health_task:
            try:
                self._health_task.cancel()
            except Exception:
                pass
            self._health_task = None

    # ------------------------------------------------------------------ internals
    def _ensure_background_tasks(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._watchdog_loop(), name="adaos-skill-service-watchdog")
        if self._health_task is None:
            self._health_task = asyncio.create_task(self._health_loop(), name="adaos-skill-service-health")

    def _service_state_dir(self, skill: str) -> Path:
        state_raw = self._ctx.paths.state_dir()
        state_dir = Path(state_raw() if callable(state_raw) else state_raw)
        return state_dir / "services" / skill

    def _issues_path(self, skill: str) -> Path:
        return self._service_state_dir(skill) / "issues.json"

    def _load_issues(self, skill: str) -> list[dict[str, Any]]:
        cached = self._issues_cache.get(skill)
        if cached is not None:
            return cached
        path = self._issues_path(skill)
        if not path.exists():
            self._issues_cache[skill] = []
            return self._issues_cache[skill]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._issues_cache[skill] = [x for x in data if isinstance(x, dict)]
            else:
                self._issues_cache[skill] = []
        except Exception:
            self._issues_cache[skill] = []
        return self._issues_cache[skill]

    def _persist_issues(self, skill: str) -> None:
        issues = self._issues_cache.get(skill)
        if issues is None:
            return
        path = self._issues_path(skill)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _record_issue(
        self,
        skill: str,
        *,
        issue_type: str,
        message: str,
        severity: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": f"iss.{int(time.time()*1000)}",
            "ts": time.time(),
            "type": issue_type,
            "severity": severity,
            "message": message,
            "details": details,
        }
        issues = self._load_issues(skill)
        issues.append(entry)
        if len(issues) > 200:
            del issues[: len(issues) - 200]
        self._persist_issues(skill)

        emit(
            self._ctx.bus,
            "skill.service.issue",
            {"skill": skill, "issue": entry},
            source="skill.service",
        )
        return entry

    async def _run_hook(self, spec: ServiceSpec, entrypoint: str, *, payload: dict[str, Any]) -> dict[str, Any] | None:
        python = self._select_python(spec)
        helper = r"""
import asyncio
import importlib
import json
import sys

def _resolve(ep: str):
    if ":" not in ep:
        raise SystemExit("entrypoint must be module:function")
    mod, fn = ep.split(":", 1)
    m = importlib.import_module(mod)
    f = getattr(m, fn)
    return f

async def _run_async(ep: str, payload: dict):
    f = _resolve(ep)
    res = f(payload)
    if asyncio.iscoroutine(res):
        res = await res
    return res

if len(sys.argv) < 3:
    raise SystemExit("Usage: hook.py <entrypoint> <payload_json>")

ep = sys.argv[1]
payload = json.loads(sys.argv[2])
result = asyncio.run(_run_async(ep, payload))
print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(spec.skill_root), env.get("PYTHONPATH", "")]).strip(os.pathsep)

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [str(python), "-c", helper, entrypoint, json.dumps(payload, ensure_ascii=False)],
                cwd=str(spec.skill_root),
                env=env,
                capture_output=True,
                timeout=spec.hook_timeout_s,
            )
        except subprocess.TimeoutExpired:
            await self._record_issue(
                spec.skill,
                issue_type="hook_timeout",
                message=f"hook timed out: {entrypoint}",
                severity="warning",
                details={"entrypoint": entrypoint, "timeout_s": spec.hook_timeout_s},
            )
            return None

        stdout = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            await self._record_issue(
                spec.skill,
                issue_type="hook_failed",
                message=f"hook failed: {entrypoint}",
                severity="warning",
                details={"entrypoint": entrypoint, "returncode": proc.returncode, "stderr": stderr[-2000:]},
            )
            return None

        # If the hook printed logs, try to parse the last JSON object line.
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return {"ok": True, "result": None}
        for ln in reversed(lines):
            if ln.startswith("{") and ln.endswith("}"):
                try:
                    data = json.loads(ln)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        return {"ok": True, "result": stdout}

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
            now = time.time()

            # Ensure all discovered services are up (unless in crash cooloff).
            self.ensure_discovered()
            for name, spec in list(self._specs.items()):
                proc = self._procs.get(name)
                if proc and proc.poll() is None:
                    continue
                cooloff_until = float(self._cooloff_until.get(name) or 0.0)
                if now < cooloff_until:
                    continue
                try:
                    await self.ensure_started(name, spec, force=False)
                except Exception:
                    _log.warning("failed to ensure service running skill=%s", name, exc_info=True)

            for name, proc in list(self._procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                emit(self._ctx.bus, "skill.service.crashed", {"skill": name, "code": code}, source="skill.service")
                self._procs.pop(name, None)
                spec = self._specs.get(name)
                if not spec:
                    continue

                # Crash loop detection (self-managed).
                history = self._crash_history.get(name)
                if history is None:
                    history = deque(maxlen=50)
                    self._crash_history[name] = history
                history.append(now)
                while history and (now - history[0]) > float(spec.crash_window_s):
                    history.popleft()

                if spec.self_managed_enabled and len(history) >= int(spec.crash_max_in_window):
                    self._cooloff_until[name] = now + float(spec.crash_cooloff_s)
                    issue = await self._record_issue(
                        name,
                        issue_type="crash_loop",
                        message=f"service crashed {len(history)} times in {spec.crash_window_s}s; cooloff {spec.crash_cooloff_s}s",
                        severity="error",
                        details={"exit_code": code, "crashes": len(history), "window_s": spec.crash_window_s, "cooloff_s": spec.crash_cooloff_s},
                    )
                    if spec.hook_on_issue:
                        await self._run_hook(spec, spec.hook_on_issue, payload={"issue": issue})
                    if spec.hook_on_self_heal:
                        await self._run_hook(spec, spec.hook_on_self_heal, payload={"issue": issue, "reason": "crash_loop"})
                    continue

                try:
                    await self.ensure_started(name, spec, force=False)
                except Exception:
                    _log.warning("failed to restart service skill=%s", name, exc_info=True)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            self.ensure_discovered()

            for name, spec in list(self._specs.items()):
                if not spec.self_managed_enabled:
                    continue

                next_at = float(self._next_health_check_at.get(name) or 0.0)
                if now < next_at:
                    continue
                self._next_health_check_at[name] = now + float(spec.health_interval_s)

                proc = self._procs.get(name)
                if not proc or proc.poll() is not None:
                    continue

                ok = False
                try:
                    status_code, _ = _http_get(spec.base_url + spec.health_path, timeout_ms=spec.health_timeout_ms)
                    ok = 200 <= status_code < 300
                except Exception:
                    ok = False

                if ok:
                    self._health_failures[name] = 0
                    continue

                failures = int(self._health_failures.get(name) or 0) + 1
                self._health_failures[name] = failures
                if failures < int(spec.health_failures_before_issue):
                    continue

                self._health_failures[name] = 0
                issue = await self._record_issue(
                    name,
                    issue_type="healthcheck_failed",
                    message=f"healthcheck failed {spec.health_failures_before_issue} times",
                    severity="warning",
                    details={"url": spec.base_url + spec.health_path, "timeout_ms": spec.health_timeout_ms},
                )
                if spec.hook_on_issue:
                    await self._run_hook(spec, spec.hook_on_issue, payload={"issue": issue})
                if spec.hook_on_self_heal:
                    await self._run_hook(spec, spec.hook_on_self_heal, payload={"issue": issue, "reason": "healthcheck_failed"})


_SUPERVISOR: ServiceSkillSupervisor | None = None


def get_service_supervisor() -> ServiceSkillSupervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        _SUPERVISOR = ServiceSkillSupervisor()
    return _SUPERVISOR
