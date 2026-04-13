# src\adaos\apps\cli\commands\api.py
import atexit
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
import requests
import typer
import uvicorn

from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config, save_config
from adaos.services.runtime_dotenv import apply_runtime_dotenv_overrides, merged_runtime_dotenv_env
from adaos.apps.cli.active_control import resolve_control_token

apply_runtime_dotenv_overrides()

app = typer.Typer(help="HTTP API for AdaOS")


def _uvicorn_loop_mode() -> str:
    if os.name != "nt":
        return "auto"
    raw = os.getenv("ADAOS_WIN_SELECTOR_LOOP")
    enabled = None
    if raw is not None:
        val = str(raw).strip().lower()
        if val in {"1", "true", "on", "yes"}:
            enabled = True
        elif val in {"0", "false", "off", "no"}:
            enabled = False
    # Default: when hub-root NATS transport is TCP on Windows, prefer selector loop.
    # Proactor-based overlapped IO can produce WinError 121 under prolonged network stalls.
    if enabled is None:
        tr = str(os.getenv("HUB_NATS_TRANSPORT", "") or "").strip().lower()
        if tr == "tcp":
            enabled = True
    if enabled:
        # Uvicorn's Windows "asyncio" path hardcodes ProactorEventLoop.
        # `loop="none"` falls back to asyncio.new_event_loop(), which respects
        # the process-wide event loop policy we set in the CLI / API server.
        return "none"
    return "auto"


def _is_local_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _advertise_base(host: str, port: int) -> str:
    advertised_host = (host or "").strip() or "127.0.0.1"
    if advertised_host in {"0.0.0.0", "::", "[::]"}:
        advertised_host = "127.0.0.1"
    return f"http://{advertised_host}:{int(port)}"


