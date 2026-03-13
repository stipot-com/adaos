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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from adaos.services.capacity import _load_node_yaml as _load_node_yaml
from adaos.services.nats_config import normalize_nats_ws_url, order_nats_ws_candidates
from adaos.services.nats_ws_transport import (
    _set_tcp_keepalive,
    _ws_heartbeat_s_from_env,
    _ws_max_queue_from_env,
    _ws_proxy_from_env,
)
from adaos.services.runtime_dotenv import merged_runtime_dotenv_env

NATS_PING = b"PING\r\n"
NATS_PONG = b"PONG\r\n"


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


def realtime_sidecar_enabled(*, role: str | None = None, os_name: str | None = None) -> bool:
    raw = os.getenv("ADAOS_REALTIME_ENABLE")
    if raw is None:
        raw = os.getenv("HUB_REALTIME_ENABLE")
    if raw is not None:
        return _truthy(raw, default=False)
    name = os_name if os_name is not None else os.name
    return str(role or "").strip().lower() == "hub" and str(name or "").strip().lower() == "nt"


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


def _realtime_ws_heartbeat_s() -> float | None:
    raw = os.getenv("ADAOS_REALTIME_WS_HEARTBEAT_S")
    if raw is not None:
        try:
            value = float(str(raw).strip() or "0")
        except Exception:
            value = 0.0
        if value <= 0.0:
            return None
        if value < 5.0:
            value = 5.0
        return value
    return _ws_heartbeat_s_from_env()


def _realtime_ws_max_queue() -> int | None:
    raw = os.getenv("ADAOS_REALTIME_WS_MAX_QUEUE")
    if raw is None:
        return _ws_max_queue_from_env()
    try:
        value = int(str(raw).strip() or "0")
    except Exception:
        return _ws_max_queue_from_env()
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
    try:
        value = float(str(raw or "15").strip() or "15")
    except Exception:
        value = 15.0
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
        return "selector"
    value = str(raw).strip().lower()
    if value in {"selector", "proactor", "auto"}:
        return value
    return "selector"


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
    try:
        node = _load_node_yaml() or {}
    except Exception:
        node = {}
    nats_cfg = node.get("nats") if isinstance(node, dict) and isinstance(node.get("nats"), dict) else {}
    node_url = normalize_nats_ws_url(str((nats_cfg or {}).get("ws_url") or "").strip(), fallback=None)
    base = normalize_nats_ws_url(explicit_url or node_url, fallback=None)
    candidates: list[str] = []
    for item in [base, "wss://nats.inimatic.com/nats", "wss://api.inimatic.com/nats"]:
        if isinstance(item, str) and item.startswith("ws") and item not in candidates:
            candidates.append(item)
    extra = str(os.getenv("ADAOS_REALTIME_REMOTE_WS_ALT", "") or "").strip()
    if extra:
        for item in [part.strip() for part in extra.split(",") if part.strip()]:
            normalized = normalize_nats_ws_url(item, fallback=None)
            if isinstance(normalized, str) and normalized.startswith("ws") and normalized not in candidates:
                candidates.append(normalized)
    prefer_dedicated = os.getenv("HUB_NATS_PREFER_DEDICATED", "1")
    return order_nats_ws_candidates(candidates, explicit_url=base, prefer_dedicated=prefer_dedicated)


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


