from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from adaos.services.nats_config import (
    normalize_nats_ws_url,
    nats_url_uses_websocket,
    order_nats_ws_candidates,
    public_nats_ws_api,
)
from adaos.services.node_runtime_state import load_nats_runtime_config, migrate_legacy_nats_runtime_config
from adaos.services.nats_ws_transport import (
    _set_tcp_keepalive,
    _ws_heartbeat_s_from_env,
    _ws_max_queue_from_env,
    _ws_proxy_from_env,
)
from adaos.services.runtime_dotenv import merged_runtime_dotenv_env
from adaos.services.runtime_paths import current_repo_root

NATS_PING = b"PING\r\n"
NATS_PONG = b"PONG\r\n"
_realtime_remote_quarantine_until: dict[str, float] = {}


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    try:
        text = str(value).strip().lower()
    except Exception:
        return default
    if not text:
        return default
    return text in {"1", "true", "on", "yes"}


def _realtime_remote_quarantine_s() -> float:
    raw = os.getenv("ADAOS_REALTIME_REMOTE_QUARANTINE_S")
    try:
        value = float(str(raw or "60").strip() or "60")
    except Exception:
        value = 60.0
    if value < 5.0:
        value = 5.0
    return value


def _realtime_remote_quarantine_key(url: str) -> str:
    try:
        parsed = urlparse(str(url))
        base = urlunparse(parsed._replace(query="", fragment=""))
    except Exception:
        base = str(url or "").strip()
    normalized = normalize_nats_ws_url(base, fallback=None)
    return str(normalized or base or "").strip()


def _should_quarantine_realtime_remote(details: str) -> bool:
    text = str(details or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "unexpected eof",
            "connectionclosederror",
            "connection closed",
            "no close frame received or sent",
            "close code=1006",
            "code=1006",
            "connection reset",
            "winerror 10054",
        )
    )


def _quarantine_realtime_remote(url: str, *, details: str | None = None) -> None:
    key = _realtime_remote_quarantine_key(url)
    if not key:
        return
    _realtime_remote_quarantine_until[key] = time.monotonic() + _realtime_remote_quarantine_s()


def _available_realtime_remote_candidates() -> list[str]:
    candidates = resolve_realtime_remote_candidates()
    if not candidates:
        return []
    now_m = time.monotonic()
    available: list[str] = []
    quarantined: list[tuple[float, int, str]] = []
    for index, candidate in enumerate(candidates):
        until = float(_realtime_remote_quarantine_until.get(_realtime_remote_quarantine_key(candidate), 0.0))
        if now_m >= until:
            available.append(candidate)
            continue
        quarantined.append((until, index, candidate))
    if available:
        return available
    quarantined.sort(key=lambda item: (item[0], item[1]))
    return [candidate for _until, _index, candidate in quarantined] or candidates


def _default_realtime_sidecar_role(role: str | None = None) -> str | None:
    role_norm = str(role or "").strip().lower() or None
    if role_norm:
        return role_norm
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        cfg = getattr(ctx, "config", None)
        role_norm = str(getattr(cfg, "role", "") or "").strip().lower() or None
        if role_norm:
            return role_norm
    except Exception:
        pass
    return None


def _realtime_sidecar_repo_root() -> Path | None:
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        repo_root = ctx.paths.repo_root()
        raw = repo_root() if callable(repo_root) else repo_root
        if raw:
            return Path(raw).expanduser().resolve()
    except Exception:
        pass
    return current_repo_root()


def realtime_sidecar_enablement_policy(*, role: str | None = None) -> dict[str, Any]:
    role_norm = _default_realtime_sidecar_role(role)
    default_enabled = role_norm == "hub"
    raw = os.getenv("ADAOS_REALTIME_ENABLE")
    env_var = "ADAOS_REALTIME_ENABLE"
    if raw is None:
        raw = os.getenv("HUB_REALTIME_ENABLE")
        env_var = "HUB_REALTIME_ENABLE"
    if raw is not None:
        enabled = _truthy(raw, default=False)
        value = str(raw).strip()
        return {
            "role": role_norm,
            "enabled": enabled,
            "default_enabled": default_enabled,
            "explicit": True,
            "source": "env_override",
            "env_var": env_var,
            "env_value": value,
            "reason": f"{env_var}={value or '0'}",
        }
    if role_norm == "hub":
        return {
            "role": role_norm,
            "enabled": True,
            "default_enabled": True,
            "explicit": False,
            "source": "role_default",
            "env_var": None,
            "env_value": None,
            "reason": "hub runtimes default to sidecar transport",
        }
    if role_norm:
        return {
            "role": role_norm,
            "enabled": False,
            "default_enabled": False,
            "explicit": False,
            "source": "role_default",
            "env_var": None,
            "env_value": None,
            "reason": "non-hub runtimes keep sidecar disabled by default",
        }
    return {
        "role": None,
        "enabled": False,
        "default_enabled": False,
        "explicit": False,
        "source": "role_unresolved",
        "env_var": None,
        "env_value": None,
        "reason": "runtime role is unresolved, so sidecar stays disabled by default",
    }