def _resolve_bind(conf, host: str, port: int) -> tuple[str, int]:
    role = str(getattr(conf, "role", "") or "").strip().lower() if conf is not None else ""
    if role != "hub":
        return host, int(port)
    if str(os.getenv("ADAOS_SUPERVISOR_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
        # Supervisor-managed runtimes already pass the slot-specific port explicitly.
        # Do not override it from node.yaml `hub_url`, or slot A can get pulled onto slot B's port.
        return host, int(port)
    if host != "127.0.0.1" or int(port) != 8777:
        return host, int(port)
    hub_url = str(getattr(conf, "hub_url", "") or "").strip()
    if not _is_local_url(hub_url):
        return host, int(port)
    try:
        parsed = urlparse(hub_url)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)
    except Exception:
        pass
    return host, int(port)


def _resolve_stop_bind(conf) -> tuple[str, int] | None:
    if conf is None:
        return None
    hub_url = str(getattr(conf, "hub_url", "") or "").strip()
    if not hub_url:
        return None
    try:
        parsed = urlparse(hub_url)
    except Exception:
        return None
    hostname = str(parsed.hostname or "").strip()
    if not hostname or not parsed.port or not _is_local_url(hub_url):
        return None
    return hostname, int(parsed.port)


def _state_dir() -> Path:
    raw = get_ctx().paths.state_dir()
    path = raw() if callable(raw) else raw
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _pidfile_path(host: str, port: int) -> Path:
    safe_host = str(host or "127.0.0.1").replace(":", "_").replace("/", "_").replace("\\", "_")
    root = _state_dir() / "api"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"serve-{safe_host}-{int(port)}.json"


def _restart_marker_path(host: str, port: int) -> Path:
    safe_host = str(host or "127.0.0.1").replace(":", "_").replace("/", "_").replace("\\", "_")
    root = _state_dir() / "api"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"restart-{safe_host}-{int(port)}.json"


def _write_restart_marker(path: Path, *, host: str, port: int, reason: str, ttl_s: float = 180.0) -> None:
    now = time.time()
    payload = {
        "host": str(host or "127.0.0.1"),
        "port": int(port),
        "reason": str(reason or "cli.restart"),
        "created_at": now,
        "expires_at": now + max(30.0, float(ttl_s)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_restart_marker(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _read_pidfile(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_pidfile(path: Path, *, host: str, port: int, advertised_base: str) -> None:
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": int(port),
        "advertised_base": advertised_base,
        "started_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _host_matches_listener(bind_host: str, listener_host: str | None) -> bool:
    host = str(bind_host or "").strip().lower()
    other = str(listener_host or "").strip().lower()
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        return True
    if host == other:
        return True
    local_any = {"0.0.0.0", "::", "[::]"}
    loopbacks = {"127.0.0.1", "::1", "localhost"}
    if host in loopbacks and (other in loopbacks or other in local_any):
        return True
    return False


def _process_looks_like_adaos_api(proc: psutil.Process) -> bool:
    try:
        cmdline = [str(part).lower() for part in proc.cmdline()]
    except Exception:
        return False
    joined = " ".join(cmdline)
    if "adaos" not in joined:
        return False
    return ("api" in joined and "serve" in joined) or "adaos.apps.autostart_runner" in joined


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


def _process_matches_bind(proc: psutil.Process, host: str, port: int) -> bool:
    try:
        cmdline = [str(part) for part in proc.cmdline()]
    except Exception:
        return False
    if not _process_looks_like_adaos_api(proc):
        return False
    raw_port = _cmdline_option_value(cmdline, "--port")
    try:
        cmd_port = int(str(raw_port or "").strip() or "8777")
    except Exception:
        return False
    if cmd_port != int(port):
        return False
    cmd_host = _cmdline_option_value(cmdline, "--host") or "127.0.0.1"
    return _host_matches_listener(host, cmd_host)


def _current_process_family_pids() -> set[int]:
    protected: set[int] = {os.getpid()}
    try:
        current = psutil.Process(os.getpid())
    except psutil.Error:
        return protected
    try:
        for proc in current.parents():
            pid = int(getattr(proc, "pid", 0) or 0)
            if pid > 0:
                protected.add(pid)
    except psutil.Error:
        pass
    try:
        for proc in current.children(recursive=True):
            pid = int(getattr(proc, "pid", 0) or 0)
            if pid > 0:
                protected.add(pid)
    except psutil.Error:
        pass
    return protected


def _find_matching_server_pids(host: str, port: int, *, protected_pids: set[int] | None = None) -> list[int]:
    matches: list[int] = []
    blocked = protected_pids or set()
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid == os.getpid() or pid in blocked:
                continue
            if _process_matches_bind(proc, host, port):
                matches.append(pid)
    except Exception:
        return matches
    return matches


def _find_listening_server_pid(host: str, port: int) -> int | None:
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


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
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


def _stop_previous_server(host: str, port: int) -> None:
    pidfile = _pidfile_path(host, port)
    protected_pids = _current_process_family_pids()
    candidate_pids: list[int] = []
    meta = _read_pidfile(pidfile)
    try:
        file_pid = int((meta or {}).get("pid") or 0)
    except Exception:
        file_pid = 0
    if file_pid > 0 and file_pid != os.getpid() and file_pid not in protected_pids:
        candidate_pids.append(file_pid)
    owner_pid = _find_listening_server_pid(host, port)
    if owner_pid and owner_pid != os.getpid() and owner_pid not in protected_pids and owner_pid not in candidate_pids:
        candidate_pids.append(owner_pid)
    for pid in _find_matching_server_pids(host, port, protected_pids=protected_pids):
        if pid not in candidate_pids:
            candidate_pids.append(pid)

    for pid in candidate_pids:
        try:
            proc = psutil.Process(pid)
        except psutil.Error:
            continue
        if not _process_looks_like_adaos_api(proc):
            continue
        _terminate_process_tree(pid)

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        owner_pid = _find_listening_server_pid(host, port)
        if not owner_pid or owner_pid == os.getpid():
            break
        time.sleep(0.1)
    try:
        if not candidate_pids and pidfile.exists():
            pidfile.unlink()
    except Exception:
        pass


def _cleanup_pidfile(path: Path) -> None:
    try:
        data = _read_pidfile(path)
        if int((data or {}).get("pid") or 0) == os.getpid():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _wait_for_server_exit(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        owner_pid = _find_listening_server_pid(host, port)
        remaining = _find_matching_server_pids(host, port, protected_pids=_current_process_family_pids())
        if not owner_pid and not remaining:
            return True
        time.sleep(0.1)
    return False


def _wait_for_server_start(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        owner_pid = _find_listening_server_pid(host, port)
        if owner_pid and owner_pid != os.getpid():
            return True
        time.sleep(0.1)
    return False


def _spawn_detached_server(host: str, port: int, *, token: str | None, reload: bool = False) -> None:
    args = [
        sys.executable,
        "-m",
        "adaos.apps.cli.commands.api",
        "serve",
        "--host",
        str(host),
        "--port",
        str(int(port)),
    ]
    if reload:
        args.append("--reload")
    if token:
        args.extend(["--token", str(token)])

    env = merged_runtime_dotenv_env(os.environ.copy())
    creationflags = 0
    popen_kwargs: dict[str, object] = {
        "args": args,
        "cwd": os.getcwd(),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        popen_kwargs["startupinfo"] = startupinfo
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(**popen_kwargs)


def _request_graceful_shutdown(host: str, port: int, *, token: str | None, reason: str = "cli.stop") -> bool:
    url = f"http://{host}:{int(port)}/api/admin/shutdown"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = str(token)
    try:
        response = requests.post(
            url,
            json={"reason": reason, "drain_timeout_sec": 5.0, "signal_delay_sec": 0.2},
            headers=headers,
            timeout=(2.0, 15.0),
        )
    except Exception:
        return False
    if response.status_code not in (200, 202):
        return False
    return _wait_for_server_exit(host, port, timeout=20.0)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload"),
    token: str | None = typer.Option(None, "--token", help="Override X-AdaOS-Token / ADAOS_TOKEN"),
):
    """Serve the AdaOS local HTTP API."""
    from adaos.apps.api.server import app as server_app

    conf = None
    try:
        conf = load_config()
    except Exception:
        conf = None

    host, port = _resolve_bind(conf, host, port)
    advertised_base = _advertise_base(host, port)
    pidfile = _pidfile_path(host, port)

    _stop_previous_server(host, port)
    _write_pidfile(pidfile, host=host, port=port, advertised_base=advertised_base)
    atexit.register(_cleanup_pidfile, pidfile)

    if conf is not None and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
        try:
            if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                conf.hub_url = advertised_base
                save_config(conf)
        except Exception:
            pass

    if token:
        os.environ["ADAOS_TOKEN"] = token
    try:
        os.environ["ADAOS_SELF_BASE_URL"] = advertised_base
    except Exception:
        pass

    try:
        loop_mode = _uvicorn_loop_mode()
        if os.getenv("HUB_NATS_TRACE", "0") == "1" or os.getenv("ADAOS_CLI_DEBUG", "0") == "1":
            try:
                print(f"[AdaOS] uvicorn loop mode={loop_mode}")
            except Exception:
                pass
        uvicorn.run(
            server_app,
            host=host,
            port=int(port),
            loop=loop_mode,
            reload=reload,
            workers=1,
            access_log=False,
        )
    finally:
        _cleanup_pidfile(pidfile)


@app.command("stop")
def stop():
    """Stop the AdaOS local HTTP API resolved from node.yaml hub_url."""
    try:
        conf = load_config()
    except Exception as exc:
        typer.secho(f"[AdaOS] failed to load node.yaml: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    bind = _resolve_stop_bind(conf)
    if bind is None:
        typer.secho(
            "[AdaOS] node.yaml does not contain a local hub_url with explicit host:port",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    host, port = bind
    pidfile = _pidfile_path(host, port)
    had_pidfile = pidfile.exists()
    owner_pid = _find_listening_server_pid(host, port)
    extra_pids = _find_matching_server_pids(host, port, protected_pids=_current_process_family_pids())

    stopped_gracefully = False
    if owner_pid or extra_pids:
        stopped_gracefully = _request_graceful_shutdown(
            host,
            port,
            token=getattr(conf, "token", None) or resolve_control_token(),
        )

    if not stopped_gracefully:
        _stop_previous_server(host, port)

    remaining_owner = _find_listening_server_pid(host, port)
    remaining_pids = _find_matching_server_pids(host, port, protected_pids=_current_process_family_pids())
    if remaining_owner or remaining_pids:
        typer.secho(
            f"[AdaOS] failed to stop api server at {host}:{port}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    if owner_pid or extra_pids or had_pidfile:
        if stopped_gracefully:
            typer.echo(f"Stopped AdaOS API gracefully at http://{host}:{port}")
        else:
            typer.echo(f"Stopped AdaOS API at http://{host}:{port}")
    else:
        typer.echo(f"No AdaOS API server running at http://{host}:{port}")


@app.command("restart")
def restart():
    """Restart the AdaOS local HTTP API with a single Telegram notification."""
    try:
        conf = load_config()
    except Exception as exc:
        typer.secho(f"[AdaOS] failed to load node.yaml: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    bind = _resolve_stop_bind(conf)
    if bind is None:
        typer.secho(
            "[AdaOS] node.yaml does not contain a local hub_url with explicit host:port",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    host, port = bind
    token = getattr(conf, "token", None) or resolve_control_token()
    marker = _restart_marker_path(host, port)
    _write_restart_marker(marker, host=host, port=port, reason="cli.restart")

    stopped_gracefully = False
    try:
        stopped_gracefully = _request_graceful_shutdown(host, port, token=token, reason="cli.restart")
        if not stopped_gracefully:
            _stop_previous_server(host, port)
            if _find_listening_server_pid(host, port) or _find_matching_server_pids(
                host, port, protected_pids=_current_process_family_pids()
            ):
                raise RuntimeError(f"failed to stop api server at {host}:{port}")

        _spawn_detached_server(host, port, token=token, reload=False)
        if not _wait_for_server_start(host, port, timeout=20.0):
            raise RuntimeError(f"api server did not start at {host}:{port}")
    except Exception as exc:
        _clear_restart_marker(marker)
        typer.secho(f"[AdaOS] restart failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    mode = "gracefully" if stopped_gracefully else "after hard stop"
    typer.echo(f"Restarted AdaOS API {mode} at http://{host}:{port}")


if __name__ == "__main__":
    app()
