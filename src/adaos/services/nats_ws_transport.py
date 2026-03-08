from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import socket
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import ParseResult

_ORIG_NATS_WS_TRANSPORT: Any | None = None
_ORIG_NATS_WS_TRANSPORT_CLIENT: Any | None = None


def _ws_impl_from_env() -> str:
    try:
        raw = str(os.getenv("HUB_NATS_WS_IMPL", "websockets") or "websockets").strip().lower()
    except Exception:
        raw = "websockets"
    if raw in ("auto", ""):
        return "websockets"
    if raw in ("ws", "websockets"):
        return "websockets"
    if raw in ("aio", "aiohttp"):
        return "aiohttp"
    return "websockets"


def _ws_max_size_from_env() -> int | None:
    """
    websockets `max_size` (incoming message limit).
    - empty / unset: unlimited (None) (safe for large Yjs / route proxy frames)
    - 0 or negative: unlimited (None)
    - >0: explicit cap
    """
    try:
        raw = os.getenv("HUB_NATS_WS_MAX_MSG_SIZE")
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        v = int(s)
    except Exception:
        return None
    if v <= 0:
        return None
    return v


def _ws_proxy_from_env() -> str | bool | None:
    """
    Control proxy handling for long-lived NATS-over-WS tunnels.

    `websockets>=15` defaults to `proxy=True`, which may pick up system proxy
    settings on Windows and route the handshake through HTTP CONNECT. That path
    has been observed to fail with `InvalidStateError` inside websockets' client
    parser before the connection is handed to AdaOS.

    Default:
    - Windows: disable auto-proxy (`None`)
    - Other OSes: keep library default (`True`)

    Override with `HUB_NATS_WS_PROXY`:
    - `auto`, `system`, `default`, `1`, `true`, `yes` -> `True`
    - `none`, `off`, `0`, `false`, `no`, empty`       -> `None`
    - any other value                                 -> explicit proxy URL
    """
    raw = os.getenv("HUB_NATS_WS_PROXY")
    if raw is None:
        return None if os.name == "nt" else True
    try:
        value = str(raw).strip()
    except Exception:
        return None if os.name == "nt" else True
    if not value:
        return None
    normalized = value.lower()
    if normalized in {"auto", "system", "default", "1", "true", "yes", "on"}:
        return True
    if normalized in {"none", "off", "0", "false", "no"}:
        return None
    return value


def _ws_headers_to_tuples(headers: Optional[Dict[str, List[str]]]) -> Optional[List[Tuple[str, str]]]:
    if not headers:
        return None
    out: list[tuple[str, str]] = []
    for name, values in headers.items():
        if not name:
            continue
        if isinstance(values, list):
            for value in values:
                try:
                    out.append((str(name), str(value)))
                except Exception:
                    continue
        else:
            try:
                out.append((str(name), str(values)))
            except Exception:
                continue
    return out or None


def _extract_ws_tag(headers: Optional[Dict[str, List[str]]]) -> str | None:
    if not headers:
        return None
    try:
        values = headers.get("X-AdaOS-Nats-Conn") or headers.get("x-adaos-nats-conn") or []
        if isinstance(values, list) and values:
            v0 = str(values[0]).strip()
            return v0 or None
        return None
    except Exception:
        return None


def _wiretap_enabled() -> bool:
    try:
        return str(os.getenv("HUB_NATS_WIRETAP", "0") or "0").strip() == "1"
    except Exception:
        return False


def _wiretap_max_bytes() -> int:
    try:
        raw = os.getenv("HUB_NATS_WIRETAP_MAX_BYTES", "200") or "200"
        v = int(str(raw).strip())
    except Exception:
        v = 200
    if v < 32:
        v = 32
    if v > 4096:
        v = 4096
    return v


def _wiretap_every_n() -> int:
    try:
        raw = os.getenv("HUB_NATS_WIRETAP_EVERY_N", "1") or "1"
        v = int(str(raw).strip())
    except Exception:
        v = 1
    if v < 1:
        v = 1
    return v


