from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import socket
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import ParseResult

_ORIG_NATS_WS_TRANSPORT: Any | None = None
_ORIG_NATS_WS_TRANSPORT_CLIENT: Any | None = None
_TRANSPORT_LOG = logging.getLogger("adaos.hub-io.transport")


def _emit_hub_io_console_log(
    msg: str,
    *,
    level: int = logging.INFO,
    also_print: bool = False,
) -> None:
    try:
        _TRANSPORT_LOG.log(level, msg)
    except Exception:
        pass
    if not also_print:
        return
    try:
        print(f"[hub-io] {msg}")
    except Exception:
        pass


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


def _ws_max_queue_from_env() -> int | None:
    """
    websockets `max_queue` (incoming message queue size).

    NATS-over-WS can be bursty (route proxy + Yjs). A low `max_queue` can apply
    backpressure and delay delivery of Root->Hub keepalives (`PING\\r\\n`), which
    then shows up as `nats keepalive pong missing` on the proxy.

    Control:
    - HUB_NATS_WS_MAX_QUEUE:
        * unset / empty -> 64
        * <= 0          -> unlimited (None)
        * > 0           -> explicit queue size
    """
    raw = os.getenv("HUB_NATS_WS_MAX_QUEUE")
    if raw is None:
        return 64
    try:
        s = str(raw).strip()
    except Exception:
        return 64
    if not s:
        return 64
    try:
        v = int(s)
    except Exception:
        return 64
    if v <= 0:
        return None
    return v


def _ws_heartbeat_s_from_env() -> float | None:
    """
    WebSocket-level heartbeat for long-lived NATS-over-WS tunnels.

    Why:
    - Root's ws-nats-proxy sends NATS protocol keepalives, but on some networks the client->root direction
      can still go idle long enough for NAT/firewalls to drop the mapping (common symptom: WS close 1006 / EOF).
    - A WS-level ping from the client guarantees periodic outbound traffic even when the NATS layer is quiet.
    - On aiohttp transports we intentionally avoid aiohttp's builtin heartbeat because it hard-closes the socket
      with close code 1006 when a WS PONG is missed once. We use a manual no-timeout WS ping instead.

    Control:
    - HUB_NATS_WS_HEARTBEAT_S:
        * unset / empty -> disabled (None)
        * <= 0          -> disable
        * > 0           -> enable with that interval (seconds)
    """
    raw = os.getenv("HUB_NATS_WS_HEARTBEAT_S")
    if raw is None:
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v <= 0.0:
        return None
    # Be conservative: too-frequent pings can create unnecessary churn on mobile networks.
    if v < 5.0:
        v = 5.0
    return v