def realtime_sidecar_enabled(*, role: str | None = None, os_name: str | None = None) -> bool:
    policy = realtime_sidecar_enablement_policy(role=role)
    return bool(policy.get("enabled"))


def realtime_sidecar_host() -> str:
    return str(os.getenv("ADAOS_REALTIME_HOST", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"


def realtime_sidecar_port() -> int:
    raw = os.getenv("ADAOS_REALTIME_PORT")
    try:
        port = int(str(raw or "7422").strip() or "7422")
    except Exception:
        port = 7422
    if port <= 0:
        port = 7422
    return port


def realtime_sidecar_local_url() -> str:
    return f"nats://{realtime_sidecar_host()}:{realtime_sidecar_port()}"


def _realtime_sidecar_lifecycle_manager() -> str:
    return "supervisor" if _truthy(os.getenv("ADAOS_SUPERVISOR_ENABLED"), default=False) else "runtime"


def realtime_sidecar_route_tunnel_contract(*, role: str | None = None) -> dict[str, Any]:
    enabled = bool(realtime_sidecar_enabled(role=role))
    lifecycle_manager = _realtime_sidecar_lifecycle_manager()
    current_support = "planned" if enabled else "disabled"
    common_blockers = [
        "route tunnel browser websocket handoff is not implemented in adaos-realtime yet",
    ]
    if not enabled:
        common_blockers = [
            "realtime sidecar is disabled",
            *common_blockers,
        ]
    return {
        "current_support": current_support,
        "lifecycle_manager": lifecycle_manager,
        "ownership_boundary": "transport_only",
        "ws": {
            "current_owner": "runtime",
            "planned_owner": "sidecar",
            "migration_phase": "phase_2_route_tunnel_ownership",
            "logical_channels": [
                "hub_member.command",
                "hub_member.event",
                "hub_member.presence",
            ],
            "current_support": current_support,
            "delegation_mode": "not_implemented",
            "listener_ready": False,
            "handoff_ready": False,
            "blockers": [
                "browser route websocket still terminates in the runtime FastAPI app",
                *common_blockers,
            ],
        },
        "yws": {
            "current_owner": "runtime",
            "planned_owner": "sidecar",
            "migration_phase": "phase_2_route_tunnel_ownership",
            "logical_channels": [
                "hub_member.sync",
            ],
            "current_support": current_support,
            "delegation_mode": "not_implemented",
            "listener_ready": False,
            "handoff_ready": False,
            "blockers": [
                "Yjs websocket/session ownership still lives in the runtime gateway",
                *common_blockers,
            ],
        },
    }


def realtime_sidecar_log_path() -> Path:
    raw = str(os.getenv("ADAOS_REALTIME_LOG", ".adaos/diagnostics/realtime_sidecar.log") or "").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def realtime_sidecar_diag_path() -> Path:
    raw = str(os.getenv("ADAOS_REALTIME_DIAG_FILE", ".adaos/diagnostics/realtime_sidecar.jsonl") or "").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _host_matches_listener(host: str, other: str | None) -> bool:
    target = str(host or "").strip().lower()
    current = str(other or "").strip().lower()
    if not target:
        return not current
    if not current:
        return False
    if target == current:
        return True
    local_any = {"0.0.0.0", "::", "[::]"}
    loopbacks = {"127.0.0.1", "::1", "localhost"}
    if target in loopbacks and (current in loopbacks or current in local_any):
        return True
    return False


def _cmdline_option_value(cmdline: list[str], option: str) -> str | None:
    opt = str(option or "").strip().lower()
    if not opt:
        return None
    for idx, part in enumerate(cmdline):
        item = str(part or "").strip()
        lower = item.lower()
        if lower == opt:
            if idx + 1 < len(cmdline):
                value = str(cmdline[idx + 1] or "").strip()
                return value or None
            return None
        prefix = f"{opt}="
        if lower.startswith(prefix):
            value = item[len(prefix) :].strip()
            return value or None
    return None


def _process_looks_like_adaos_realtime(proc: Any) -> bool:
    try:
        cmdline = [str(part).lower() for part in proc.cmdline()]
    except Exception:
        return False
    joined = " ".join(cmdline)
    return "adaos" in joined and "realtime" in joined and "serve" in joined


def _process_matches_realtime_bind(proc: Any, host: str, port: int) -> bool:
    try:
        cmdline = [str(part) for part in proc.cmdline()]
    except Exception:
        return False
    if not _process_looks_like_adaos_realtime(proc):
        return False
    raw_port = _cmdline_option_value(cmdline, "--port")
    try:
        cmd_port = int(str(raw_port or "").strip() or "7422")
    except Exception:
        return False
    if cmd_port != int(port):
        return False
    cmd_host = _cmdline_option_value(cmdline, "--host") or "127.0.0.1"
    return _host_matches_listener(host, cmd_host)


def _find_realtime_listener_pid(host: str, port: int) -> int | None:
    try:
        import psutil
    except Exception:
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = getattr(conn, "laddr", None)
            if not laddr or int(getattr(laddr, "port", 0) or 0) != int(port):
                continue
            listener_host = getattr(laddr, "ip", None) or getattr(laddr, "host", None)
            if not _host_matches_listener(host, listener_host):
                continue
            pid = int(conn.pid or 0)
            if pid > 0:
                return pid
    except Exception:
        return None
    return None


def _terminate_process_tree(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        import psutil
    except Exception:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return False
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(children, timeout=3.0)
    for child in children:
        try:
            if child.is_running():
                child.kill()
        except psutil.Error:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except psutil.Error:
            pass
    except psutil.Error:
        pass
    return True


def _replace_existing_realtime_listener(host: str, port: int) -> bool:
    try:
        import psutil
    except Exception:
        return False
    pid = _find_realtime_listener_pid(host, port)
    if not pid or pid == os.getpid():
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return False
    if not _process_matches_realtime_bind(proc, host, port):
        return False
    if not _terminate_process_tree(pid):
        return False
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        owner_pid = _find_realtime_listener_pid(host, port)
        if not owner_pid or owner_pid == os.getpid():
            return True
        time.sleep(0.1)
    return False


def _realtime_ws_heartbeat_s() -> float | None:
    raw = os.getenv("ADAOS_REALTIME_WS_HEARTBEAT_S")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip() or "0")
    except Exception:
        value = 0.0
    if value <= 0.0:
        return None
    if value < 5.0:
        value = 5.0
    return value


def _realtime_ws_max_queue() -> int | None:
    raw = os.getenv("ADAOS_REALTIME_WS_MAX_QUEUE")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip() or "0")
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _realtime_ws_proxy() -> str | bool | None:
    raw = os.getenv("ADAOS_REALTIME_WS_PROXY")
    if raw is None:
        return _ws_proxy_from_env()
    try:
        value = str(raw).strip()
    except Exception:
        return _ws_proxy_from_env()
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"auto", "system", "default", "1", "true", "yes"}:
        return True
    if lowered in {"none", "off", "0", "false", "no"}:
        return None
    return value