def _wiretap_skip_kinds() -> set[str]:
    try:
        raw = str(os.getenv("HUB_NATS_WIRETAP_SKIP", "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return set()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _wiretap_head(raw: bytes, max_bytes: int) -> str:
    if not raw:
        return ""
    head = raw[:max_bytes]
    try:
        text = head.decode("utf-8", errors="replace")
    except Exception:
        try:
            text = repr(head)
        except Exception:
            return ""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _nats_head_info(raw: bytes) -> tuple[str, str | None]:
    if not raw:
        return "RAW", None
    line = raw.split(b"\n", 1)[0].strip()
    if not line:
        return "RAW", None
    try:
        if line.startswith(b"PING"):
            return "PING", None
        if line.startswith(b"PONG"):
            return "PONG", None
        if line.startswith(b"+OK"):
            return "+OK", None
        if line.startswith(b"-ERR"):
            return "-ERR", None
        if line.startswith(b"INFO"):
            return "INFO", None
        parts = line.split()
        if not parts:
            return "RAW", None
        kind = parts[0].decode("utf-8", errors="replace").upper()
        subj = None
        if kind in ("PUB", "SUB", "MSG") and len(parts) >= 2:
            try:
                subj = parts[1].decode("utf-8", errors="replace")
            except Exception:
                subj = None
        return kind, subj
    except Exception:
        return "RAW", None


_NATS_ROUTE_MSG_RE = re.compile(br"(?:^|\n)MSG (route\.(?:to_hub|to_browser)\.[^\s\r\n]+)")


def _route_trace_enabled() -> bool:
    raw = os.getenv("HUB_NATS_ROUTE_TRACE")
    if raw is not None:
        try:
            return str(raw).strip() == "1"
        except Exception:
            return False
    try:
        return (
            str(os.getenv("HUB_NATS_WS_TRACE", "0") or "0").strip() == "1"
            or str(os.getenv("HUB_ROUTE_TRACE", "0") or "0").strip() == "1"
            or str(os.getenv("HUB_TRACE", "0") or "0").strip() == "1"
        )
    except Exception:
        return False


def _extract_route_subjects(
    raw: bytes,
    *,
    prefix: bytes | None = None,
    limit: int = 8192,
    max_subjects: int = 8,
) -> list[str]:
    if not raw:
        return []
    try:
        head = raw[:limit]
    except Exception:
        head = raw
    out: list[str] = []
    seen: set[str] = set()
    try:
        for match in _NATS_ROUTE_MSG_RE.finditer(head):
            subj_b = match.group(1)
            if prefix and not subj_b.startswith(prefix):
                continue
            subj = subj_b.decode("utf-8", errors="replace")
            if not subj or subj in seen:
                continue
            seen.add(subj)
            out.append(subj)
            if len(out) >= max_subjects:
                break
    except Exception:
        return out
    return out


def _route_rx_trace_line(url: str | None, data: bytes, nc: Any) -> str | None:
    subjects = _extract_route_subjects(data, prefix=b"route.to_hub.")
    if not subjects:
        return None
    state, buf_len = _nats_parser_diag(nc)
    shown = ",".join(subjects[:4])
    more = ""
    if len(subjects) > 4:
        more = f",...+{len(subjects) - 4}"
    return (
        f"nats ws route rx url={url} count={len(subjects)} "
        f"subjects={shown}{more} size={len(data)} "
        f"parser_state={state} parser_buf_len={buf_len}"
    )


def _route_tx_trace_line(url: str | None, subj: str | None, payload: bytes, pending_q: Any) -> str | None:
    if not subj or not subj.startswith("route.to_browser."):
        return None
    try:
        qsize = pending_q.qsize() if pending_q is not None else None
    except Exception:
        qsize = None
    return f"nats ws route tx url={url} subj={subj} size={len(payload)} pending_q={qsize}"


def _tcp_keepalive_enabled() -> bool:
    try:
        raw = os.getenv("HUB_NATS_TCP_KEEPALIVE")
    except Exception:
        raw = None
    if raw is None:
        return os.name == "nt"
    try:
        val = str(raw).strip().lower()
    except Exception:
        val = ""
    if not val:
        return os.name == "nt"
    return val not in {"0", "false", "off", "no"}


def _tcp_keepalive_params() -> tuple[float, float, int]:
    try:
        idle_s = float(os.getenv("HUB_NATS_TCP_KEEPALIVE_S", "30") or "30")
    except Exception:
        idle_s = 30.0
    try:
        interval_s = float(os.getenv("HUB_NATS_TCP_KEEPALIVE_INTERVAL_S", "10") or "10")
    except Exception:
        interval_s = 10.0
    try:
        probes = int(os.getenv("HUB_NATS_TCP_KEEPALIVE_PROBES", "5") or "5")
    except Exception:
        probes = 5
    if idle_s < 5.0:
        idle_s = 5.0
    if interval_s < 1.0:
        interval_s = 1.0
    if probes < 1:
        probes = 1
    return idle_s, interval_s, probes


def _set_tcp_keepalive(sock: Any) -> bool:
    if sock is None:
        return False
    if not _tcp_keepalive_enabled():
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        return False
    idle_s, interval_s, probes = _tcp_keepalive_params()
    if os.name == "nt":
        try:
            keepalive_vals = (1, int(idle_s * 1000), int(interval_s * 1000))
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, keepalive_vals)  # type: ignore[attr-defined]
        except Exception:
            pass
        return True
    try:
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, int(idle_s))
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, int(interval_s))
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, int(probes))
    except Exception:
        pass
    return True


def _exact_nats_control_frame(data: bytes) -> str | None:
    if data == b"PING\r\n":
        return "PING"
    if data == b"PONG\r\n":
        return "PONG"
    return None


def _nats_parser_diag(nc: Any) -> tuple[Any, int | None]:
    ps = getattr(nc, "_ps", None)
    state = getattr(ps, "state", None) if ps is not None else None
    buf = getattr(ps, "buf", None) if ps is not None else None
    try:
        buf_len = len(buf) if buf is not None else None
    except Exception:
        buf_len = None
    return state, buf_len