async def wait_realtime_sidecar_ready(*, host: str, port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        if await _is_port_open(host, port):
            return True
        await asyncio.sleep(0.1)
    return False


async def start_realtime_sidecar_subprocess(*, role: str | None = None) -> subprocess.Popen[Any] | None:
    if not realtime_sidecar_enabled(role=role):
        return None
    host = realtime_sidecar_host()
    port = realtime_sidecar_port()
    if await _is_port_open(host, port):
        return None
    env = merged_runtime_dotenv_env(os.environ.copy())
    env["ADAOS_REALTIME_ENABLE"] = "1"
    env["ADAOS_REALTIME_CHILD"] = "1"
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
        cwd=os.getcwd(),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=(
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(getattr(subprocess, "DETACHED_PROCESS", 0))
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


class RealtimeSidecarServer:
    def __init__(self, *, host: str, port: int) -> None:
        self._host = str(host or "127.0.0.1")
        self._port = int(port)
        self._server: asyncio.AbstractServer | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._diag_task: asyncio.Task[Any] | None = None
        self._stopped = asyncio.Event()
        self._stats = _RelayStats()

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
        for candidate in resolve_realtime_remote_candidates():
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
                self._log(
                    f"remote connect ok url={target} ping_interval={heartbeat_s} max_queue={max_queue} "
                    f"proxy={proxy} tcp_keepalive={keepalive_ok}"
                )
                return ws, target
            except Exception as exc:
                last_exc = exc
                self._log(f"remote connect failed url={target} err={type(exc).__name__}: {exc}")
        raise RuntimeError(f"realtime remote connect failed: {type(last_exc).__name__}: {last_exc}") from last_exc

    async def _relay_local_to_remote(self, reader: asyncio.StreamReader, ws: Any) -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            self._stats.local_rx_bytes += len(chunk)
            self._stats.last_local_rx_at = time.monotonic()
            if chunk == NATS_PING:
                self._stats.local_nats_pings_tx += 1
                self._stats.client_nats_pings_outstanding += 1
            elif chunk == NATS_PONG:
                self._stats.local_nats_pongs_tx += 1
            await ws.send(chunk)
            self._stats.remote_tx_bytes += len(chunk)
            self._stats.last_remote_tx_at = time.monotonic()

    async def _relay_remote_to_local(self, ws: Any, writer: asyncio.StreamWriter) -> None:
        while True:
            raw = await ws.recv()
            if isinstance(raw, str):
                payload = raw.encode("utf-8", errors="replace")
            else:
                payload = bytes(raw)
            if not payload:
                continue
            self._stats.remote_rx_bytes += len(payload)
            self._stats.last_remote_rx_at = time.monotonic()
            if payload == NATS_PING:
                self._stats.remote_nats_pings_rx += 1
            elif payload == NATS_PONG:
                self._stats.remote_nats_pongs_rx += 1
                if self._stats.client_nats_pings_outstanding > 0:
                    self._stats.client_nats_pings_outstanding -= 1
                    if self._stats.sidecar_nats_pings_outstanding > 0:
                        self._stats.sidecar_nats_pings_outstanding -= 1
                        self._stats.sidecar_nats_pongs_rx += 1
                elif self._stats.sidecar_nats_pings_outstanding > 0:
                    self._stats.sidecar_nats_pings_outstanding -= 1
                    self._stats.sidecar_nats_pongs_rx += 1
                    continue
            writer.write(payload)
            await writer.drain()
            self._stats.local_tx_bytes += len(payload)
            self._stats.last_local_tx_at = time.monotonic()

    async def _sidecar_keepalive_loop(self, ws: Any, *, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            if getattr(ws, "closed", False):
                return
            if self._stats.sidecar_nats_pings_outstanding > 0:
                continue
            await ws.send(NATS_PING)
            self._stats.sidecar_nats_pings_tx += 1
            self._stats.sidecar_nats_pings_outstanding += 1
            self._stats.remote_tx_bytes += len(NATS_PING)
            self._stats.last_remote_tx_at = time.monotonic()

    async def _bridge_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ws = None
        session_id = f"rt-{uuid.uuid4().hex[:10]}"
        self._stats = _RelayStats(session_id=session_id, local_connected_at=time.monotonic())
        try:
            ws, remote_url = await self._connect_remote(session_id=session_id)
            self._stats.remote_url = remote_url
            self._stats.remote_connected_at = time.monotonic()
            interval_s = _realtime_nats_ping_interval_s()
            self._stats.sidecar_nats_ping_interval_s = interval_s
            self._log(f"session open id={session_id} remote={remote_url}")
            tasks = [
                asyncio.create_task(self._relay_local_to_remote(reader, ws), name=f"adaos-realtime-l2r-{session_id}"),
                asyncio.create_task(self._relay_remote_to_local(ws, writer), name=f"adaos-realtime-r2l-{session_id}"),
            ]
            if interval_s is not None:
                tasks.append(
                    asyncio.create_task(
                        self._sidecar_keepalive_loop(ws, interval_s=interval_s),
                        name=f"adaos-realtime-ka-{session_id}",
                    )
                )
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
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
            self._log(f"session error id={session_id} err={details}")
        finally:
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
        if self._active_task is not None and not self._active_task.done():
            self._log("superseding previous local NATS client")
            self._active_task.cancel()
            with contextlib.suppress(Exception):
                await self._active_task
        self._active_task = asyncio.create_task(self._bridge_session(reader, writer), name="adaos-realtime-session")
        with contextlib.suppress(Exception):
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