def _realtime_nats_ping_interval_s() -> float | None:
    raw = os.getenv("ADAOS_REALTIME_NATS_PING_S")
    if raw is None:
        raw = os.getenv("ADAOS_REALTIME_UPSTREAM_NATS_PING_S")
    if raw is None:
        return 15.0
    try:
        value = float(str(raw).strip() or "0")
    except Exception:
        return None
    if value <= 0.0:
        return None
    if value < 5.0:
        value = 5.0
    return value


def _ws_socket(ws: Any) -> Any | None:
    try:
        transport = getattr(ws, "transport", None)
        if transport is None:
            protocol = getattr(ws, "protocol", None)
            transport = getattr(protocol, "transport", None)
        if transport is None:
            return None
        return transport.get_extra_info("socket")
    except Exception:
        return None


def _sidecar_loop_mode() -> str:
    raw = os.getenv("ADAOS_REALTIME_WIN_LOOP")
    if raw is None:
        return "proactor"
    value = str(raw).strip().lower()
    if value in {"selector", "proactor", "auto"}:
        return value
    return "proactor"


def apply_realtime_loop_policy() -> None:
    if os.name != "nt":
        return
    mode = _sidecar_loop_mode()
    if mode == "auto":
        return
    try:
        if mode == "selector":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        elif mode == "proactor":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