def _effective_ws_heartbeat_s(*, ws_impl: str | None = None) -> float | None:
    """
    Resolve WS-level heartbeat with transport-specific safety rules.

    On Windows, the `websockets` client is stable in our isolated diagnostics without
    client-originated WS PINGs, but the full AdaOS runtime can wedge after a few
    keepalive cycles when those control frames are enabled. Root already sends its own
    WS/NATS keepalives, so suppress client WS heartbeats for this transport by default.

    Operators can still force-enable the behavior for targeted diagnostics via
    `HUB_NATS_WS_HEARTBEAT_FORCE=1`.
    """
    heartbeat_s = _ws_heartbeat_s_from_env()
    if heartbeat_s is None:
        return None
    try:
        force = str(os.getenv("HUB_NATS_WS_HEARTBEAT_FORCE", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    except Exception:
        force = False
    if force:
        return heartbeat_s
    try:
        impl = (ws_impl or _ws_impl_from_env()).strip().lower()
    except Exception:
        impl = "websockets"
    if os.name == "nt" and impl == "websockets":
        return None
    return heartbeat_s


def _ws_data_heartbeat_s_from_env(*, ws_impl: str | None = None) -> float | None:
    """
    NATS protocol heartbeat for WS transports (send `PONG\\r\\n` as WS *data*).

    Why:
    - Some intermediaries terminate WS idle connections based on application data.
      WS control frames may be terminated/handled by proxies and not guarantee end-to-end hub->root traffic.
    - Root already sends NATS PINGs to elicit hub PONGs, but if that cadence is insufficient (or stops),
      this provides a conservative hub->root keepalive.

    Control:
    - HUB_NATS_WS_DATA_HEARTBEAT_S:
        * unset         -> enable conservative default (15s)
        * empty         -> disable
        * <= 0          -> disable
        * > 0           -> enable with that interval (seconds; min 5)

    Transport safety:
    - On Windows, WS transports already receive Root-originated NATS keepalives. An extra
      implicit client-originated `PONG\\r\\n` data heartbeat can interfere with Root's
      keepalive accounting by injecting standalone NATS `PONG` frames that are unrelated
      to a preceding Root `PING`.
    - Therefore when the env var is *unset*, suppress the implicit default on Windows for
      NATS-over-WS transports. Operators can still explicitly opt back in by setting
      `HUB_NATS_WS_DATA_HEARTBEAT_S` to a positive value.
    """
    raw = os.getenv("HUB_NATS_WS_DATA_HEARTBEAT_S")
    if raw is None:
        try:
            impl = (ws_impl or _ws_impl_from_env()).strip().lower()
        except Exception:
            impl = "websockets"
        if os.name == "nt" and impl in {"websockets", "aiohttp"}:
            return None
        return 15.0
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v <= 0.0:
        return None
    if v < 5.0:
        v = 5.0
    return v


def _ws_data_ping_s_from_env(*, ws_impl: str | None = None) -> float | None:
    """
    NATS protocol data ping for WS transports (send `PING\\r\\n` as WS *data*).

    This is intentionally separate from WebSocket PING control frames and from
    nats-py's ping interval task. The raw diagnostic client stays stable under
    outbound PUB load when it periodically sends NATS `PING` frames and receives
    server `PONG`s. Sending this from the transport's high-priority path keeps
    read-side liveness visible even when user traffic is mostly outbound.

    Control:
    - HUB_NATS_WS_DATA_PING_S:
        * unset         -> enable conservative default (5s) for Windows/websockets
        * empty         -> disable
        * <= 0          -> disable
        * > 0           -> enable with that interval (seconds; min 5)
    """
    raw = os.getenv("HUB_NATS_WS_DATA_PING_S")
    if raw is None:
        try:
            impl = (ws_impl or _ws_impl_from_env()).strip().lower()
        except Exception:
            impl = "websockets"
        if os.name == "nt" and impl == "websockets":
            return 5.0
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v <= 0.0:
        return None
    if v < 5.0:
        v = 5.0
    return v


def _ws_recv_timeout_s_from_env() -> float | None:
    """
    Read timeout for WS transports (max time to wait for a WS *data* message).

    Why:
    - In some failure modes the WS tunnel becomes "control-frame-only": WS pings still flow, but NATS data
      frames stop arriving. `ws.recv()` can then block forever and the hub won't reconnect promptly.
    - Root sends NATS keepalives frequently, so a moderate read timeout helps detect stalled tunnels.

    Control:
    - HUB_NATS_WS_RECV_TIMEOUT_S:
        * unset / empty -> disabled (None)
        * <= 0          -> disable
        * > 0           -> enable with that timeout (seconds; min 5)
    """
    raw = os.getenv("HUB_NATS_WS_RECV_TIMEOUT_S")
    if raw is None:
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        v = float(s)
    except Exception:
        return None
    if v <= 0.0:
        return None
    if v < 5.0:
        v = 5.0
    return v


def _effective_ws_recv_timeout_s(*, ws_impl: str | None = None) -> float | None:
    """
    Resolve WS read timeout with transport-specific safety rules.

    For `websockets` on Windows, the tunnel can legitimately spend long periods with only
    control traffic (`PING`/`PONG`) while no NATS data frames are delivered to the parser.
    Treating that as a stalled tunnel causes the hub to close a still-healthy connection
    itself, which root then reports as a clean close (code 1000).

    Keep the timeout enabled for aiohttp and for explicit diagnostics, but suppress it for
    websockets-on-Windows unless the operator force-enables it.
    """
    recv_timeout_s = _ws_recv_timeout_s_from_env()
    if recv_timeout_s is None:
        return None
    try:
        force = str(os.getenv("HUB_NATS_WS_RECV_TIMEOUT_FORCE", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    except Exception:
        force = False
    if force:
        return recv_timeout_s
    try:
        impl = (ws_impl or _ws_impl_from_env()).strip().lower()
    except Exception:
        impl = "websockets"
    if os.name == "nt" and impl == "websockets":
        return None
    return recv_timeout_s


def _ws_io_poll_s_from_env() -> float:
    """
    Max time a WS reader may hold the transport I/O lock while waiting for input.

    Why:
    - Raw/manual NATS-over-WS traffic is stable, but the hub transport can silently wedge after alternating
      inbound `MSG` and outbound `PUB` traffic on Windows.
    - The current transport lets the WS reader and writer operate from different tasks. On affected setups that
      appears to be enough to stall incoming delivery after a few cycles.
    - We serialize WS `recv` and `send` calls through one lock and poll `recv` with a short timeout so writers are
      never blocked behind an indefinitely pending read.
    """
    raw = os.getenv("HUB_NATS_WS_IO_POLL_S")
    if raw is None:
        return 0.2
    try:
        value = float(str(raw).strip())
    except Exception:
        return 0.2
    if value <= 0.0:
        return 0.2
    if value < 0.01:
        value = 0.01
    if value > 1.0:
        value = 1.0
    return value


def _ws_shared_io_enabled_from_env() -> bool:
    raw = os.getenv("HUB_NATS_WS_SHARED_IO")
    if raw is None:
        return True
    return str(raw or "").strip().lower() not in {"0", "false", "no", "off"}


def _ws_control_intercept_enabled_from_env() -> bool:
    """
    Whether the WS transport should consume exact NATS control frames itself.

    The intercept path answers Root-originated `PING\r\n` directly from the
    transport so keepalive PONGs are not delayed behind the nats-py flusher.
    Keep this toggle available while diagnosing hub-root stalls: disabling it
    lets nats-py observe and handle PING/PONG frames exactly as on TCP.
    """
    raw = os.getenv("HUB_NATS_WS_CONTROL_INTERCEPT")
    if raw is None:
        return True
    return str(raw or "").strip().lower() not in {"0", "false", "no", "off"}


def _ws_proxy_from_env() -> str | bool | None:
    """
    Control proxy handling for long-lived NATS-over-WS tunnels.

    `websockets>=15` defaults to `proxy=True`, which may pick up system proxy
    settings and route the handshake through HTTP CONNECT. Our independent
    `tools/diag_nats_ws.py` probes intentionally use that library default and
    have been stable on Windows, while forcing a direct route (`proxy=None`) can
    create one-way client->Root stalls on some networks.

    Default:
    - keep the `websockets` library default (`True` / system proxy auto-detect)

    Override with `HUB_NATS_WS_PROXY`:
        - `auto`, `system`, `default`, `1`, `true`, `yes` -> `True`
        - `none`, `off`, `0`, `false`, `no`, empty`       -> `None`
        - any other value                                 -> explicit proxy URL
    """
    raw = os.getenv("HUB_NATS_WS_PROXY")
    if raw is None:
        return True
    try:
        value = str(raw).strip()
    except Exception:
        return True
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
        if pending_q is None:
            qsize = None
        elif isinstance(pending_q, (tuple, list)):
            qsize = 0
            for q in pending_q:
                qsize += int(q.qsize())
        else:
            qsize = int(pending_q.qsize())
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


def _strip_nats_ping_control_frames(data: bytes) -> tuple[bytes, int]:
    """
    Remove NATS protocol PING control lines that appear on frame boundaries.

    Root's ws-nats-proxy can coalesce `PING\r\n` with route `MSG` frames in the
    same WebSocket message. The raw tools answer those PINGs immediately; the
    transport needs the same behavior, while still avoiding false positives for
    `PING\r\n` bytes that are part of a MSG payload.
    """
    if b"PING\r\n" not in data:
        return data, 0

    out = bytearray()
    pos = 0
    pings = 0
    total = len(data)

    while pos < total:
        line_end = data.find(b"\n", pos)
        if line_end < 0:
            out.extend(data[pos:])
            break

        line = data[pos : line_end + 1]
        stripped = line.strip()
        upper = stripped.upper()
        if upper == b"PING":
            pings += 1
            pos = line_end + 1
            continue

        parts = stripped.split()
        kind = parts[0].upper() if parts else b""
        if kind in {b"MSG", b"HMSG", b"PUB", b"HPUB"}:
            try:
                payload_len = int(parts[-1])
            except Exception:
                out.extend(data[pos:])
                break
            frame_end = line_end + 1 + payload_len + 2
            if frame_end > total:
                out.extend(data[pos:])
                break
            out.extend(data[pos:frame_end])
            pos = frame_end
            continue

        if kind in {b"INFO", b"+OK", b"-ERR", b"PONG"}:
            out.extend(line)
            pos = line_end + 1
            continue

        out.extend(data[pos:])
        break

    if pings <= 0:
        return data, 0
    return bytes(out), pings


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
    - WS-level heartbeat pings are opt-in (HUB_NATS_WS_HEARTBEAT_S). Root's ws-nats-proxy still sends NATS protocol
      keepalives; the WS heartbeat is transport-level.
    - We default to unlimited `max_size` to avoid disconnects on large frames (Yjs sync can exceed 1 MiB).
    """

    def __init__(self, ws_headers: Optional[Dict[str, List[str]]] = None):
        self._ws: Any = None
        self._pending_hi: asyncio.Queue = asyncio.Queue()
        self._pending: asyncio.Queue = asyncio.Queue()
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._io_poll_s: float = _ws_io_poll_s_from_env()
        self._pending_event: asyncio.Event = asyncio.Event()
        self._drain_event: asyncio.Event = asyncio.Event()
        self._drain_event.set()
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self._io_task: asyncio.Task | None = None
        self._direct_recv_task: asyncio.Task | None = None
        self._io_sending: bool = False
        self._pending_ws_ping: int = 0
        self._shared_io_enabled: bool = _ws_shared_io_enabled_from_env()
        self._control_intercept_enabled: bool = _ws_control_intercept_enabled_from_env()
        self._close_task: asyncio.Task | None = None
        self._data_heartbeat_task: asyncio.Task | None = None
        self._data_ping_task: asyncio.Task | None = None
        self._ws_heartbeat_task: asyncio.Task | None = None
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
        self._adaos_ws_data_heartbeat: float | None = None
        self._adaos_ws_data_ping: float | None = None
        self._adaos_tx_connect_at: float | None = None
        self._adaos_rx_info_at: float | None = None
        self._adaos_nats_max_payload: int | None = None
        self._adaos_last_recv_error: Exception | None = None
        self._adaos_last_recv_error_at: float | None = None
        self._adaos_nc: Any = None
        # Keepalive diagnostics (Root sends NATS `PING\r\n` as WS *data* every ~20s).
        self._adaos_pings_rx: int = 0
        self._adaos_pongs_tx: int = 0
        self._adaos_last_ping_rx_at: float | None = None
        self._adaos_last_pong_tx_at: float | None = None
        self._adaos_last_pong_tx_wait_s: float | None = None
        self._adaos_last_pong_tx_send_s: float | None = None
        self._adaos_ws_heartbeat_mode: str | None = None
        self._adaos_ws_pings_tx: int = 0
        self._adaos_last_ws_ping_tx_at: float | None = None
        self._adaos_last_ws_ping_tx_wait_s: float | None = None
        self._adaos_last_ws_ping_tx_send_s: float | None = None
        self._adaos_data_pings_tx: int = 0
        self._adaos_last_data_ping_tx_at: float | None = None
        self._adaos_last_data_ping_tx_wait_s: float | None = None
        self._adaos_last_data_ping_tx_send_s: float | None = None

    def _trace(self, msg: str) -> None:
        if not self._adaos_ws_trace:
            return
        _emit_hub_io_console_log(msg, level=logging.DEBUG, also_print=True)

    def _wiretap_log(self, msg: str) -> None:
        if not self._adaos_wiretap:
            return
        _emit_hub_io_console_log(msg, level=logging.DEBUG, also_print=True)

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
        if payload in (b"PONG\r\n", b"PING\r\n"):
            self._pending_hi.put_nowait(payload)
        else:
            self._pending.put_nowait(payload)
        self._drain_event.clear()
        self._pending_event.set()

    def writelines(self, payload: List[bytes]) -> None:
        for message in payload:
            self.write(message)

    async def read(self, buffer_size: int) -> bytes:
        return await self.readline()

    async def _maybe_consume_direct_control_frame(self, data: bytes) -> tuple[bytes, bool]:
        kind = _exact_nats_control_frame(data)
        nc = getattr(self, "_adaos_nc", None)
        state, buf_len = _nats_parser_diag(nc) if nc is not None else (None, None)
        if kind is None:
            # Only scan coalesced frames when nats-py's parser isn't in the middle
            # of a MSG payload. Otherwise `PING\r\n` may be application bytes.
            if not (state in (None, 1) and (buf_len in (None, 0))):
                return data, False
            cleaned, ping_count = _strip_nats_ping_control_frames(data)
            if ping_count <= 0:
                return data, False
            self._wiretap("rx", data)
            if self._adaos_ws_trace:
                self._trace(
                    "nats ws direct control rx kind=PING "
                    f"count={ping_count} mode=coalesced parser_state={state} parser_buf_len={buf_len} "
                    f"cleaned_bytes={len(cleaned)} original_bytes={len(data)}"
                )
            try:
                for _ in range(ping_count):
                    try:
                        self._adaos_pings_rx += 1
                        self._adaos_last_ping_rx_at = time.monotonic()
                    except Exception:
                        pass
                    if self._adaos_ws_trace:
                        self._trace("nats ws direct control dispatch kind=PING handler=raw_pong mode=coalesced")
                    await self._send_nats_pong(reason="ping")
            except Exception as e:
                self._adaos_last_recv_error = e
                self._adaos_last_recv_error_at = time.monotonic()
                raise
            return cleaned, True

        self._wiretap("rx", data)
        if self._adaos_ws_trace:
            self._trace(
                f"nats ws direct control rx kind={kind} parser_state={state} parser_buf_len={buf_len}"
            )
        try:
            if kind == "PING":
                try:
                    self._adaos_pings_rx += 1
                    self._adaos_last_ping_rx_at = time.monotonic()
                except Exception:
                    pass
                # Reply inline at the transport layer.
                #
                # Routing server PING through `nats-py` (`_process_ping`) queues the matching
                # PONG onto the client's normal flusher path. Under Windows/WS this can delay or
                # wedge the response long enough for Root's keepalive watchdog to declare
                # `nats keepalive pong missing`, even though the socket itself is still open.
                #
                # A direct raw `PONG\r\n` keeps the control-frame round-trip independent from
                # user traffic / flusher timing and mirrors the already-stable aiohttp path.
                if self._adaos_ws_trace:
                    self._trace("nats ws direct control dispatch kind=PING handler=raw_pong")
                await self._send_nats_pong(reason="ping")
            else:
                if nc is None:
                    return data, False
                handler = getattr(nc, "_process_pong", None)
                if not callable(handler):
                    return data, False
                if self._adaos_ws_trace:
                    self._trace("nats ws direct control dispatch kind=PONG handler=nats_client")
                await handler()
        except Exception as e:
            self._adaos_last_recv_error = e
            self._adaos_last_recv_error_at = time.monotonic()
            raise
        return b"", True

    async def _send_nats_pong(self, *, reason: str) -> None:
        ws = self._ws
        if ws is None:
            return
        payload = b"PONG\r\n"
        try:
            io_task = getattr(self, "_io_task", None)
            current_task = asyncio.current_task()
        except Exception:
            io_task = None
            current_task = None
        # When the shared IO loop is currently processing Root's NATS PING, answer it inline.
        # Do not drain normal pending PUB/SUB traffic from this path: keepalive PONG must be a
        # tiny, isolated response, otherwise it can get coupled to app traffic and Root may age
        # out the tunnel even though we logged a local "PONG sent".
        if io_task is not None and current_task is io_task:
            pong_start_at = None
            try:
                pong_start_at = time.monotonic()
                self._adaos_last_tx_at = pong_start_at
                self._adaos_last_pong_tx_at = pong_start_at
            except Exception:
                pass
            self._wiretap("tx", payload)
            try:
                self._adaos_last_tx_kind = "PONG"
                self._adaos_last_tx_subj = None
                self._adaos_last_tx_len = len(payload)
            except Exception:
                pass
            lock_wait_s = None
            send_s = None
            lock_wait_start = time.monotonic()
            async with self._send_lock:
                lock_acquired_at = time.monotonic()
                try:
                    lock_wait_s = lock_acquired_at - lock_wait_start
                except Exception:
                    lock_wait_s = None
                await ws.send(payload)
                send_done_at = time.monotonic()
                try:
                    send_s = send_done_at - lock_acquired_at
                except Exception:
                    send_s = None
            try:
                self._adaos_pongs_tx += 1
                self._adaos_last_pong_tx_wait_s = lock_wait_s
                self._adaos_last_pong_tx_send_s = send_s
            except Exception:
                pass
            try:
                if self._adaos_ws_trace:
                    n = int(getattr(self, "_adaos_pongs_tx", 0) or 0)
                    lw_ms = round(float(lock_wait_s) * 1000.0, 3) if isinstance(lock_wait_s, (int, float)) else None
                    sd_ms = round(float(send_s) * 1000.0, 3) if isinstance(send_s, (int, float)) else None
                    self._trace(
                        f"nats ws direct control tx kind=PONG reason={reason} wait_ms={lw_ms} send_ms={sd_ms} n={n}"
                    )
            except Exception:
                pass
            return

        # From tasks outside the shared IO loop, enqueue through the regular prioritized path
        # so websocket writes remain serialized with normal transport sends.
        if io_task is not None:
            self.write(payload)
            await self.drain()
            try:
                self._adaos_pongs_tx += 1
                self._adaos_last_pong_tx_wait_s = 0.0
                self._adaos_last_pong_tx_send_s = 0.0
                self._adaos_last_pong_tx_at = time.monotonic()
            except Exception:
                pass
            try:
                if self._adaos_ws_trace:
                    n = int(getattr(self, "_adaos_pongs_tx", 0) or 0)
                    self._trace(
                        f"nats ws direct control tx kind=PONG reason={reason} wait_ms=0.0 send_ms=0.0 n={n}"
                    )
            except Exception:
                pass
            return
        pong_start_at = None
        try:
            pong_start_at = time.monotonic()
            self._adaos_last_tx_at = pong_start_at
            self._adaos_last_pong_tx_at = pong_start_at
        except Exception:
            pass
        self._wiretap("tx", payload)
        try:
            self._adaos_last_tx_kind = "PONG"
            self._adaos_last_tx_subj = None
            self._adaos_last_tx_len = len(payload)
        except Exception:
            pass
        lock_wait_s = None
        send_s = None
        lock_wait_start = time.monotonic()
        async with self._send_lock:
            lock_acquired_at = time.monotonic()
            try:
                lock_wait_s = lock_acquired_at - lock_wait_start
            except Exception:
                lock_wait_s = None
            await ws.send(payload)
            send_done_at = time.monotonic()
            try:
                send_s = send_done_at - lock_acquired_at
            except Exception:
                send_s = None
        try:
            self._adaos_pongs_tx += 1
            self._adaos_last_pong_tx_wait_s = lock_wait_s
            self._adaos_last_pong_tx_send_s = send_s
        except Exception:
            pass
        # When WS trace is enabled, always log PONG sends. This is low-volume in normal operation and helps
        # correlate hub-side behavior with root-side `nats keepalive pong missing`.
        try:
            if self._adaos_ws_trace:
                n = int(getattr(self, "_adaos_pongs_tx", 0) or 0)
                lw_ms = round(float(lock_wait_s) * 1000.0, 3) if isinstance(lock_wait_s, (int, float)) else None
                sd_ms = round(float(send_s) * 1000.0, 3) if isinstance(send_s, (int, float)) else None
                self._trace(
                    f"nats ws direct control tx kind=PONG reason={reason} wait_ms={lw_ms} send_ms={sd_ms} n={n}"
                )
        except Exception:
            pass

    async def _send_nats_ping(self, *, reason: str) -> None:
        ws = self._ws
        if ws is None:
            return
        payload = b"PING\r\n"
        try:
            io_task = getattr(self, "_io_task", None)
            current_task = asyncio.current_task()
        except Exception:
            io_task = None
            current_task = None
        if io_task is not None:
            self.write(payload)
            if current_task is io_task:
                await self._direct_drain()
            else:
                await self.drain()
            try:
                self._adaos_data_pings_tx += 1
                self._adaos_last_data_ping_tx_wait_s = 0.0
                self._adaos_last_data_ping_tx_send_s = 0.0
                self._adaos_last_data_ping_tx_at = time.monotonic()
            except Exception:
                pass
            try:
                if self._adaos_ws_trace:
                    n = int(getattr(self, "_adaos_data_pings_tx", 0) or 0)
                    self._trace(
                        f"nats ws data heartbeat tx kind=PING reason={reason} wait_ms=0.0 send_ms=0.0 n={n}"
                    )
            except Exception:
                pass
            return

        ping_start_at = None
        try:
            ping_start_at = time.monotonic()
            self._adaos_last_tx_at = ping_start_at
            self._adaos_last_data_ping_tx_at = ping_start_at
        except Exception:
            pass
        self._wiretap("tx", payload)
        try:
            self._adaos_last_tx_kind = "PING"
            self._adaos_last_tx_subj = None
            self._adaos_last_tx_len = len(payload)
        except Exception:
            pass
        lock_wait_s = None
        send_s = None
        lock_wait_start = time.monotonic()
        async with self._send_lock:
            lock_acquired_at = time.monotonic()
            try:
                lock_wait_s = lock_acquired_at - lock_wait_start
            except Exception:
                lock_wait_s = None
            await ws.send(payload)
            send_done_at = time.monotonic()
            try:
                send_s = send_done_at - lock_acquired_at
            except Exception:
                send_s = None
        try:
            self._adaos_data_pings_tx += 1
            self._adaos_last_data_ping_tx_wait_s = lock_wait_s
            self._adaos_last_data_ping_tx_send_s = send_s
        except Exception:
            pass
        try:
            if self._adaos_ws_trace:
                n = int(getattr(self, "_adaos_data_pings_tx", 0) or 0)
                lw_ms = round(float(lock_wait_s) * 1000.0, 3) if isinstance(lock_wait_s, (int, float)) else None
                sd_ms = round(float(send_s) * 1000.0, 3) if isinstance(send_s, (int, float)) else None
                self._trace(
                    f"nats ws data heartbeat tx kind=PING reason={reason} wait_ms={lw_ms} send_ms={sd_ms} n={n}"
                )
        except Exception:
            pass

    def _ensure_io_task(self) -> None:
        if not self._shared_io_enabled:
            return
        try:
            if self._io_task is not None and not self._io_task.done():
                return
            if self._ws is None:
                return
            if self._direct_recv_task is not None and not self._direct_recv_task.done():
                return
            while not self._recv_queue.empty():
                self._recv_queue.get_nowait()
        except Exception:
            return
        try:
            self._io_task = asyncio.create_task(self._io_loop(self._ws), name="adaos-nats-ws-io")
        except Exception:
            self._io_task = None

    async def _direct_readline(self) -> bytes:
        idle_started_at = time.monotonic()
        while True:
            ws = self._ws
            if ws is None:
                return b""
            try:
                recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                direct_recv_task = getattr(self, "_direct_recv_task", None)
                if not isinstance(direct_recv_task, asyncio.Task) or direct_recv_task.done():
                    direct_recv_task = asyncio.create_task(ws.recv(), name="adaos-nats-ws-direct-recv")
                    self._direct_recv_task = direct_recv_task
                wait_s = None
                if isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0:
                    remaining_s = float(recv_timeout_s) - (time.monotonic() - idle_started_at)
                    if remaining_s <= 0.0:
                        raise asyncio.TimeoutError()
                    wait_s = remaining_s
                if isinstance(wait_s, (int, float)):
                    raw = await asyncio.wait_for(asyncio.shield(direct_recv_task), timeout=wait_s)
                else:
                    raw = await asyncio.shield(direct_recv_task)
            except asyncio.TimeoutError as e:
                try:
                    recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                except Exception:
                    recv_timeout_s = None
                if not (isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0):
                    continue
                if (time.monotonic() - idle_started_at) < float(recv_timeout_s):
                    continue
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        self._trace(
                            f"nats ws recv timeout url={self._adaos_ws_url} timeout_s={getattr(self, '_adaos_ws_recv_timeout', None)}"
                        )
                    except Exception:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
                return b""
            except Exception as e:
                try:
                    if getattr(self, "_direct_recv_task", None) is direct_recv_task:
                        self._direct_recv_task = None
                except Exception:
                    pass
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                try:
                    ws_state = getattr(ws, "state", None)
                    _emit_hub_io_console_log(
                        "nats ws recv failed "
                        f"url={self._adaos_ws_url} err={type(e).__name__}: {e} "
                        f"code={getattr(e, 'code', None)} reason={getattr(e, 'reason', None)} "
                        f"rcvd={getattr(e, 'rcvd', None)} sent={getattr(e, 'sent', None)} state={ws_state}",
                        level=logging.WARNING,
                    )
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
                    raise e
                return b""
            try:
                if getattr(self, "_direct_recv_task", None) is direct_recv_task:
                    self._direct_recv_task = None
            except Exception:
                pass
            try:
                self._adaos_last_rx_at = time.monotonic()
            except Exception:
                pass
            idle_started_at = time.monotonic()
            if isinstance(raw, str):
                data = raw.encode("utf-8")
            elif isinstance(raw, (bytes, bytearray, memoryview)):
                data = bytes(raw)
            else:
                data = b""
            if data and self._control_intercept_enabled:
                data, consumed_control = await self._maybe_consume_direct_control_frame(data)
                if consumed_control and not data:
                    continue
            self._wiretap("rx", data)
            if self._adaos_route_trace and data:
                try:
                    line = _route_rx_trace_line(self._adaos_ws_url, data, getattr(self, "_adaos_nc", None))
                    if line:
                        self._trace(line)
                except Exception:
                    pass
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

    async def _direct_drain(self) -> None:
        ws = self._ws
        if ws is None:
            return
        while not self._pending_hi.empty() or not self._pending.empty():
            if not self._pending_hi.empty():
                message = self._pending_hi.get_nowait()
            else:
                message = self._pending.get_nowait()
            await self._io_send_payload("bytes", message)

    def _pending_empty(self) -> bool:
        try:
            return self._pending_ws_ping <= 0 and self._pending_hi.empty() and self._pending.empty()
        except Exception:
            return False

    def _dequeue_pending_nowait(self) -> tuple[str, Any] | None:
        try:
            if self._pending_ws_ping > 0:
                self._pending_ws_ping -= 1
                return ("ws_ping", None)
        except Exception:
            pass
        try:
            if not self._pending_hi.empty():
                return ("bytes", self._pending_hi.get_nowait())
        except Exception:
            pass
        try:
            if not self._pending.empty():
                return ("bytes", self._pending.get_nowait())
        except Exception:
            pass
        return None

    async def _io_send_payload(self, kind: str, payload: Any) -> None:
        ws = self._ws
        if ws is None:
            return
        self._io_sending = True
        try:
            if kind == "ws_ping":
                ping_started_at = time.monotonic()
                try:
                    self._adaos_last_tx_at = ping_started_at
                    self._adaos_last_tx_kind = "WS.PING"
                    self._adaos_last_tx_subj = None
                    self._adaos_last_tx_len = 0
                except Exception:
                    pass
                await ws.ping()
                ping_done_at = time.monotonic()
                try:
                    self._adaos_ws_pings_tx += 1
                    self._adaos_last_ws_ping_tx_at = ping_started_at
                    self._adaos_last_ws_ping_tx_wait_s = 0.0
                    self._adaos_last_ws_ping_tx_send_s = ping_done_at - ping_started_at
                except Exception:
                    pass
                try:
                    if self._adaos_ws_trace:
                        n = int(getattr(self, "_adaos_ws_pings_tx", 0) or 0)
                        sd_ms = round((ping_done_at - ping_started_at) * 1000.0, 3)
                        self._trace(
                            f"nats ws heartbeat tx kind=PING mode={self._adaos_ws_heartbeat_mode} wait_ms=0.0 send_ms={sd_ms} n={n}"
                        )
                except Exception:
                    pass
                return

            if isinstance(payload, memoryview):
                data = payload.tobytes()
            elif isinstance(payload, (bytes, bytearray)):
                data = bytes(payload)
            else:
                data = payload
            try:
                self._adaos_last_tx_at = time.monotonic()
            except Exception:
                pass
            self._wiretap("tx", data)
            try:
                head = data[:256] if isinstance(data, (bytes, bytearray)) else b""
                kind0 = None
                subj0 = None
                if isinstance(head, (bytes, bytearray)):
                    if head.startswith(b"PUB "):
                        kind0 = "PUB"
                    elif head.startswith(b"SUB "):
                        kind0 = "SUB"
                    elif head.startswith(b"CONNECT "):
                        kind0 = "CONNECT"
                    elif head.startswith(b"PING"):
                        kind0 = "PING"
                    elif head.startswith(b"PONG"):
                        kind0 = "PONG"
                    if kind0 in ("PUB", "SUB"):
                        line_end = head.find(b"\n")
                        if line_end < 0:
                            line_end = len(head)
                        parts = head[:line_end].split()
                        if len(parts) >= 2:
                            subj0 = parts[1].decode("utf-8", errors="replace")
                    if kind0 == "CONNECT":
                        self._adaos_tx_connect_at = time.monotonic()
                self._adaos_last_tx_kind = kind0
                self._adaos_last_tx_subj = subj0
                self._adaos_last_tx_len = len(data) if hasattr(data, "__len__") else None
            except Exception:
                pass
            await ws.send(data)
            if self._adaos_ws_trace:
                try:
                    line = _route_tx_trace_line(
                        self._adaos_ws_url,
                        self._adaos_last_tx_subj,
                        data,
                        (self._pending_hi, self._pending),
                    )
                    if line:
                        self._trace(line)
                except Exception:
                    pass
        finally:
            self._io_sending = False
            if self._pending_empty():
                self._drain_event.set()

    async def _io_loop(self, ws0: Any) -> None:
        try:
            while True:
                try:
                    ws1 = self._ws
                    if ws1 is None or ws1 is not ws0 or self.at_eof():
                        return
                except Exception:
                    return

                pending_item = self._dequeue_pending_nowait()
                if pending_item is not None:
                    kind, payload = pending_item
                    try:
                        await self._io_send_payload(kind, payload)
                    except Exception as e:
                        try:
                            self._adaos_last_recv_error = e
                            self._adaos_last_recv_error_at = time.monotonic()
                        except Exception:
                            pass
                        try:
                            await self._recv_queue.put(e)
                        except Exception:
                            pass
                        return
                    continue

                try:
                    # Match the independently stable tools/diag_nats_ws.py loop:
                    # write pending frames, then do one short recv attempt. Avoid a
                    # long-lived recv task that gets cancelled by outbound traffic;
                    # on Windows/proxy/websockets that pattern can make local sends
                    # appear successful while Root stops seeing client frames.
                    raw = await asyncio.wait_for(ws0.recv(), timeout=self._io_poll_s)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    try:
                        self._adaos_last_recv_error = e
                        self._adaos_last_recv_error_at = time.monotonic()
                    except Exception:
                        pass
                    try:
                        await self._recv_queue.put(e)
                    except Exception:
                        pass
                    return

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

                if data and self._control_intercept_enabled:
                    try:
                        data, consumed_control = await self._maybe_consume_direct_control_frame(data)
                    except Exception as e:
                        try:
                            self._adaos_last_recv_error = e
                            self._adaos_last_recv_error_at = time.monotonic()
                        except Exception:
                            pass
                        try:
                            await self._recv_queue.put(e)
                        except Exception:
                            pass
                        return
                    if consumed_control and not data:
                        continue

                try:
                    await self._recv_queue.put(data)
                except Exception:
                    return
        finally:
            try:
                await self._recv_queue.put(None)
            except Exception:
                pass

    async def readline(self) -> bytes:
        self._ensure_io_task()
        if self._io_task is None:
            return await self._direct_readline()
        idle_started_at = time.monotonic()
        while True:
            if self._ws is None and self._recv_queue.empty():
                return b""
            try:
                recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                poll_s = float(getattr(self, "_io_poll_s", 0.2) or 0.2)
                wait_s = poll_s
                if isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0:
                    remaining_s = float(recv_timeout_s) - (time.monotonic() - idle_started_at)
                    if remaining_s <= 0.0:
                        raise asyncio.TimeoutError()
                    wait_s = min(wait_s, remaining_s)
                raw = await asyncio.wait_for(self._recv_queue.get(), timeout=wait_s)
            except asyncio.TimeoutError as e:
                try:
                    recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                except Exception:
                    recv_timeout_s = None
                if not (isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0):
                    continue
                if (time.monotonic() - idle_started_at) < float(recv_timeout_s):
                    continue
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        self._trace(
                            f"nats ws recv timeout url={self._adaos_ws_url} timeout_s={getattr(self, '_adaos_ws_recv_timeout', None)}"
                        )
                    except Exception:
                        pass
                try:
                    ws = self._ws
                    if ws is not None:
                        await ws.close()
                except Exception:
                    pass
                self._ws = None
                return b""
            if raw is None:
                return b""
            if isinstance(raw, Exception):
                e = raw
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                try:
                    ws = self._ws
                    ws_state = getattr(ws, "state", None)
                    _emit_hub_io_console_log(
                        "nats ws recv failed "
                        f"url={self._adaos_ws_url} err={type(e).__name__}: {e} "
                        f"code={getattr(e, 'code', None)} reason={getattr(e, 'reason', None)} "
                        f"rcvd={getattr(e, 'rcvd', None)} sent={getattr(e, 'sent', None)} state={ws_state}",
                        level=logging.WARNING,
                    )
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        ws = self._ws
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
                    raise e
                return b""
            try:
                self._adaos_last_rx_at = time.monotonic()
            except Exception:
                pass
            idle_started_at = time.monotonic()
            if isinstance(raw, str):
                data = raw.encode("utf-8")
            elif isinstance(raw, (bytes, bytearray, memoryview)):
                data = bytes(raw)
            else:
                data = b""
            if data and self._control_intercept_enabled:
                data, consumed_control = await self._maybe_consume_direct_control_frame(data)
                if consumed_control and not data:
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
        self._ensure_io_task()
        if self._io_task is None:
            await self._direct_drain()
            return
        if self._ws is None:
            return
        poll_s = float(getattr(self, "_io_poll_s", 0.2) or 0.2)
        while True:
            if self._ws is None or self.at_eof():
                return
            io_task = self._io_task
            if io_task is None:
                await self._direct_drain()
                return
            if io_task.done():
                err = getattr(self, "_adaos_last_recv_error", None)
                if isinstance(err, BaseException):
                    raise err
                return
            if self._pending_empty() and not self._io_sending:
                self._drain_event.set()
                return
            self._pending_event.set()
            try:
                await asyncio.wait_for(self._drain_event.wait(), timeout=poll_s)
            except asyncio.TimeoutError:
                continue

    async def wait_closed(self) -> None:
        try:
            if self._close_task is not None:
                await asyncio.wait_for(self._close_task, timeout=1.0)
        except BaseException:
            pass
        try:
            if self._io_task is not None:
                await asyncio.wait_for(self._io_task, timeout=1.0)
        except BaseException:
            pass
        try:
            if self._direct_recv_task is not None:
                await asyncio.wait_for(self._direct_recv_task, timeout=1.0)
        except BaseException:
            pass
        try:
            ws = self._ws
            if ws is not None and callable(getattr(ws, "wait_closed", None)):
                await asyncio.wait_for(ws.wait_closed(), timeout=1.0)
        except BaseException:
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
        try:
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._data_ping_task is not None and not self._data_ping_task.done():
                self._data_ping_task.cancel()
        except Exception:
            pass
        self._data_ping_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        self._direct_recv_task = None
        self._io_task = None
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
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._data_ping_task is not None and not self._data_ping_task.done():
                self._data_ping_task.cancel()
        except Exception:
            pass
        self._data_ping_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        try:
            if self._io_task is not None and not self._io_task.done():
                self._io_task.cancel()
        except Exception:
            pass
        try:
            if self._direct_recv_task is not None and not self._direct_recv_task.done():
                self._direct_recv_task.cancel()
        except Exception:
            pass
        try:
            self._recv_queue.put_nowait(None)
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

    def _start_ws_heartbeat_task(self) -> None:
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        try:
            heartbeat_s = getattr(self, "_adaos_ws_heartbeat", None)
        except Exception:
            heartbeat_s = None
        if heartbeat_s is None:
            return
        ws0 = self._ws

        async def _ws_heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(float(heartbeat_s))
                try:
                    ws1 = self._ws
                    if ws1 is None or ws1 is not ws0 or self.at_eof():
                        return
                except Exception:
                    return
                try:
                    self._ensure_io_task()
                except Exception:
                    return
                try:
                    self._pending_ws_ping += 1
                    self._drain_event.clear()
                    self._pending_event.set()
                except Exception:
                    return

        try:
            self._ws_heartbeat_task = asyncio.create_task(
                _ws_heartbeat_loop(), name="adaos-nats-ws-heartbeat"
            )
        except Exception:
            self._ws_heartbeat_task = None

    def _start_data_ping_task(self) -> None:
        try:
            if self._data_ping_task is not None and not self._data_ping_task.done():
                self._data_ping_task.cancel()
        except Exception:
            pass
        self._data_ping_task = None
        try:
            data_ping_s = getattr(self, "_adaos_ws_data_ping", None)
        except Exception:
            data_ping_s = None
        if data_ping_s is None:
            return
        ws0 = self._ws

        async def _data_ping_loop() -> None:
            interval_s = float(data_ping_s)
            while True:
                await asyncio.sleep(interval_s)
                try:
                    ws1 = self._ws
                    if ws1 is None or ws1 is not ws0 or self.at_eof():
                        return
                except Exception:
                    return
                try:
                    now = time.monotonic()
                    last_rx_at = getattr(self, "_adaos_last_rx_at", None)
                    if isinstance(last_rx_at, (int, float)) and (now - float(last_rx_at)) < interval_s:
                        continue
                except Exception:
                    pass
                try:
                    await self._send_nats_ping(reason="data_ping")
                except Exception:
                    return

        try:
            self._data_ping_task = asyncio.create_task(
                _data_ping_loop(), name="adaos-nats-ws-data-ping"
            )
        except Exception:
            self._data_ping_task = None

    async def _connect_impl(self, target: str, *, ssl_context: ssl.SSLContext | None, connect_timeout: int) -> None:
        try:
            import websockets  # type: ignore
        except Exception as e:
            raise RuntimeError(f"websockets is required for NATS-over-WS: {type(e).__name__}: {e}") from e

        headers = _ws_headers_to_tuples(self._ws_headers)
        max_size = _ws_max_size_from_env()
        heartbeat_s = _effective_ws_heartbeat_s(ws_impl="websockets")
        data_heartbeat_s = _ws_data_heartbeat_s_from_env(ws_impl="websockets")
        data_ping_s = _ws_data_ping_s_from_env(ws_impl="websockets")
        recv_timeout_s = _effective_ws_recv_timeout_s(ws_impl="websockets")
        try:
            setattr(self, "_adaos_ws_heartbeat", heartbeat_s)
        except Exception:
            pass
        try:
            self._adaos_ws_heartbeat_mode = "manual_no_timeout" if heartbeat_s is not None else None
        except Exception:
            pass
        try:
            self._adaos_ws_data_heartbeat = data_heartbeat_s
        except Exception:
            pass
        try:
            self._adaos_ws_data_ping = data_ping_s
        except Exception:
            pass
        try:
            self._adaos_ws_recv_timeout = recv_timeout_s
        except Exception:
            pass
        # Use the same manual no-timeout heartbeat strategy as the aiohttp transport.
        # This keeps client->root traffic observable in diagnostics and avoids relying on
        # the websocket library's internal keepalive task on Windows/Proactor.
        ping_interval = None
        ping_timeout: float | None = None
        max_queue = _ws_max_queue_from_env()
        ws_kwargs: dict[str, Any] = {
            "subprotocols": ["nats"],
            "open_timeout": connect_timeout,
            "ping_interval": ping_interval,
            "ping_timeout": ping_timeout,
            "close_timeout": 2.0,
            "max_size": max_size,
            "max_queue": max_queue,
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
                    f"proxy={ws_kwargs.get('proxy')} max_size={max_size} "
                    f"max_queue={max_queue} "
                    f"heartbeat_s={heartbeat_s} heartbeat_mode={self._adaos_ws_heartbeat_mode} "
                    f"data_ping_s={data_ping_s} "
                    f"ping_interval={ping_interval} ping_timeout={ping_timeout} recv_timeout_s={recv_timeout_s} "
                    f"tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        try:
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._data_ping_task is not None and not self._data_ping_task.done():
                self._data_ping_task.cancel()
        except Exception:
            pass
        self._data_ping_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        try:
            if self._io_task is not None and not self._io_task.done():
                self._io_task.cancel()
        except Exception:
            pass
        try:
            if self._direct_recv_task is not None and not self._direct_recv_task.done():
                self._direct_recv_task.cancel()
        except Exception:
            pass
        self._direct_recv_task = None
        self._io_task = None
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
            while not self._recv_queue.empty():
                self._recv_queue.get_nowait()
        except Exception:
            pass
        try:
            self._pending_event.clear()
        except Exception:
            pass
        if self._pending_empty():
            self._drain_event.set()
        else:
            self._drain_event.clear()
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
        self._io_task = None
        # `websockets` doesn't allow concurrent `recv()` calls. Start the shared reader task
        # as soon as the socket is ready so we don't fall back to ad-hoc direct reads first.
        self._ensure_io_task()
        self._start_ws_heartbeat_task()
        self._start_data_ping_task()

        # Optional NATS-data heartbeat (send PONG) to keep end-to-end hub->root traffic visible.
        if data_heartbeat_s is not None:
            ws0 = self._ws

            async def _data_heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(float(data_heartbeat_s))
                    try:
                        ws1 = self._ws
                        if ws1 is None or ws1 is not ws0 or self.at_eof():
                            return
                    except Exception:
                        return
                    try:
                        now = time.monotonic()
                        last_tx_at = getattr(self, "_adaos_last_tx_at", None)
                        if isinstance(last_tx_at, (int, float)) and (now - float(last_tx_at)) < float(data_heartbeat_s):
                            continue
                    except Exception:
                        pass
                    try:
                        await self._send_nats_pong(reason="data_hb")
                    except Exception:
                        return

            try:
                self._data_heartbeat_task = asyncio.create_task(
                    _data_heartbeat_loop(), name="adaos-nats-ws-data-heartbeat"
                )
            except Exception:
                self._data_heartbeat_task = None
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
        self._pending_hi: asyncio.Queue = asyncio.Queue()
        self._pending: asyncio.Queue = asyncio.Queue()
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._io_poll_s: float = _ws_io_poll_s_from_env()
        self._close_task = asyncio.Future()
        self._data_heartbeat_task: asyncio.Task | None = None
        self._ws_heartbeat_task: asyncio.Task | None = None
        self._using_tls: Optional[bool] = None
        self._ws_headers: Optional[Dict[str, List[str]]] = ws_headers
        self._control_intercept_enabled: bool = _ws_control_intercept_enabled_from_env()
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
        self._adaos_ws_data_heartbeat: float | None = None
        self._adaos_tx_connect_at: float | None = None
        self._adaos_rx_info_at: float | None = None
        self._adaos_nats_max_payload: int | None = None
        self._adaos_last_recv_error: Exception | None = None
        self._adaos_last_recv_error_at: float | None = None
        self._adaos_nc: Any = None
        # Keepalive diagnostics (Root sends NATS `PING\r\n` as WS *data* every ~20s).
        self._adaos_pings_rx: int = 0
        self._adaos_pongs_tx: int = 0
        self._adaos_last_ping_rx_at: float | None = None
        self._adaos_last_pong_tx_at: float | None = None
        self._adaos_last_pong_tx_wait_s: float | None = None
        self._adaos_last_pong_tx_send_s: float | None = None
        self._adaos_ws_heartbeat_mode: str | None = None
        self._adaos_ws_pings_tx: int = 0
        self._adaos_last_ws_ping_tx_at: float | None = None
        self._adaos_last_ws_ping_tx_wait_s: float | None = None
        self._adaos_last_ws_ping_tx_send_s: float | None = None

    def _trace(self, msg: str) -> None:
        if not self._adaos_ws_trace:
            return
        _emit_hub_io_console_log(msg, level=logging.DEBUG, also_print=True)

    def _wiretap_log(self, msg: str) -> None:
        if not self._adaos_wiretap:
            return
        _emit_hub_io_console_log(msg, level=logging.DEBUG, also_print=True)

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
        heartbeat_s = _effective_ws_heartbeat_s(ws_impl="aiohttp")
        data_heartbeat_s = _ws_data_heartbeat_s_from_env(ws_impl="aiohttp")
        recv_timeout_s = _effective_ws_recv_timeout_s(ws_impl="aiohttp")
        try:
            setattr(self, "_adaos_ws_heartbeat", heartbeat_s)
        except Exception:
            pass
        try:
            self._adaos_ws_heartbeat_mode = "manual_no_timeout" if heartbeat_s is not None else None
        except Exception:
            pass
        try:
            self._adaos_ws_data_heartbeat = data_heartbeat_s
        except Exception:
            pass
        try:
            self._adaos_ws_recv_timeout = recv_timeout_s
        except Exception:
            pass
        try:
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        self._adaos_ws_url = uri.geturl()
        if self._adaos_ws_trace:
            try:
                self._trace(
                    f"nats ws connect start url={self._adaos_ws_url} tls=False proxy=None max_size=None "
                    f"heartbeat_s={heartbeat_s} heartbeat_mode={self._adaos_ws_heartbeat_mode} recv_timeout_s={recv_timeout_s} tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        kwargs: dict[str, Any] = {"timeout": connect_timeout, "headers": headers}
        self._ws = await self._client.ws_connect(uri.geturl(), **kwargs)
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
        heartbeat_s = _effective_ws_heartbeat_s(ws_impl="aiohttp")
        data_heartbeat_s = _ws_data_heartbeat_s_from_env(ws_impl="aiohttp")
        recv_timeout_s = _effective_ws_recv_timeout_s(ws_impl="aiohttp")
        try:
            setattr(self, "_adaos_ws_heartbeat", heartbeat_s)
        except Exception:
            pass
        try:
            self._adaos_ws_heartbeat_mode = "manual_no_timeout" if heartbeat_s is not None else None
        except Exception:
            pass
        try:
            self._adaos_ws_data_heartbeat = data_heartbeat_s
        except Exception:
            pass
        try:
            self._adaos_ws_recv_timeout = recv_timeout_s
        except Exception:
            pass
        try:
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
        self._adaos_ws_url = str(target)
        if self._adaos_ws_trace:
            try:
                self._trace(
                    f"nats ws connect start url={self._adaos_ws_url} tls=True proxy=None max_size=None "
                    f"heartbeat_s={heartbeat_s} heartbeat_mode={self._adaos_ws_heartbeat_mode} recv_timeout_s={recv_timeout_s} tag={self._adaos_ws_tag}"
                )
            except Exception:
                pass
        kwargs_tls: dict[str, Any] = {
            "ssl": ssl_context,
            "timeout": connect_timeout,
            "headers": headers,
        }
        self._ws = await self._client.ws_connect(target, **kwargs_tls)
        self._using_tls = True
        self._after_connect()

    def write(self, payload: bytes) -> None:
        if payload == b"PONG\r\n":
            self._pending_hi.put_nowait(payload)
        else:
            self._pending.put_nowait(payload)

    def writelines(self, payload: List[bytes]) -> None:
        for message in payload:
            self.write(message)

    async def read(self, buffer_size: int) -> bytes:
        return await self.readline()

    async def _maybe_consume_direct_control_frame(self, data: bytes) -> tuple[bytes, bool]:
        kind = _exact_nats_control_frame(data)
        nc = getattr(self, "_adaos_nc", None)
        state, buf_len = _nats_parser_diag(nc) if nc is not None else (None, None)
        if kind is None:
            if not (state in (None, 1) and (buf_len in (None, 0))):
                return data, False
            cleaned, ping_count = _strip_nats_ping_control_frames(data)
            if ping_count <= 0:
                return data, False
            self._wiretap("rx", data)
            if self._adaos_ws_trace:
                self._trace(
                    "nats ws direct control rx kind=PING "
                    f"count={ping_count} mode=coalesced parser_state={state} parser_buf_len={buf_len} "
                    f"cleaned_bytes={len(cleaned)} original_bytes={len(data)}"
                )
            try:
                for _ in range(ping_count):
                    try:
                        self._adaos_pings_rx += 1
                        self._adaos_last_ping_rx_at = time.monotonic()
                    except Exception:
                        pass
                    if self._adaos_ws_trace:
                        self._trace("nats ws direct control dispatch kind=PING handler=raw_pong mode=coalesced")
                    await self._send_nats_pong(reason="ping")
            except Exception as e:
                self._adaos_last_recv_error = e
                self._adaos_last_recv_error_at = time.monotonic()
                raise
            return cleaned, True

        self._wiretap("rx", data)
        if self._adaos_ws_trace:
            self._trace(
                f"nats ws direct control rx kind={kind} parser_state={state} parser_buf_len={buf_len}"
            )
        try:
            if kind == "PING":
                try:
                    self._adaos_pings_rx += 1
                    self._adaos_last_ping_rx_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    self._trace("nats ws direct control dispatch kind=PING handler=raw_pong")
                await self._send_nats_pong(reason="ping")
            else:
                if nc is None:
                    return data, False
                handler = getattr(nc, "_process_pong", None)
                if not callable(handler):
                    return data, False
                if self._adaos_ws_trace:
                    self._trace("nats ws direct control dispatch kind=PONG handler=nats_client")
                await handler()
        except Exception as e:
            self._adaos_last_recv_error = e
            self._adaos_last_recv_error_at = time.monotonic()
            raise
        return b"", True

    async def _send_nats_pong(self, *, reason: str) -> None:
        ws = self._ws
        if ws is None:
            return
        payload = b"PONG\r\n"
        pong_start_at = None
        try:
            pong_start_at = time.monotonic()
            self._adaos_last_tx_at = pong_start_at
            self._adaos_last_pong_tx_at = pong_start_at
        except Exception:
            pass
        self._wiretap("tx", payload)
        try:
            self._adaos_last_tx_kind = "PONG"
            self._adaos_last_tx_subj = None
            self._adaos_last_tx_len = len(payload)
        except Exception:
            pass
        lock_wait_s = None
        send_s = None
        lock_wait_start = time.monotonic()
        async with self._send_lock:
            lock_acquired_at = time.monotonic()
            try:
                lock_wait_s = lock_acquired_at - lock_wait_start
            except Exception:
                lock_wait_s = None
            await ws.send_bytes(payload)
            send_done_at = time.monotonic()
            try:
                send_s = send_done_at - lock_acquired_at
            except Exception:
                send_s = None
        try:
            self._adaos_pongs_tx += 1
            self._adaos_last_pong_tx_wait_s = lock_wait_s
            self._adaos_last_pong_tx_send_s = send_s
        except Exception:
            pass
        try:
            if self._adaos_ws_trace:
                n = int(getattr(self, "_adaos_pongs_tx", 0) or 0)
                lw_ms = round(float(lock_wait_s) * 1000.0, 3) if isinstance(lock_wait_s, (int, float)) else None
                sd_ms = round(float(send_s) * 1000.0, 3) if isinstance(send_s, (int, float)) else None
                self._trace(
                    f"nats ws direct control tx kind=PONG reason={reason} wait_ms={lw_ms} send_ms={sd_ms} n={n}"
                )
        except Exception:
            pass

    async def readline(self) -> bytes:
        idle_started_at = time.monotonic()
        while True:
            ws = self._ws
            if ws is None:
                return b""
            try:
                recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                poll_s = float(getattr(self, "_io_poll_s", 0.2) or 0.2)
                wait_s = poll_s
                if isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0:
                    remaining_s = float(recv_timeout_s) - (time.monotonic() - idle_started_at)
                    if remaining_s <= 0.0:
                        raise asyncio.TimeoutError()
                    wait_s = min(wait_s, remaining_s)
                async with self._send_lock:
                    msg = await asyncio.wait_for(ws.receive(), timeout=wait_s)
            except asyncio.TimeoutError as e:
                try:
                    recv_timeout_s = getattr(self, "_adaos_ws_recv_timeout", None)
                except Exception:
                    recv_timeout_s = None
                if not (isinstance(recv_timeout_s, (int, float)) and float(recv_timeout_s) > 0.0):
                    continue
                if (time.monotonic() - idle_started_at) < float(recv_timeout_s):
                    continue
                try:
                    self._adaos_last_recv_error = e
                    self._adaos_last_recv_error_at = time.monotonic()
                except Exception:
                    pass
                if self._adaos_ws_trace:
                    try:
                        self._trace(
                            f"nats ws recv timeout url={self._adaos_ws_url} timeout_s={getattr(self, '_adaos_ws_recv_timeout', None)}"
                        )
                    except Exception:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
                return b""
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
            idle_started_at = time.monotonic()

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

            if data and self._control_intercept_enabled:
                data, consumed_control = await self._maybe_consume_direct_control_frame(data)
                if consumed_control and not data:
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
        while not self._pending_hi.empty() or not self._pending.empty():
            if not self._pending_hi.empty():
                message = self._pending_hi.get_nowait()
            else:
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
                async with self._send_lock:
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
                        (self._pending_hi, self._pending),
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
        try:
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
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
            if self._data_heartbeat_task is not None and not self._data_heartbeat_task.done():
                self._data_heartbeat_task.cancel()
        except Exception:
            pass
        self._data_heartbeat_task = None
        try:
            if self._ws_heartbeat_task is not None and not self._ws_heartbeat_task.done():
                self._ws_heartbeat_task.cancel()
        except Exception:
            pass
        self._ws_heartbeat_task = None
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
        try:
            heartbeat_s = getattr(self, "_adaos_ws_heartbeat", None)
        except Exception:
            heartbeat_s = None
        if heartbeat_s is not None:
            ws0 = self._ws

            async def _ws_heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(float(heartbeat_s))
                    try:
                        ws1 = self._ws
                        if ws1 is None or ws1 is not ws0 or self.at_eof():
                            return
                    except Exception:
                        return
                    ping_started_at = time.monotonic()
                    try:
                        self._adaos_last_tx_at = ping_started_at
                        self._adaos_last_tx_kind = "WS.PING"
                        self._adaos_last_tx_subj = None
                        self._adaos_last_tx_len = 0
                    except Exception:
                        pass
                    lock_wait_s = None
                    send_s = None
                    lock_wait_started_at = time.monotonic()
                    try:
                        async with self._send_lock:
                            lock_acquired_at = time.monotonic()
                            try:
                                lock_wait_s = lock_acquired_at - lock_wait_started_at
                            except Exception:
                                lock_wait_s = None
                            await ws1.ping()
                            ping_sent_at = time.monotonic()
                            try:
                                send_s = ping_sent_at - lock_acquired_at
                            except Exception:
                                send_s = None
                    except Exception:
                        return
                    try:
                        self._adaos_ws_pings_tx += 1
                        self._adaos_last_ws_ping_tx_at = ping_started_at
                        self._adaos_last_ws_ping_tx_wait_s = lock_wait_s
                        self._adaos_last_ws_ping_tx_send_s = send_s
                    except Exception:
                        pass
                    if self._adaos_ws_trace:
                        try:
                            n = int(getattr(self, "_adaos_ws_pings_tx", 0) or 0)
                            lw_ms = (
                                round(float(lock_wait_s) * 1000.0, 3)
                                if isinstance(lock_wait_s, (int, float))
                                else None
                            )
                            sd_ms = (
                                round(float(send_s) * 1000.0, 3)
                                if isinstance(send_s, (int, float))
                                else None
                            )
                            self._trace(
                                f"nats ws heartbeat tx kind=PING mode={self._adaos_ws_heartbeat_mode} wait_ms={lw_ms} send_ms={sd_ms} n={n}"
                            )
                        except Exception:
                            pass

            try:
                self._ws_heartbeat_task = asyncio.create_task(
                    _ws_heartbeat_loop(), name="adaos-nats-ws-heartbeat"
                )
            except Exception:
                self._ws_heartbeat_task = None
        # Optional NATS-data heartbeat (send PONG) to keep end-to-end hub->root traffic visible.
        try:
            data_heartbeat_s = self._adaos_ws_data_heartbeat
        except Exception:
            data_heartbeat_s = None
        if data_heartbeat_s is not None:
            ws0 = self._ws

            async def _data_heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(float(data_heartbeat_s))
                    try:
                        ws1 = self._ws
                        if ws1 is None or ws1 is not ws0 or self.at_eof():
                            return
                    except Exception:
                        return
                    try:
                        now = time.monotonic()
                        last_tx_at = getattr(self, "_adaos_last_tx_at", None)
                        if isinstance(last_tx_at, (int, float)) and (now - float(last_tx_at)) < float(data_heartbeat_s):
                            continue
                    except Exception:
                        pass
                    try:
                        await self._send_nats_pong(reason="data_hb")
                    except Exception:
                        return

            try:
                self._data_heartbeat_task = asyncio.create_task(
                    _data_heartbeat_loop(), name="adaos-nats-ws-data-heartbeat"
                )
            except Exception:
                self._data_heartbeat_task = None
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
            raw_patch_aiohttp = os.getenv("HUB_NATS_WS_PATCH_AIOHTTP")
            if raw_patch_aiohttp is None:
                use_patched_aiohttp = True
            else:
                use_patched_aiohttp = str(raw_patch_aiohttp or "").strip() != "0"
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
