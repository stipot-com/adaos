from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.reliability import channel_diagnostics_snapshot, runtime_signal_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.skill.service_supervisor import get_service_supervisor


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _iso_from_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return None


def _logs_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.logs_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_dir() -> Path:
    ctx = get_ctx()
    raw = ctx.paths.state_dir()
    path = Path(raw() if callable(raw) else raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _results_path() -> Path:
    path = _state_dir() / "root_mcp" / "infra_access_results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_results() -> dict[str, Any]:
    path = _results_path()
    if not path.exists():
        return {"tests": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"tests": {}}
    return payload if isinstance(payload, dict) else {"tests": {}}


def _write_results(payload: dict[str, Any]) -> None:
    _results_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _tail_lines(path: Path, *, max_lines: int) -> list[str]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return []


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _run_coroutine_in_thread(coro_factory) -> Any:
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            error["exc"] = exc

    thread = threading.Thread(target=runner, name="adaos-root-mcp-infra-access", daemon=True)
    thread.start()
    thread.join(timeout=30.0)
    if thread.is_alive():
        raise TimeoutError("infra access coroutine did not finish in time")
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


def read_local_logs(*, tail: int = 200, max_files: int = 5) -> dict[str, Any]:
    tail_lines = max(1, min(int(tail), 500))
    selected_files = max(1, min(int(max_files), 10))
    log_dir = _logs_dir()
    candidates = [
        item
        for item in log_dir.iterdir()
        if item.is_file() and item.suffix.lower() in {".log", ".txt"}
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)

    files: list[dict[str, Any]] = []
    for path in candidates[:selected_files]:
        try:
            stat = path.stat()
            size_bytes = int(stat.st_size)
            modified_at = _iso_from_ts(stat.st_mtime)
        except Exception:
            size_bytes = 0
            modified_at = None
        lines = _tail_lines(path, max_lines=tail_lines)
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": size_bytes,
                "modified_at": modified_at,
                "line_count": len(lines),
                "tail": lines,
            }
        )

    return {
        "mode": "local_process",
        "log_dir": str(log_dir),
        "tail": tail_lines,
        "max_files": selected_files,
        "files": files,
    }


def run_local_healthchecks() -> dict[str, Any]:
    lifecycle = runtime_lifecycle_snapshot()
    signals = runtime_signal_snapshot()
    diagnostics = channel_diagnostics_snapshot()

    supervisor = get_service_supervisor()
    service_names = supervisor.list()
    services: list[dict[str, Any]] = []
    failing_services: list[str] = []
    for name in service_names:
        status = supervisor.status(name, check_health=True) or {"name": name, "kind": "service"}
        services.append(status)
        if status.get("running") is False or status.get("health_ok") is False:
            failing_services.append(str(status.get("name") or name))

    lifecycle_ok = str(lifecycle.get("node_state") or "").strip().lower() in {"ready", "running", "active"}
    root_control = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
    route = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
    root_state = str(((root_control.get("stability") or {}) if isinstance(root_control.get("stability"), dict) else {}).get("state") or "").strip().lower()
    route_state = str(((route.get("stability") or {}) if isinstance(route.get("stability"), dict) else {}).get("state") or "").strip().lower()

    checks = [
        {
            "id": "lifecycle",
            "status": "ok" if lifecycle_ok else "warn",
            "summary": str(lifecycle.get("node_state") or "unknown"),
            "details": dict(lifecycle),
        },
        {
            "id": "root_control",
            "status": "ok" if root_state in {"ok", "stable", "healthy"} else "warn",
            "summary": str((signals.get("root_control") or {}).get("status") if isinstance(signals.get("root_control"), dict) else "unknown"),
            "details": dict(root_control),
        },
        {
            "id": "route",
            "status": "ok" if route_state in {"ok", "stable", "healthy"} else "warn",
            "summary": str((signals.get("route") or {}).get("status") if isinstance(signals.get("route"), dict) else "unknown"),
            "details": dict(route),
        },
        {
            "id": "services",
            "status": "ok" if not failing_services else "warn",
            "summary": f"{len(services)} discovered",
            "details": {"failing": failing_services},
        },
    ]

    overall = "ok" if all(item["status"] == "ok" for item in checks) else "warn"
    return {
        "mode": "local_process",
        "status": overall,
        "checks": checks,
        "lifecycle": dict(lifecycle),
        "signals": {
            "root_control": dict(signals.get("root_control") or {}) if isinstance(signals.get("root_control"), dict) else {},
            "route": dict(signals.get("route") or {}) if isinstance(signals.get("route"), dict) else {},
        },
        "services": services,
        "summary": {
            "service_count": len(services),
            "failing_services": failing_services,
        },
    }


def restart_local_service(*, service: str, allowed_services: list[str] | None = None) -> dict[str, Any]:
    service_name = str(service or "").strip()
    if not service_name:
        raise ValueError("service is required")
    allowed = _normalize_str_list(allowed_services or [])
    if allowed and service_name not in allowed:
        raise ValueError(f"service '{service_name}' is not in the infra access allowlist")
    supervisor = get_service_supervisor()
    supervisor.ensure_discovered(force=True)
    if service_name not in supervisor.list():
        raise KeyError(service_name)
    _run_coroutine_in_thread(lambda: supervisor.restart(service_name))
    return {
        "mode": "local_process",
        "service": service_name,
        "status": supervisor.status(service_name, check_health=True) or {"name": service_name},
    }


def run_allowed_tests(
    *,
    target_id: str,
    allowed_test_paths: list[str] | None = None,
    requested_tests: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    allowed = _normalize_str_list(allowed_test_paths or [])
    requested = _normalize_str_list(requested_tests or [])
    if not allowed:
        raise ValueError("target does not publish any allowed test paths")
    selected = requested or allowed
    for item in selected:
        if item not in allowed:
            raise ValueError(f"test path '{item}' is not in the infra access allowlist")

    cwd = _repo_root()
    command = [sys.executable, "-m", "pytest", "-q", *selected]
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=max(10, int(timeout_seconds)),
            check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        status = "passed" if proc.returncode == 0 else "failed"
        exit_code = int(proc.returncode)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        exit_code = None
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""

    finished = time.time()
    result = {
        "target_id": target_id,
        "mode": "local_process",
        "selected_tests": selected,
        "allowed_tests": allowed,
        "status": status,
        "exit_code": exit_code,
        "started_at": _iso_from_ts(started),
        "finished_at": _iso_from_ts(finished),
        "duration_s": round(max(0.0, finished - started), 3),
        "stdout_tail": stdout.splitlines()[-80:],
        "stderr_tail": stderr.splitlines()[-80:],
        "summary": {
            "stdout_lines": len(stdout.splitlines()),
            "stderr_lines": len(stderr.splitlines()),
        },
    }
    payload = _read_results()
    tests = payload.get("tests")
    if not isinstance(tests, dict):
        tests = {}
    tests[str(target_id)] = result
    payload["tests"] = tests
    _write_results(payload)
    return result


def read_test_results(*, target_id: str) -> dict[str, Any]:
    tests = _read_results().get("tests")
    if not isinstance(tests, dict):
        tests = {}
    item = tests.get(str(target_id))
    if not isinstance(item, dict):
        return {"available": False, "target_id": target_id}
    return {"available": True, "target_id": target_id, "result": item}


__all__ = [
    "read_local_logs",
    "read_test_results",
    "restart_local_service",
    "run_allowed_tests",
    "run_local_healthchecks",
]