def resolve_realtime_remote_candidates() -> list[str]:
    explicit_url = str(os.getenv("ADAOS_REALTIME_REMOTE_WS_URL") or "").strip() or None
    nats_cfg = load_nats_runtime_config()
    if not nats_cfg:
        nats_cfg = migrate_legacy_nats_runtime_config()
    node_url_raw = str((nats_cfg or {}).get("ws_url") or "").strip() or None
    if explicit_url and nats_url_uses_websocket(explicit_url):
        base = normalize_nats_ws_url(explicit_url, fallback=None)
        candidates: list[str] = []
        for item in [base]:
            if isinstance(item, str) and item.startswith("ws") and item not in candidates:
                candidates.append(item)
        extra = str(os.getenv("ADAOS_REALTIME_REMOTE_WS_ALT", "") or "").strip()
        if extra:
            for item in [part.strip() for part in extra.split(",") if part.strip()]:
                normalized = normalize_nats_ws_url(item, fallback=None)
                if isinstance(normalized, str) and normalized.startswith("ws") and normalized not in candidates:
                    candidates.append(normalized)
        allow_api_fallback = _truthy(os.getenv("ADAOS_REALTIME_ALLOW_API_FALLBACK"), default=False)
        if allow_api_fallback:
            for item in [public_nats_ws_api(), "wss://nats.inimatic.com/nats"]:
                if item not in candidates:
                    candidates.append(item)
        return candidates
    target_url = explicit_url or node_url_raw
    if target_url and not nats_url_uses_websocket(target_url):
        prefer_dedicated = os.getenv("ADAOS_REALTIME_PREFER_DEDICATED", "0")
        allow_api_fallback = _truthy(os.getenv("ADAOS_REALTIME_ALLOW_API_FALLBACK"), default=True)
        allow_tcp_fallback = _truthy(os.getenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK"), default=False)
        ws_candidates = [public_nats_ws_api()]
        if allow_api_fallback:
            ws_candidates.append("wss://nats.inimatic.com/nats")
        ordered = order_nats_ws_candidates(ws_candidates, explicit_url=None, prefer_dedicated=prefer_dedicated)
        base_tcp = str(target_url).strip()
        if allow_tcp_fallback and base_tcp.startswith("nats://") and base_tcp not in ordered:
            ordered.append(base_tcp)
        return ordered
    node_url = normalize_nats_ws_url(node_url_raw, fallback=None)
    base = normalize_nats_ws_url(explicit_url or node_url, fallback=None)
    candidates: list[str] = []
    for item in [base, public_nats_ws_api(), "wss://nats.inimatic.com/nats"]:
        if isinstance(item, str) and item.startswith("ws") and item not in candidates:
            candidates.append(item)
    extra = str(os.getenv("ADAOS_REALTIME_REMOTE_WS_ALT", "") or "").strip()
    if extra:
        for item in [part.strip() for part in extra.split(",") if part.strip()]:
            normalized = normalize_nats_ws_url(item, fallback=None)
            if isinstance(normalized, str) and normalized.startswith("ws") and normalized not in candidates:
                candidates.append(normalized)
    # For long-lived sidecar sessions, the root ingress is the safer default and the dedicated hostname
    # remains a fallback. Some environments observe abnormal closes on the dedicated endpoint after tens
    # of seconds even with keepalives enabled.
    prefer_dedicated = os.getenv("ADAOS_REALTIME_PREFER_DEDICATED", "0")
    ordered = order_nats_ws_candidates(candidates, explicit_url=base, prefer_dedicated=prefer_dedicated)
    api_ingress = public_nats_ws_api()
    allow_api_fallback = _truthy(os.getenv("ADAOS_REALTIME_ALLOW_API_FALLBACK"), default=True)
    if api_ingress in ordered and not allow_api_fallback:
        ordered = [item for item in ordered if item != api_ingress]
    return ordered


async def _is_port_open(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        return False
    try:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    except Exception:
        pass
    return True


async def probe_realtime_sidecar_ready(*, host: str, port: int, timeout_s: float = 2.0) -> bool:
    async def _probe() -> bool:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            line = await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return bool(line.startswith(b"INFO "))

    try:
        return bool(await asyncio.wait_for(_probe(), timeout=max(0.1, float(timeout_s))))
    except Exception:
        return False


async def wait_realtime_sidecar_ready(*, host: str, port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if await probe_realtime_sidecar_ready(host=host, port=port, timeout_s=min(remaining, 2.5)):
            return True
        await asyncio.sleep(0.1)
    return False


async def start_realtime_sidecar_subprocess(
    *,
    role: str | None = None,
    repo_root: str | Path | None = None,
) -> subprocess.Popen[Any] | None:
    if not realtime_sidecar_enabled(role=role):
        return None
    if not resolve_realtime_remote_candidates():
        return None
    host = realtime_sidecar_host()
    port = realtime_sidecar_port()
    if await _is_port_open(host, port):
        try:
            await asyncio.to_thread(_replace_existing_realtime_listener, host, port)
        except Exception:
            pass
    if await _is_port_open(host, port):
        return None
    env = merged_runtime_dotenv_env(os.environ.copy())
    env["ADAOS_REALTIME_ENABLE"] = "1"
    env["ADAOS_REALTIME_CHILD"] = "1"
    env.setdefault("ADAOS_REALTIME_PREFER_DEDICATED", "0")
    env["ADAOS_REALTIME_ALLOW_API_FALLBACK"] = "1"
    env.setdefault("ADAOS_REALTIME_WIN_LOOP", "proactor")
    resolved_repo_root = (
        Path(repo_root).expanduser().resolve()
        if str(repo_root or "").strip()
        else _realtime_sidecar_repo_root()
    )
    launch_cwd = (
        resolved_repo_root
        if isinstance(resolved_repo_root, Path) and resolved_repo_root.exists()
        else Path(os.getcwd()).resolve()
    )
    if resolved_repo_root is not None:
        env["ADAOS_ROOT_REPO_ROOT"] = str(resolved_repo_root)
    log_path = realtime_sidecar_log_path()
    stdout_handle = log_path.open("ab")
    args = [
        sys.executable,
        "-m",
        "adaos",
        "realtime",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(
        args,
        cwd=str(launch_cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        ),
    )
    with contextlib.suppress(Exception):
        stdout_handle.close()
    if not await wait_realtime_sidecar_ready(host=host, port=port, timeout_s=10.0):
        with contextlib.suppress(Exception):
            proc.terminate()
        raise RuntimeError(f"adaos-realtime sidecar did not bind {host}:{port}")
    return proc


async def stop_realtime_sidecar_subprocess(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):
        proc.kill()


def realtime_sidecar_listener_snapshot(
    proc: subprocess.Popen[Any] | None = None,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    host = realtime_sidecar_host()
    port = realtime_sidecar_port()
    listener_pid = _find_realtime_listener_pid(host, port)
    managed_pid: int | None = None
    managed_alive = False
    managed_exit_code: int | None = None
    try:
        if proc is not None:
            pid = int(getattr(proc, "pid", 0) or 0)
            managed_pid = pid or None
            exit_code = proc.poll()
            if exit_code is None:
                managed_alive = True
            elif isinstance(exit_code, int):
                managed_exit_code = exit_code
    except Exception:
        managed_pid = managed_pid if isinstance(managed_pid, int) and managed_pid > 0 else None
        managed_alive = False
        managed_exit_code = None
    listener_running = bool(isinstance(listener_pid, int) and listener_pid > 0)
    listener_matches_managed = bool(
        listener_running
        and isinstance(managed_pid, int)
        and managed_pid > 0
        and int(listener_pid) == int(managed_pid)
    )
    adopted_listener = bool(listener_running and not listener_matches_managed)
    enablement_policy = realtime_sidecar_enablement_policy(role=role)
    return {
        "host": host,
        "port": int(port),
        "local_url": realtime_sidecar_local_url(),
        "log_path": str(realtime_sidecar_log_path()),
        "diag_path": str(realtime_sidecar_diag_path()),
        "managed_pid": managed_pid,
        "managed_alive": managed_alive,
        "managed_exit_code": managed_exit_code,
        "listener_pid": int(listener_pid) if listener_running else None,
        "listener_running": listener_running,
        "listener_matches_managed": listener_matches_managed,
        "adopted_listener": adopted_listener,
        "enablement_policy": enablement_policy,
        "route_tunnel_contract": realtime_sidecar_route_tunnel_contract(role=role),
    }


async def restart_realtime_sidecar_subprocess(
    *,
    proc: subprocess.Popen[Any] | None,
    role: str | None = None,
    repo_root: str | Path | None = None,
) -> tuple[subprocess.Popen[Any] | None, dict[str, Any]]:
    before = realtime_sidecar_listener_snapshot(proc, role=role)
    if not realtime_sidecar_enabled(role=role):
        return proc, {
            "ok": True,
            "accepted": False,
            "enabled": False,
            "reason": "disabled",
            "before": before,
            "after": before,
        }
    await stop_realtime_sidecar_subprocess(proc)
    new_proc = await start_realtime_sidecar_subprocess(role=role, repo_root=repo_root)
    after = realtime_sidecar_listener_snapshot(new_proc, role=role)
    return new_proc, {
        "ok": True,
        "accepted": True,
        "enabled": True,
        "reason": "restarted",
        "before": before,
        "after": after,
    }


@dataclass
class _RelayStats:
    session_id: str | None = None
    remote_url: str | None = None
    ws_ping_interval_s: float | None = None
    sidecar_nats_ping_interval_s: float | None = None
    local_connected_at: float | None = None
    remote_connected_at: float | None = None
    local_rx_bytes: int = 0
    local_tx_bytes: int = 0
    remote_rx_bytes: int = 0
    remote_tx_bytes: int = 0
    last_local_rx_at: float | None = None
    last_local_tx_at: float | None = None
    last_remote_rx_at: float | None = None
    last_remote_tx_at: float | None = None
    local_nats_pings_tx: int = 0
    local_nats_pongs_tx: int = 0
    remote_nats_pings_rx: int = 0
    remote_nats_pongs_rx: int = 0
    sidecar_nats_pings_tx: int = 0
    sidecar_nats_pongs_rx: int = 0
    sidecar_nats_pings_outstanding: int = 0
    client_nats_pings_outstanding: int = 0
    last_error: str | None = None
    active_session: bool = False
    local_client_total: int = 0
    session_open_total: int = 0
    session_close_total: int = 0
    remote_connect_total: int = 0
    remote_connect_fail_total: int = 0
    remote_quarantine_total: int = 0
    superseded_total: int = 0
    last_session_open_at: float | None = None
    last_session_close_at: float | None = None
    last_remote_connect_error: str | None = None
    last_remote_connect_error_at: float | None = None
    last_remote_disconnect_at: float | None = None


class RealtimeSidecarServer:
    def __init__(self, *, host: str, port: int) -> None:
        self._host = str(host or "127.0.0.1")
        self._port = int(port)
        self._server: asyncio.AbstractServer | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._diag_task: asyncio.Task[Any] | None = None
        self._stopped = asyncio.Event()
        self._stats = _RelayStats()
        self._pending_ping_sources: deque[str] = deque()

    def _begin_session_stats(self, *, session_id: str) -> None:
        previous = self._stats
        self._stats = _RelayStats(
            session_id=session_id,
            local_connected_at=time.monotonic(),
            active_session=True,
            local_client_total=int(previous.local_client_total or 0),
            session_open_total=int(previous.session_open_total or 0),
            session_close_total=int(previous.session_close_total or 0),
            remote_connect_total=int(previous.remote_connect_total or 0),
            remote_connect_fail_total=int(previous.remote_connect_fail_total or 0),
            remote_quarantine_total=int(previous.remote_quarantine_total or 0),
            superseded_total=int(previous.superseded_total or 0),
            last_session_open_at=time.monotonic(),
            last_session_close_at=previous.last_session_close_at,
            last_remote_connect_error=previous.last_remote_connect_error,
            last_remote_connect_error_at=previous.last_remote_connect_error_at,
            last_remote_disconnect_at=previous.last_remote_disconnect_at,
        )

    def _log(self, msg: str) -> None:
        try:
            print(f"[adaos-realtime] {msg}", flush=True)
        except Exception:
            pass

    @property
    def listen_host(self) -> str:
        return self._host

    @property
    def listen_port(self) -> int:
        try:
            if self._server is not None and getattr(self._server, "sockets", None):
                sock = self._server.sockets[0]
                return int(sock.getsockname()[1])
        except Exception:
            pass
        return int(self._port)

    def _diag_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()

        def _ago(value: float | None) -> float | None:
            if not isinstance(value, (int, float)):
                return None
            return round(now - float(value), 3)

        return {
            "ts": round(time.time(), 3),
            "listen": f"{self._host}:{self._port}",
            "session_id": self._stats.session_id,
            "active_session": self._stats.active_session,
            "ownership_boundary": "transport_only",
            "enablement_policy": realtime_sidecar_enablement_policy(),
            "route_tunnel_contract": realtime_sidecar_route_tunnel_contract(),
            "remote_url": self._stats.remote_url,
            "ws_ping_interval_s": self._stats.ws_ping_interval_s,
            "sidecar_nats_ping_interval_s": self._stats.sidecar_nats_ping_interval_s,
            "local_connected_ago_s": _ago(self._stats.local_connected_at),
            "remote_connected_ago_s": _ago(self._stats.remote_connected_at),
            "local_rx_bytes": self._stats.local_rx_bytes,
            "local_tx_bytes": self._stats.local_tx_bytes,
            "remote_rx_bytes": self._stats.remote_rx_bytes,
            "remote_tx_bytes": self._stats.remote_tx_bytes,
            "last_local_rx_ago_s": _ago(self._stats.last_local_rx_at),
            "last_local_tx_ago_s": _ago(self._stats.last_local_tx_at),
            "last_remote_rx_ago_s": _ago(self._stats.last_remote_rx_at),
            "last_remote_tx_ago_s": _ago(self._stats.last_remote_tx_at),
            "local_nats_pings_tx": self._stats.local_nats_pings_tx,
            "local_nats_pongs_tx": self._stats.local_nats_pongs_tx,
            "remote_nats_pings_rx": self._stats.remote_nats_pings_rx,
            "remote_nats_pongs_rx": self._stats.remote_nats_pongs_rx,
            "sidecar_nats_pings_tx": self._stats.sidecar_nats_pings_tx,
            "sidecar_nats_pongs_rx": self._stats.sidecar_nats_pongs_rx,
            "sidecar_nats_pings_outstanding": self._stats.sidecar_nats_pings_outstanding,
            "client_nats_pings_outstanding": self._stats.client_nats_pings_outstanding,
            "last_error": self._stats.last_error,
            "local_client_total": self._stats.local_client_total,
            "session_open_total": self._stats.session_open_total,
            "session_close_total": self._stats.session_close_total,
            "remote_connect_total": self._stats.remote_connect_total,
            "remote_connect_fail_total": self._stats.remote_connect_fail_total,
            "remote_quarantine_total": self._stats.remote_quarantine_total,
            "superseded_total": self._stats.superseded_total,
            "last_session_open_ago_s": _ago(self._stats.last_session_open_at),
            "last_session_close_ago_s": _ago(self._stats.last_session_close_at),
            "last_remote_disconnect_ago_s": _ago(self._stats.last_remote_disconnect_at),
            "last_remote_connect_error": self._stats.last_remote_connect_error,
            "last_remote_connect_error_ago_s": _ago(self._stats.last_remote_connect_error_at),
            "loop_policy": type(asyncio.get_event_loop_policy()).__name__,
            "loop": type(asyncio.get_running_loop()).__name__,
        }

    async def _diag_loop(self) -> None:
        try:
            every_s = float(os.getenv("ADAOS_REALTIME_DIAG_EVERY_S", "2") or "2")
        except Exception:
            every_s = 2.0
        if every_s <= 0:
            every_s = 2.0
        path = realtime_sidecar_diag_path()
        while not self._stopped.is_set():
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(self._diag_snapshot(), ensure_ascii=False) + "\n")
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=every_s)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        self._diag_task = asyncio.create_task(self._diag_loop(), name="adaos-realtime-diag")
        self._log(
            f"serve start listen=nats://{self.listen_host}:{self.listen_port} remote_candidates={resolve_realtime_remote_candidates()} "
            f"loop={type(asyncio.get_running_loop()).__name__} log={realtime_sidecar_log_path()} diag={realtime_sidecar_diag_path()}"
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        self._stopped.set()
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            with contextlib.suppress(BaseException):
                await self._active_task
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(BaseException):
                await self._server.wait_closed()
        if self._diag_task is not None and not self._diag_task.done():
            self._diag_task.cancel()
            with contextlib.suppress(BaseException):
                await self._diag_task

    def _tagged_remote_url(self, url: str, *, session_id: str) -> str:
        if not _truthy(os.getenv("ADAOS_REALTIME_CONNECT_TAG_QUERY", "1"), default=True):
            return url
        try:
            parsed = urlparse(str(url))
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params.setdefault("adaos_conn", session_id)
            return urlunparse(parsed._replace(query=urlencode(params)))
        except Exception:
            return url

    async def _connect_remote(self, *, session_id: str) -> tuple[Any, str]:
        import websockets  # type: ignore

        last_exc: Exception | None = None
        heartbeat_s = _realtime_ws_heartbeat_s()
        max_queue = _realtime_ws_max_queue()
        proxy = _realtime_ws_proxy()
        for candidate in _available_realtime_remote_candidates():
            target = self._tagged_remote_url(candidate, session_id=session_id)
            try:
                kwargs = {
                    "subprotocols": ["nats"],
                    "open_timeout": 5.0,
                    "close_timeout": 2.0,
                    "max_size": None,
                    "max_queue": max_queue,
                    "compression": None,
                    "ping_interval": heartbeat_s,
                    "ping_timeout": None,
                    "proxy": proxy,
                }
                try:
                    ws = await websockets.connect(target, **kwargs)
                except TypeError:
                    kwargs.pop("proxy", None)
                    ws = await websockets.connect(target, **kwargs)
                sock = _ws_socket(ws)
                keepalive_ok = _set_tcp_keepalive(sock)
                self._stats.ws_ping_interval_s = heartbeat_s
                self._stats.remote_connect_total = int(self._stats.remote_connect_total or 0) + 1
                self._stats.last_remote_connect_error = None
                self._stats.last_remote_connect_error_at = None
                self._log(
                    f"remote connect ok url={target} ping_interval={heartbeat_s} max_queue={max_queue} "
                    f"proxy={proxy} tcp_keepalive={keepalive_ok}"
                )
                return ws, target
            except Exception as exc:
                last_exc = exc
                self._stats.remote_connect_fail_total = int(self._stats.remote_connect_fail_total or 0) + 1
                self._stats.last_remote_connect_error = f"{type(exc).__name__}: {exc}"
                self._stats.last_remote_connect_error_at = time.monotonic()
                self._log(f"remote connect failed url={target} err={type(exc).__name__}: {exc}")
        raise RuntimeError(f"realtime remote connect failed: {type(last_exc).__name__}: {last_exc}") from last_exc

    async def _relay_local_to_remote(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws: Any) -> None:
        send_q: asyncio.Queue[bytes] = asyncio.Queue()
        send_event = asyncio.Event()
        recv_q: asyncio.Queue[bytes] = asyncio.Queue()

        async def _queue_remote_payload(payload: bytes) -> None:
            await send_q.put(payload)
            send_event.set()

        async def _local_reader_loop() -> None:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                self._stats.local_rx_bytes += len(chunk)
                self._stats.last_local_rx_at = time.monotonic()
                if chunk == NATS_PING:
                    self._stats.local_nats_pings_tx += 1
                    self._stats.client_nats_pings_outstanding += 1
                    self._pending_ping_sources.append("client")
                elif chunk == NATS_PONG:
                    self._stats.local_nats_pongs_tx += 1
                await _queue_remote_payload(chunk)

        async def _remote_writer_loop() -> None:
            recv_task: asyncio.Task[Any] | None = asyncio.create_task(ws.recv(), name="adaos-realtime-ws-recv")
            wake_task: asyncio.Task[Any] | None = None
            try:
                while True:
                    if recv_task is not None and recv_task.done():
                        try:
                            raw = await recv_task
                        finally:
                            recv_task = None
                        if isinstance(raw, str):
                            payload = raw.encode("utf-8", errors="replace")
                        else:
                            payload = bytes(raw)
                        if not payload:
                            recv_task = asyncio.create_task(ws.recv(), name="adaos-realtime-ws-recv")
                            continue
                        self._stats.remote_rx_bytes += len(payload)
                        self._stats.last_remote_rx_at = time.monotonic()
                        if payload == NATS_PING:
                            self._stats.remote_nats_pings_rx += 1
                        elif payload == NATS_PONG:
                            self._stats.remote_nats_pongs_rx += 1
                            source = self._pending_ping_sources.popleft() if self._pending_ping_sources else None
                            if source == "sidecar":
                                if self._stats.sidecar_nats_pings_outstanding > 0:
                                    self._stats.sidecar_nats_pings_outstanding -= 1
                                self._stats.sidecar_nats_pongs_rx += 1
                                recv_task = asyncio.create_task(ws.recv(), name="adaos-realtime-ws-recv")
                                continue
                            if source == "client":
                                if self._stats.client_nats_pings_outstanding > 0:
                                    self._stats.client_nats_pings_outstanding -= 1
                            elif self._stats.client_nats_pings_outstanding > 0:
                                self._stats.client_nats_pings_outstanding -= 1
                            elif self._stats.sidecar_nats_pings_outstanding > 0:
                                self._stats.sidecar_nats_pings_outstanding -= 1
                                self._stats.sidecar_nats_pongs_rx += 1
                                recv_task = asyncio.create_task(ws.recv(), name="adaos-realtime-ws-recv")
                                continue
                        await recv_q.put(payload)
                        recv_task = asyncio.create_task(ws.recv(), name="adaos-realtime-ws-recv")
                        continue

                    try:
                        payload = send_q.get_nowait()
                    except asyncio.QueueEmpty:
                        payload = None
                    if payload is not None:
                        await ws.send(payload)
                        self._stats.remote_tx_bytes += len(payload)
                        self._stats.last_remote_tx_at = time.monotonic()
                        if send_q.empty():
                            send_event.clear()
                        continue

                    send_event.clear()
                    wake_task = asyncio.create_task(send_event.wait(), name="adaos-realtime-ws-send")
                    done, pending = await asyncio.wait({recv_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
                    if wake_task in done:
                        wake_task = None
                        continue
                    if wake_task in pending and not wake_task.done():
                        wake_task.cancel()
                    wake_task = None
                    if recv_task not in done:
                        continue
            finally:
                if recv_task is not None and not recv_task.done():
                    recv_task.cancel()
                if wake_task is not None and not wake_task.done():
                    wake_task.cancel()

        async def _remote_reader_loop() -> None:
            while True:
                payload = await recv_q.get()
                writer.write(payload)
                await writer.drain()
                self._stats.local_tx_bytes += len(payload)
                self._stats.last_local_tx_at = time.monotonic()

        async def _sidecar_keepalive_loop(*, interval_s: float) -> None:
            while True:
                await asyncio.sleep(interval_s)
                if getattr(ws, "closed", False):
                    return
                if self._stats.sidecar_nats_pings_outstanding > 0:
                    continue
                self._pending_ping_sources.append("sidecar")
                self._stats.sidecar_nats_pings_tx += 1
                self._stats.sidecar_nats_pings_outstanding += 1
                await _queue_remote_payload(NATS_PING)

        interval_s = _realtime_nats_ping_interval_s()
        self._stats.sidecar_nats_ping_interval_s = interval_s
        tasks = [
            asyncio.create_task(_local_reader_loop(), name="adaos-realtime-l2r"),
            asyncio.create_task(_remote_writer_loop(), name="adaos-realtime-ws-io"),
            asyncio.create_task(_remote_reader_loop(), name="adaos-realtime-r2l"),
        ]
        if interval_s is not None:
            tasks.append(asyncio.create_task(_sidecar_keepalive_loop(interval_s=interval_s), name="adaos-realtime-ka"))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _bridge_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ws = None
        remote_url: str | None = None
        session_id = f"rt-{uuid.uuid4().hex[:10]}"
        self._begin_session_stats(session_id=session_id)
        self._stats.session_open_total = int(self._stats.session_open_total or 0) + 1
        self._pending_ping_sources = deque()
        try:
            ws, remote_url = await self._connect_remote(session_id=session_id)
            self._stats.remote_url = remote_url
            self._stats.remote_connected_at = time.monotonic()
            self._log(f"session open id={session_id} remote={remote_url}")
            await self._relay_local_to_remote(reader, writer, ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            details = f"{type(exc).__name__}: {exc}"
            try:
                code = getattr(exc, "code", None)
                reason = getattr(exc, "reason", None)
                rcvd = getattr(exc, "rcvd", None)
                sent = getattr(exc, "sent", None)
                if code is not None or reason is not None or rcvd is not None or sent is not None:
                    details += f" code={code} reason={reason} rcvd={rcvd} sent={sent}"
            except Exception:
                pass
            self._stats.last_error = details
            if remote_url and _should_quarantine_realtime_remote(details):
                _quarantine_realtime_remote(remote_url, details=details)
                self._stats.remote_quarantine_total = int(self._stats.remote_quarantine_total or 0) + 1
                self._log(
                    f"remote quarantined url={_realtime_remote_quarantine_key(remote_url)} "
                    f"for={_realtime_remote_quarantine_s():.0f}s err={details}"
                )
            self._log(f"session error id={session_id} err={details}")
        finally:
            self._stats.active_session = False
            self._stats.session_close_total = int(self._stats.session_close_total or 0) + 1
            self._stats.last_session_close_at = time.monotonic()
            self._stats.last_remote_disconnect_at = time.monotonic()
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
                with contextlib.suppress(Exception):
                    await ws.wait_closed()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._log(f"session close id={session_id}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self._stats.local_client_total = int(self._stats.local_client_total or 0) + 1
        if self._active_task is not None and not self._active_task.done():
            self._log("superseding previous local NATS client")
            self._stats.superseded_total = int(self._stats.superseded_total or 0) + 1
            self._active_task.cancel()
            with contextlib.suppress(BaseException):
                await self._active_task
        self._active_task = asyncio.create_task(self._bridge_session(reader, writer), name="adaos-realtime-session")
        with contextlib.suppress(BaseException):
            await self._active_task


async def run_realtime_sidecar(*, host: str | None = None, port: int | None = None) -> int:
    apply_realtime_loop_policy()
    server = RealtimeSidecarServer(host=host or realtime_sidecar_host(), port=port or realtime_sidecar_port())
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await server.close()
    return 0
