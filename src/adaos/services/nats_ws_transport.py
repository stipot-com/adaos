from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import ParseResult


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

    async def readline(self) -> bytes:
        ws = self._ws
        if ws is None:
            return b""
        try:
            raw = await ws.recv()
        except Exception:
            return b""
        try:
            self._adaos_last_rx_at = time.monotonic()
        except Exception:
            pass
        if isinstance(raw, str):
            return raw.encode("utf-8")
        if isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
        else:
            data = b""
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
            await ws.send(payload)

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
        self._ws = None

    def close(self) -> None:
        ws = self._ws
        if ws is None:
            return
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
        }
        if ssl_context is not None:
            ws_kwargs["ssl"] = ssl_context

        self._adaos_ws_url = str(target)
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
                # Older websockets uses `extra_headers=...`.
                if headers:
                    self._ws = await websockets.connect(target, extra_headers=headers, **ws_kwargs)
                else:
                    self._ws = await websockets.connect(target, **ws_kwargs)
        except Exception:
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


def install_nats_ws_transport_patch(*, verbose: bool = False) -> str:
    """
    Patch nats-py websocket transport to use `websockets`.
    Returns the active impl name ("websockets" or "aiohttp").
    """
    ws_impl = _ws_impl_from_env()
    if ws_impl == "aiohttp":
        return "aiohttp"

    from nats.aio import transport as nats_transport  # type: ignore
    from nats.aio import client as nats_client  # type: ignore

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
