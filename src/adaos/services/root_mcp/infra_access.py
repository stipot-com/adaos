from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.reliability import channel_diagnostics_snapshot, runtime_signal_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.skill.service_supervisor import get_service_supervisor


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


def _tail_lines(path: Path, *, max_lines: int) -> list[str]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return []


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


__all__ = ["read_local_logs", "run_local_healthchecks"]