class WebSocketTransportWebsockets:
    """
    Drop-in replacement for `nats.aio.transport.WebSocketTransport` using `websockets` instead of `aiohttp`.

    Why:
    - On some Windows environments aiohttp WS client disconnects with close 1006 under sustained PUB load
      (`Cannot write to closing transport`), causing hub to appear offline in browser (`yjs_sync_timeout`).
    - `websockets` library has been observed to be stable in the same conditions.

    Notes:
    - We intentionally disable websockets' own keepalive pings; Root's ws-nats-proxy sends NATS protocol keepalives.
    - We default to unlimited `max_size` to avoid disconnects on large frames (Yjs sync can exceed 1 MiB).
    """

    def __init__(self, ws_headers: Optional[Dict[str, List[str]]] = None):
        self._ws: Any = None
        self._pending: asyncio.Queue = asyncio.Queue()
        self._close_task: asyncio.Task | None = None
        self._using_tls: Optional[bool] = None
        self._ws_headers: Optional[Dict[str, List[str]]] = ws_headers
        self._adaos_ws_trace: bool = os.getenv("HUB_NATS_WS_TRACE", "0") == "1"
        self._adaos_route_trace: bool = _route_trace_enabled()
        self._adaos_ws_raise_on_recv_err: bool = os.getenv("HUB_NATS_WS_RAISE_ON_RECV_ERR", "1") == "1"
        self._adaos_wiretap: bool = _wiretap_enabled()
        self._adaos_wiretap_max: int = _wiretap_max_bytes()
        self._adaos_wiretap_every: int = _wiretap_every_n()
        self._adaos_wiretap_skip: set[str] = _wiretap_skip_kinds()
        self._adaos_wiretap_rx: int = 0
        self._adaos_wiretap_tx: int = 0

        # Diagnostics (best-effort, used by hub logs).
        self._adaos_last_rx_at: float | None = None
        self._adaos_last_tx_at: float | None = None
        self._adaos_last_tx_kind: str | None = None
        self._adaos_last_tx_subj: str | None = None
        self._adaos_last_tx_len: int | None = None
        self._adaos_ws_tag: str | None = _extract_ws_tag(ws_headers)
        self._adaos_ws_url: str | None = None
        self._adaos_ws_proto: str | None = None
        self._adaos_tx_connect_at: float | None = None
        self._adaos_rx_info_at: float | None = None
        self._adaos_nats_max_payload: int | None = None
        self._adaos_last_recv_error: Exception | None = None
        self._adaos_last_recv_error_at: float | None = None
        self._adaos_nc: Any = None

    def _trace(self, msg: str) -> None:
        if not self._adaos_ws_trace:
            return
        try:
            print(f"[hub-io] {msg}")
        except Exception:
            pass

    def _wiretap_log(self, msg: str) -> None:
        if not self._adaos_wiretap:
            return
        try:
            print(f"[hub-io] {msg}")
        except Exception:
            pass

    def _wiretap(self, direction: str, data: Any) -> None:
        if not self._adaos_wiretap:
            return
        try:
            if direction == "rx":
                self._adaos_wiretap_rx += 1
                seq = self._adaos_wiretap_rx
            else:
                self._adaos_wiretap_tx += 1
                seq = self._adaos_wiretap_tx
            every = self._adaos_wiretap_every
            if every > 1 and (seq % every) != 0:
                return
            if data is None:
                return
            if isinstance(data, str):
                raw = data.encode("utf-8", errors="replace")
            elif isinstance(data, memoryview):
                raw = data.tobytes()
            elif isinstance(data, (bytes, bytearray)):
                raw = bytes(data)
            else:
                try:
                    raw = bytes(data)
                except Exception:
                    return
            kind, subj = _nats_head_info(raw)
            if kind.upper() in self._adaos_wiretap_skip:
                return
            head = _wiretap_head(raw, self._adaos_wiretap_max)
            subj_part = f" subj={subj}" if subj else ""
            self._wiretap_log(
                f"nats ws wiretap {direction} kind={kind}{subj_part} size={len(raw)} head={head}"
            )
        except Exception:
            pass

    async def connect(self, uri: ParseResult, buffer_size: int, connect_timeout: int) -> None:
        await self._connect_impl(uri.geturl(), ssl_context=None, connect_timeout=connect_timeout)
        self._using_tls = False

    async def connect_tls(
        self,
        uri: Union[str, ParseResult],
        ssl_context: ssl.SSLContext,
        buffer_size: int,
        connect_timeout: int,
    ) -> None:
        target = uri if isinstance(uri, str) else uri.geturl()
        await self._connect_impl(target, ssl_context=ssl_context, connect_timeout=connect_timeout)
        self._using_tls = True

    def write(self, payload: bytes) -> None:
        self._pending.put_nowait(payload)

    def writelines(self, payload: List[bytes]) -> None:
        for message in payload:
            self.write(message)

    async def read(self, buffer_size: int) -> bytes:
        return await self.readline()

    async def _maybe_consume_direct_control_frame(self, data: bytes) -> bool:
        kind = _exact_nats_control_frame(data)
        if kind is None:
            return False
        nc = getattr(self, "_adaos_nc", None)
        if nc is None:
            return False
        handler_name = "_process_ping" if kind == "PING" else "_process_pong"
        handler = getattr(nc, handler_name, None)
        if not callable(handler):
            return False
        state, buf_len = _nats_parser_diag(nc)
        self._wiretap("rx", data)
        if self._adaos_ws_trace:
            self._trace(
                f"nats ws direct control rx kind={kind} parser_state={state} parser_buf_len={buf_len}"
            )
        try:
            await handler()
        except Exception as e:
            self._adaos_last_recv_error = e
            self._adaos_last_recv_error_at = time.monotonic()
            raise
        return True

    async def readline(self) -> bytes:
        while True:
            ws = self._ws
            if ws is None:
                return b""
            try:
                raw = await ws.recv()
            except Exception as e:
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        code = getattr(e, "code", None)
                        reason = getattr(e, "reason", None)
                        rcvd = getattr(e, "rcvd", None)
                        sent = getattr(e, "sent", None)
                        state = getattr(ws, "state", None)
                        self._trace(
                            "nats ws recv failed "
                            f"url={self._adaos_ws_url} err={type(e).__name__}: {e} "
                            f"code={code} reason={reason} rcvd={rcvd} sent={sent} state={state}"
                        )
                    except Exception:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
                if self._adaos_ws_raise_on_recv_err:
                    raise
                return b""
            try:
                self._adaos_last_rx_at = time.monotonic()
            except Exception:
                pass
            if isinstance(raw, str):
                data = raw.encode("utf-8")
            elif isinstance(raw, (bytes, bytearray, memoryview)):
                data = bytes(raw)
            else:
                data = b""
            if data and await self._maybe_consume_direct_control_frame(data):
                continue
            self._wiretap("rx", data)
            if self._adaos_route_trace and data:
                try:
                    line = _route_rx_trace_line(self._adaos_ws_url, data, getattr(self, "_adaos_nc", None))
                    if line:
                        self._trace(line)
                except Exception:
                    pass
            # Parse INFO once (best-effort) to expose max_payload in diagnostics.
            try:
                if data.startswith(b"INFO "):
                    self._adaos_rx_info_at = time.monotonic()
                    line_end = data.find(b"\n")
                    if line_end < 0:
                        line_end = len(data)
                    line = data[:line_end].strip()
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    js0 = line[len(b"INFO ") :].strip()
                    obj = json.loads(js0.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict):
                        mp = obj.get("max_payload", None)
                        if isinstance(mp, int):
                            self._adaos_nats_max_payload = mp
            except Exception:
                pass
            return data

    async def drain(self) -> None:
        ws = self._ws
        if ws is None:
            return
        while not self._pending.empty():
            message = self._pending.get_nowait()
            if isinstance(message, memoryview):
                payload = message.tobytes()
            elif isinstance(message, (bytes, bytearray)):
                payload = bytes(message)
            else:
                payload = message
            try:
                self._adaos_last_tx_at = time.monotonic()
            except Exception:
                pass
            self._wiretap("tx", payload)
            # Extract command kind + subject (no payload logging) for later error diagnostics.
            try:
                head = payload[:256] if isinstance(payload, (bytes, bytearray)) else b""
                kind = None
                subj = None
                if isinstance(head, (bytes, bytearray)):
                    if head.startswith(b"PUB "):
                        kind = "PUB"
                    elif head.startswith(b"SUB "):
                        kind = "SUB"
                    elif head.startswith(b"CONNECT "):
                        kind = "CONNECT"
                    elif head.startswith(b"PING"):
                        kind = "PING"
                    elif head.startswith(b"PONG"):
                        kind = "PONG"
                    if kind in ("PUB", "SUB"):
                        line_end = head.find(b"\n")
                        if line_end < 0:
                            line_end = len(head)
                        parts = head[:line_end].split()
                        if len(parts) >= 2:
                            subj = parts[1].decode("utf-8", errors="replace")
                    if kind == "CONNECT":
                        self._adaos_tx_connect_at = time.monotonic()
                self._adaos_last_tx_kind = kind
                self._adaos_last_tx_subj = subj
                try:
                    self._adaos_last_tx_len = len(payload) if hasattr(payload, "__len__") else None
                except Exception:
                    self._adaos_last_tx_len = None
            except Exception:
                pass
            try:
                await ws.send(payload)
            except Exception as e:
                if self._adaos_ws_trace:
                    self._trace(f"nats ws send failed url={self._adaos_ws_url} err={type(e).__name__}: {e}")
                raise
            if self._adaos_ws_trace:
                try:
                    line = _route_tx_trace_line(
                        self._adaos_ws_url,
                        self._adaos_last_tx_subj,
                        payload,
                        self._pending,
                    )
                    if line:
                        self._trace(line)
                except Exception:
                    pass

    async def wait_closed(self) -> None:
        try:
            if self._close_task is not None:
                await self._close_task
        except Exception:
            pass
        try:
            ws = self._ws
            if ws is not None and callable(getattr(ws, "wait_closed", None)):
                await ws.wait_closed()
        except Exception:
            pass
        if self._adaos_ws_trace:
            try:
                ws = self._ws
                code = getattr(ws, "close_code", None) if ws is not None else None
                reason = getattr(ws, "close_reason", None) if ws is not None else None
                exc = None
                try:
                    exf = getattr(ws, "exception", None)
                    if callable(exf):
                        exc = exf()
                except Exception:
                    exc = None
                self._trace(f"nats ws closed url={self._adaos_ws_url} code={code} reason={reason} exc={exc}")
            except Exception:
                pass
        self._ws = None

    def close(self) -> None:
        ws = self._ws
        if ws is None:
            return
        if self._adaos_ws_trace:
            try:
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", None)
                self._trace(f"nats ws close requested url={self._adaos_ws_url} code={code} reason={reason}")
            except Exception:
                pass
        try:
            self._close_task = asyncio.create_task(ws.close())
        except Exception:
            self._close_task = None

    def at_eof(self) -> bool:
        ws = self._ws
        if ws is None:
            return True
        closed = getattr(ws, "closed", None)
        if closed is not None:
            try:
                return bool(closed)
            except Exception:
                return True
        state = getattr(ws, "state", None)
        try:
            return str(state).endswith("CLOSED")
        except Exception:
            return False

    def __bool__(self) -> bool:
        return self._ws is not None

    async def _connect_impl(self, target: str, *, ssl_context: ssl.SSLContext | None, connect_timeout: int) -> None:
        try:
            import websockets  # type: ignore
        except Exception as e:
            raise RuntimeError(f"websockets is required for NATS-over-WS: {type(e).__name__}: {e}") from e

        headers = _ws_headers_to_tuples(self._ws_headers)
        max_size = _ws_max_size_from_env()
        ws_kwargs: dict[str, Any] = {
            "subprotocols": ["nats"],
            "open_timeout": connect_timeout,
            "ping_interval": None,
            "ping_timeout": None,
            "close_timeout": 2.0,
            "max_size": max_size,
            "compression": None,
            "proxy": _ws_proxy_from_env(),
        }
        if ssl_context is not None:
            ws_kwargs["ssl"] = ssl_context

        self._adaos_ws_url = str(target)
        if self._adaos_ws_trace:
            try:
                self._trace(
                    "nats ws connect start "
                    f"url={self._adaos_ws_url} tls={ssl_context is not None} "
                    f"proxy={ws_kwargs.get('proxy')} max_size={max_size} tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        try:
            if callable(getattr(self._ws, "close", None)) and not self.at_eof():
                await self._ws.close()
        except Exception:
            pass

        try:
            try:
                if headers:
                    self._ws = await websockets.connect(target, additional_headers=headers, **ws_kwargs)
                else:
                    self._ws = await websockets.connect(target, **ws_kwargs)
            except TypeError:
                # Older websockets may use `extra_headers=...` and may not support `proxy=...`.
                retry_kwargs = dict(ws_kwargs)
                retry_kwargs.pop("proxy", None)
                if headers:
                    self._ws = await websockets.connect(target, extra_headers=headers, **retry_kwargs)
                else:
                    self._ws = await websockets.connect(target, **retry_kwargs)
        except Exception as e:
            if self._adaos_ws_trace:
                try:
                    self._trace(f"nats ws connect failed url={self._adaos_ws_url} err={type(e).__name__}: {e}")
                except Exception:
                    pass
            self._ws = None
            raise

        try:
            self._adaos_last_rx_at = time.monotonic()
        except Exception:
            pass
        try:
            self._adaos_ws_proto = (
                getattr(self._ws, "subprotocol", None)
                or getattr(self._ws, "protocol", None)
                or self._adaos_ws_proto
            )
        except Exception:
            pass
        try:
            sock = None
            try:
                transport = getattr(self._ws, "transport", None)
                if transport is not None and callable(getattr(transport, "get_extra_info", None)):
                    sock = transport.get_extra_info("socket")
            except Exception:
                sock = None
            if _set_tcp_keepalive(sock):
                if self._adaos_ws_trace:
                    try:
                        idle_s, interval_s, probes = _tcp_keepalive_params()
                        self._trace(
                            f"nats ws tcp keepalive enabled idle_s={idle_s} interval_s={interval_s} probes={probes}"
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        if self._adaos_ws_trace:
            try:
                ws = self._ws
                proto = self._adaos_ws_proto
                remote = getattr(ws, "remote_address", None) if ws is not None else None
                local = getattr(ws, "local_address", None) if ws is not None else None
                status = None
                try:
                    resp = getattr(ws, "response", None) or getattr(ws, "_response", None)
                    status = getattr(resp, "status", None) if resp is not None else None
                except Exception:
                    status = None
                self._trace(
                    f"nats ws connect ok url={self._adaos_ws_url} proto={proto} status={status} remote={remote} local={local}"
                )
            except Exception:
                pass


class WebSocketTransportAiohttp:
    """
    Aiohttp-based NATS WS transport with extra diagnostics (mirrors the default transport).
    """

    def __init__(self, ws_headers: Optional[Dict[str, List[str]]] = None):
        try:
            import aiohttp  # type: ignore
        except Exception as e:
            raise ImportError(
                "Could not import aiohttp transport, please install it with `pip install aiohttp`"
            ) from e
        self._aiohttp = aiohttp
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._client: aiohttp.ClientSession = aiohttp.ClientSession()
        self._pending: asyncio.Queue = asyncio.Queue()
        self._close_task = asyncio.Future()
        self._using_tls: Optional[bool] = None
        self._ws_headers: Optional[Dict[str, List[str]]] = ws_headers
        self._adaos_ws_trace: bool = os.getenv("HUB_NATS_WS_TRACE", "0") == "1"
        self._adaos_route_trace: bool = _route_trace_enabled()
        self._adaos_ws_raise_on_recv_err: bool = os.getenv("HUB_NATS_WS_RAISE_ON_RECV_ERR", "1") == "1"
        self._adaos_wiretap: bool = _wiretap_enabled()
        self._adaos_wiretap_max: int = _wiretap_max_bytes()
        self._adaos_wiretap_every: int = _wiretap_every_n()
        self._adaos_wiretap_skip: set[str] = _wiretap_skip_kinds()
        self._adaos_wiretap_rx: int = 0
        self._adaos_wiretap_tx: int = 0

        # Diagnostics (best-effort, used by hub logs).
        self._adaos_last_rx_at: float | None = None
        self._adaos_last_tx_at: float | None = None
        self._adaos_last_tx_kind: str | None = None
        self._adaos_last_tx_subj: str | None = None
        self._adaos_last_tx_len: int | None = None
        self._adaos_ws_tag: str | None = _extract_ws_tag(ws_headers)
        self._adaos_ws_url: str | None = None
        self._adaos_ws_proto: str | None = None
        self._adaos_tx_connect_at: float | None = None
        self._adaos_rx_info_at: float | None = None
        self._adaos_nats_max_payload: int | None = None
        self._adaos_last_recv_error: Exception | None = None
        self._adaos_last_recv_error_at: float | None = None
        self._adaos_nc: Any = None

    def _trace(self, msg: str) -> None:
        if not self._adaos_ws_trace:
            return
        try:
            print(f"[hub-io] {msg}")
        except Exception:
            pass

    def _wiretap_log(self, msg: str) -> None:
        if not self._adaos_wiretap:
            return
        try:
            print(f"[hub-io] {msg}")
        except Exception:
            pass

    def _wiretap(self, direction: str, data: Any) -> None:
        if not self._adaos_wiretap:
            return
        try:
            if direction == "rx":
                self._adaos_wiretap_rx += 1
                seq = self._adaos_wiretap_rx
            else:
                self._adaos_wiretap_tx += 1
                seq = self._adaos_wiretap_tx
            every = self._adaos_wiretap_every
            if every > 1 and (seq % every) != 0:
                return
            if data is None:
                return
            if isinstance(data, str):
                raw = data.encode("utf-8", errors="replace")
            elif isinstance(data, memoryview):
                raw = data.tobytes()
            elif isinstance(data, (bytes, bytearray)):
                raw = bytes(data)
            else:
                try:
                    raw = bytes(data)
                except Exception:
                    return
            kind, subj = _nats_head_info(raw)
            if kind.upper() in self._adaos_wiretap_skip:
                return
            head = _wiretap_head(raw, self._adaos_wiretap_max)
            subj_part = f" subj={subj}" if subj else ""
            self._wiretap_log(
                f"nats ws wiretap {direction} kind={kind}{subj_part} size={len(raw)} head={head}"
            )
        except Exception:
            pass

    async def connect(self, uri: ParseResult, buffer_size: int, connect_timeout: int) -> None:
        headers = self._get_custom_headers()
        self._adaos_ws_url = uri.geturl()
        if self._adaos_ws_trace:
            try:
                self._trace(
                    f"nats ws connect start url={self._adaos_ws_url} tls=False proxy=None max_size=None tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        self._ws = await self._client.ws_connect(uri.geturl(), timeout=connect_timeout, headers=headers)
        self._using_tls = False
        self._after_connect()

    async def connect_tls(
        self,
        uri: Union[str, ParseResult],
        ssl_context: ssl.SSLContext,
        buffer_size: int,
        connect_timeout: int,
    ) -> None:
        if self._ws and not self._ws.closed:
            if self._using_tls:
                return
            raise RuntimeError("ws: cannot upgrade to TLS")
        headers = self._get_custom_headers()
        target = uri if isinstance(uri, str) else uri.geturl()
        self._adaos_ws_url = str(target)
        if self._adaos_ws_trace:
            try:
                self._trace(
                    f"nats ws connect start url={self._adaos_ws_url} tls=True proxy=None max_size=None tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        self._ws = await self._client.ws_connect(
            target,
            ssl=ssl_context,
            timeout=connect_timeout,
            headers=headers,
        )
        self._using_tls = True
        self._after_connect()

    def write(self, payload: bytes) -> None:
        self._pending.put_nowait(payload)

    def writelines(self, payload: List[bytes]) -> None:
        for message in payload:
            self.write(message)

    async def read(self, buffer_size: int) -> bytes:
        return await self.readline()

    async def _maybe_consume_direct_control_frame(self, data: bytes) -> bool:
        kind = _exact_nats_control_frame(data)
        if kind is None:
            return False
        nc = getattr(self, "_adaos_nc", None)
        if nc is None:
            return False
        handler_name = "_process_ping" if kind == "PING" else "_process_pong"
        handler = getattr(nc, handler_name, None)
        if not callable(handler):
            return False
        state, buf_len = _nats_parser_diag(nc)
        self._wiretap("rx", data)
        if self._adaos_ws_trace:
            self._trace(
                f"nats ws direct control rx kind={kind} parser_state={state} parser_buf_len={buf_len}"
            )
        try:
            await handler()
        except Exception as e:
            self._adaos_last_recv_error = e
            self._adaos_last_recv_error_at = time.monotonic()
            raise
        return True

    async def readline(self) -> bytes:
        while True:
            ws = self._ws
            if ws is None:
                return b""
            try:
                msg = await ws.receive()
            except Exception as e:
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        code = getattr(ws, "close_code", None)
                        reason = getattr(ws, "close_reason", None)
                        exc = None
                        try:
                            exc = ws.exception()
                        except Exception:
                            exc = None
                        self._trace(
                            "nats ws recv failed "
                            f"url={self._adaos_ws_url} err={type(e).__name__}: {e} "
                            f"code={code} reason={reason} exc={exc}"
                        )
                    except Exception:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
                if self._adaos_ws_raise_on_recv_err:
                    raise
                return b""
            try:
                self._adaos_last_rx_at = time.monotonic()
            except Exception:
                pass

            if msg.type == self._aiohttp.WSMsgType.TEXT:
                data = msg.data.encode("utf-8", errors="replace")
            elif msg.type == self._aiohttp.WSMsgType.BINARY:
                data = bytes(msg.data)
            elif msg.type in (
                self._aiohttp.WSMsgType.CLOSE,
                self._aiohttp.WSMsgType.CLOSING,
                self._aiohttp.WSMsgType.CLOSED,
            ):
                err = RuntimeError("ws closed")
                try:
                    self._adaos_last_recv_error = err
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        self._trace(
                            f"nats ws recv closed url={self._adaos_ws_url} code={getattr(ws, 'close_code', None)} reason={getattr(ws, 'close_reason', None)}"
                        )
                    except Exception:
                        pass
                if self._adaos_ws_raise_on_recv_err:
                    raise err
                return b""
            elif msg.type == self._aiohttp.WSMsgType.ERROR:
                err = ws.exception() or RuntimeError("ws error")
                try:
                    self._adaos_last_recv_error = err
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        self._trace(
                            f"nats ws recv error url={self._adaos_ws_url} err={type(err).__name__}: {err}"
                        )
                    except Exception:
                        pass
                if self._adaos_ws_raise_on_recv_err:
                    raise err
                return b""
            else:
                data = b""

            if data and await self._maybe_consume_direct_control_frame(data):
                continue
            self._wiretap("rx", data)
            if self._adaos_route_trace and data:
                try:
                    line = _route_rx_trace_line(self._adaos_ws_url, data, getattr(self, "_adaos_nc", None))
                    if line:
                        self._trace(line)
                except Exception:
                    pass
            # Parse INFO once (best-effort) to expose max_payload in diagnostics.
            try:
                if data.startswith(b"INFO "):
                    self._adaos_rx_info_at = time.monotonic()
                    line_end = data.find(b"\n")
                    if line_end < 0:
                        line_end = len(data)
                    line = data[:line_end].strip()
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    js0 = line[len(b"INFO ") :].strip()
                    obj = json.loads(js0.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict):
                        mp = obj.get("max_payload", None)
                        if isinstance(mp, int):
                            self._adaos_nats_max_payload = mp
            except Exception:
                pass
            return data

    async def drain(self) -> None:
        ws = self._ws
        if ws is None:
            return
        # send all the messages pending
        while not self._pending.empty():
            message = self._pending.get_nowait()
            if isinstance(message, memoryview):
                payload = message.tobytes()
            elif isinstance(message, (bytes, bytearray)):
                payload = bytes(message)
            else:
                payload = message
            try:
                self._adaos_last_tx_at = time.monotonic()
            except Exception:
                pass
            self._wiretap("tx", payload)
            # Extract command kind + subject (no payload logging) for later error diagnostics.
            try:
                head = payload[:256] if isinstance(payload, (bytes, bytearray)) else b""
                kind = None
                subj = None
                if isinstance(head, (bytes, bytearray)):
                    if head.startswith(b"PUB "):
                        kind = "PUB"
                    elif head.startswith(b"SUB "):
                        kind = "SUB"
                    elif head.startswith(b"CONNECT "):
                        kind = "CONNECT"
                    elif head.startswith(b"PING"):
                        kind = "PING"
                    elif head.startswith(b"PONG"):
                        kind = "PONG"
                    if kind in ("PUB", "SUB"):
                        line_end = head.find(b"\n")
                        if line_end < 0:
                            line_end = len(head)
                        parts = head[:line_end].split()
                        if len(parts) >= 2:
                            subj = parts[1].decode("utf-8", errors="replace")
                    if kind == "CONNECT":
                        self._adaos_tx_connect_at = time.monotonic()
                self._adaos_last_tx_kind = kind
                self._adaos_last_tx_subj = subj
                try:
                    self._adaos_last_tx_len = len(payload) if hasattr(payload, "__len__") else None
                except Exception:
                    self._adaos_last_tx_len = None
            except Exception:
                pass
            try:
                await ws.send_bytes(payload)
            except Exception as e:
                if self._adaos_ws_trace:
                    self._trace(f"nats ws send failed url={self._adaos_ws_url} err={type(e).__name__}: {e}")
                raise
            if self._adaos_ws_trace:
                try:
                    line = _route_tx_trace_line(
                        self._adaos_ws_url,
                        self._adaos_last_tx_subj,
                        payload,
                        self._pending,
                    )
                    if line:
                        self._trace(line)
                except Exception:
                    pass

    async def wait_closed(self) -> None:
        try:
            await self._close_task
        except Exception:
            pass
        try:
            if self._client:
                await self._client.close()
        except Exception:
            pass
        if self._adaos_ws_trace:
            try:
                ws = self._ws
                code = getattr(ws, "close_code", None) if ws is not None else None
                reason = getattr(ws, "close_reason", None) if ws is not None else None
                exc = None
                try:
                    exc = ws.exception() if ws is not None else None
                except Exception:
                    exc = None
                self._trace(f"nats ws closed url={self._adaos_ws_url} code={code} reason={reason} exc={exc}")
            except Exception:
                pass
        self._ws = self._client = None

    def close(self) -> None:
        ws = self._ws
        if ws is None:
            return
        if self._adaos_ws_trace:
            try:
                code = getattr(ws, "close_code", None)
                reason = getattr(ws, "close_reason", None)
                self._trace(f"nats ws close requested url={self._adaos_ws_url} code={code} reason={reason}")
            except Exception:
                pass
        try:
            self._close_task = asyncio.create_task(ws.close())
        except Exception:
            self._close_task = asyncio.Future()

    def at_eof(self) -> bool:
        ws = self._ws
        if ws is None:
            return True
        try:
            return bool(ws.closed)
        except Exception:
            return True

    def __bool__(self) -> bool:
        return bool(self._client)

    def _get_custom_headers(self):
        if self._ws_headers is None:
            return None
        try:
            from multidict import CIMultiDict
        except Exception:
            CIMultiDict = None  # type: ignore[assignment]
        if CIMultiDict is None:
            return self._ws_headers
        md: CIMultiDict[str] = CIMultiDict()
        for name, values in self._ws_headers.items():
            if isinstance(values, list):
                for v in values:
                    md.add(name, v)
            elif isinstance(values, str):
                md.add(name, values)
        return md

    def _after_connect(self) -> None:
        try:
            self._adaos_last_rx_at = time.monotonic()
        except Exception:
            pass
        try:
            ws = self._ws
            self._adaos_ws_proto = getattr(ws, "protocol", None) if ws is not None else None
        except Exception:
            pass
        if not self._adaos_ws_proto:
            try:
                ws = self._ws
                if ws is not None and getattr(ws, "_response", None) is not None:
                    self._adaos_ws_proto = ws._response.headers.get("Sec-WebSocket-Protocol")  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            sock = None
            try:
                ws = self._ws
                if ws is not None:
                    resp = getattr(ws, "_response", None)
                    conn = getattr(resp, "connection", None) if resp is not None else None
                    transport = getattr(conn, "transport", None) if conn is not None else None
                    if transport is not None and callable(getattr(transport, "get_extra_info", None)):
                        sock = transport.get_extra_info("socket")
            except Exception:
                sock = None
            if _set_tcp_keepalive(sock):
                if self._adaos_ws_trace:
                    try:
                        idle_s, interval_s, probes = _tcp_keepalive_params()
                        self._trace(
                            f"nats ws tcp keepalive enabled idle_s={idle_s} interval_s={interval_s} probes={probes}"
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        if self._adaos_ws_trace:
            try:
                ws = self._ws
                proto = self._adaos_ws_proto
                status = None
                try:
                    resp = getattr(ws, "_response", None)
                    status = getattr(resp, "status", None) if resp is not None else None
                except Exception:
                    status = None
                self._trace(f"nats ws connect ok url={self._adaos_ws_url} proto={proto} status={status} remote=None local=None")
            except Exception:
                pass


def install_nats_ws_transport_patch(*, verbose: bool = False) -> str:
    """
    Patch nats-py websocket transport to use `websockets`.
    Returns the active impl name ("websockets" or "aiohttp").
    """
    from nats.aio import transport as nats_transport  # type: ignore
    from nats.aio import client as nats_client  # type: ignore
    import importlib

    ws_impl = _ws_impl_from_env()

    # Patch transport class (idempotent).
    #
    # IMPORTANT: nats-py imports WebSocketTransport into `nats.aio.client` at import time:
    #   from .transport import ... WebSocketTransport
    # Therefore patching only `nats.aio.transport.WebSocketTransport` is not enough; we must also
    # patch `nats.aio.client.WebSocketTransport` to affect `Client.connect()`.
    def _is_websockets_transport(obj: Any) -> bool:
        try:
            return (
                obj is WebSocketTransportWebsockets
                or getattr(obj, "_adaos_ws_transport", None) == "websockets"
                or getattr(obj, "__name__", None) == "WebSocketTransportWebsockets"
            )
        except Exception:
            return False

    global _ORIG_NATS_WS_TRANSPORT
    global _ORIG_NATS_WS_TRANSPORT_CLIENT
    try:
        if _ORIG_NATS_WS_TRANSPORT is None:
            _ORIG_NATS_WS_TRANSPORT = getattr(nats_transport, "WebSocketTransport", None)
        if _ORIG_NATS_WS_TRANSPORT_CLIENT is None:
            _ORIG_NATS_WS_TRANSPORT_CLIENT = getattr(nats_client, "WebSocketTransport", None)
    except Exception:
        pass

    if ws_impl == "aiohttp":
        try:
            use_patched_aiohttp = (
                str(os.getenv("HUB_NATS_WS_PATCH_AIOHTTP", "0") or "0").strip() == "1"
                or os.getenv("HUB_NATS_WS_TRACE", "0") == "1"
                or _wiretap_enabled()
            )
        except Exception:
            use_patched_aiohttp = False
        # If we already patched to websockets in-process, try to recover the original
        # aiohttp transport via module reload and restore it.
        try:
            if _is_websockets_transport(_ORIG_NATS_WS_TRANSPORT) or _is_websockets_transport(_ORIG_NATS_WS_TRANSPORT_CLIENT):
                try:
                    importlib.reload(nats_transport)
                    importlib.reload(nats_client)
                except Exception:
                    pass
                _ORIG_NATS_WS_TRANSPORT = getattr(nats_transport, "WebSocketTransport", None)
                _ORIG_NATS_WS_TRANSPORT_CLIENT = getattr(nats_client, "WebSocketTransport", None)
        except Exception:
            pass
        if use_patched_aiohttp:
            try:
                setattr(WebSocketTransportAiohttp, "_adaos_ws_transport", "aiohttp")
            except Exception:
                pass
            try:
                nats_transport.WebSocketTransport = WebSocketTransportAiohttp  # type: ignore[assignment]
            except Exception:
                pass
            try:
                nats_client.WebSocketTransport = WebSocketTransportAiohttp  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            try:
                if _ORIG_NATS_WS_TRANSPORT is not None:
                    nats_transport.WebSocketTransport = _ORIG_NATS_WS_TRANSPORT  # type: ignore[assignment]
            except Exception:
                pass
            try:
                if _ORIG_NATS_WS_TRANSPORT_CLIENT is not None:
                    nats_client.WebSocketTransport = _ORIG_NATS_WS_TRANSPORT_CLIENT  # type: ignore[attr-defined]
            except Exception:
                pass
        return "aiohttp"

    try:
        current = getattr(nats_transport, "WebSocketTransport", None)
        current_client = getattr(nats_client, "WebSocketTransport", None)
        already = (
            _is_websockets_transport(current)
            and _is_websockets_transport(current_client)
        )
        if already:
            active = "websockets"
        else:
            setattr(WebSocketTransportWebsockets, "_adaos_ws_transport", "websockets")
            try:
                nats_transport.WebSocketTransport = WebSocketTransportWebsockets  # type: ignore[assignment]
            except Exception:
                pass
            try:
                nats_client.WebSocketTransport = WebSocketTransportWebsockets  # type: ignore[attr-defined]
            except Exception:
                pass
            # Be truthful: verify that `Client.connect()` will actually use our transport.
            active = "websockets" if _is_websockets_transport(getattr(nats_client, "WebSocketTransport", None)) else "aiohttp"
    except Exception:
        active = "websockets" if _is_websockets_transport(getattr(nats_client, "WebSocketTransport", None)) else "aiohttp"

    # Patch `_process_pong` to avoid InvalidStateError on cancelled/done futures (observed with nats-py 2.12.0).
    try:
        orig_process_pong = getattr(getattr(nats_client, "Client", object), "_process_pong", None)
        if callable(orig_process_pong) and not getattr(orig_process_pong, "_adaos_pong_patch", False):

            async def _process_pong_safe(self) -> None:  # type: ignore[no-redef]
                try:
                    pongs = getattr(self, "_pongs", None)
                    if isinstance(pongs, list):
                        while pongs and getattr(pongs[0], "done", lambda: False)():
                            try:
                                pongs.pop(0)
                            except Exception:
                                break
                        if pongs:
                            future = pongs.pop(0)
                            try:
                                if not future.cancelled() and not future.done():
                                    future.set_result(True)
                            except asyncio.InvalidStateError:
                                pass
                    try:
                        self._pongs_received += 1
                    except Exception:
                        pass
                finally:
                    try:
                        self._pings_outstanding = 0
                    except Exception:
                        pass

            try:
                setattr(_process_pong_safe, "_adaos_pong_patch", True)
            except Exception:
                pass
            try:
                setattr(nats_client.Client, "_process_pong", _process_pong_safe)
            except Exception:
                pass
    except Exception:
        pass

    if verbose:
        try:
            print(f"[hub-io] nats ws transport: {active}")
        except Exception:
            pass
    return active
