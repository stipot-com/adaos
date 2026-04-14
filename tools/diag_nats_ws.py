import argparse
import asyncio
import json
import os
import random
import string
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


NATS_PING = b"PING\r\n"
NATS_PONG = b"PONG\r\n"


def _load_node_yaml(path: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed; cannot parse node.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected node.yaml type: {type(data).__name__}")
    return data


def _load_runtime_nats(path: str) -> dict[str, Any]:
    runtime_path = Path(path).expanduser().resolve().parent / "state" / "node_runtime.json"
    try:
        payload = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    nats = payload.get("nats")
    return dict(nats) if isinstance(nats, dict) else {}


def _rand_id(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _coerce_ws_url(base: str) -> str:
    base = (base or "").strip()
    if not base:
        return ""
    if base.startswith("ws://") or base.startswith("wss://"):
        return base
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    return base


def _extract_urls(node: dict[str, Any], runtime_nats: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    legacy_nats = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    base = runtime_nats.get("ws_url") or legacy_nats.get("ws_url") or legacy_nats.get("ws") or legacy_nats.get("url") or ""
    base = _coerce_ws_url(str(base or ""))
    if base:
        urls.append(base)
    # Fallbacks used in hub code
    urls.append("wss://api.inimatic.com/nats")
    urls.append("wss://nats.inimatic.com/nats")
    # Optional alternates
    extra = os.getenv("NATS_WS_URL_ALT", "")
    for it in [x.strip() for x in extra.split(",") if x.strip()]:
        if it.startswith("ws"):
            urls.append(it)
    # Dedup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


@dataclass
class RunResult:
    url: str
    ok: bool
    seconds: float
    close_code: Optional[int]
    close_reason: Optional[str]
    ws_pings_rx: int
    ws_pongs_rx: int
    client_ws_pings_tx: int
    client_ws_pongs_rx: int
    client_ws_pong_timeouts: int
    nats_pings_rx: int
    nats_pongs_tx: int
    client_pings_tx: int
    client_pongs_rx: int
    client_pong_timeouts: int
    pubs_tx: int
    info_rx: int
    errors: list[str]


def _add_conn_tag(url: str, tag: str) -> str:
    if not tag:
        return url
    try:
        u = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        if "adaos_conn" not in qs:
            qs["adaos_conn"] = [tag]
        query = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, query, u.fragment))
    except Exception:
        return url


async def _run_one(
    url: str,
    *,
    ws_lib: str,
    user: str,
    password: str,
    duration_s: float,
    trace: bool,
    conn_tag: str,
    client_ping_every_s: float,
    client_pong_timeout_s: float,
    ws_ping_every_s: float,
    ws_pong_timeout_s: float,
    subs: list[str],
    pub_subject: str,
    pub_every_s: float,
    pub_payload: str,
    pub_split: bool,
    send_text: bool,
) -> RunResult:
    lib = str(ws_lib or "aiohttp").strip().lower()
    if lib in ("auto", ""):
        try:
            lib = "websockets" if os.name == "nt" else "aiohttp"
        except Exception:
            lib = "aiohttp"
    if lib in ("websockets", "ws"):
        return await _run_one_websockets(
            url,
            user=user,
            password=password,
            duration_s=duration_s,
            trace=trace,
            conn_tag=conn_tag,
            client_ping_every_s=client_ping_every_s,
            client_pong_timeout_s=client_pong_timeout_s,
            ws_ping_every_s=ws_ping_every_s,
            ws_pong_timeout_s=ws_pong_timeout_s,
            subs=subs,
            pub_subject=pub_subject,
            pub_every_s=pub_every_s,
            pub_payload=pub_payload,
            pub_split=pub_split,
            send_text=send_text,
        )
    return await _run_one_aiohttp(
        url,
        user=user,
        password=password,
        duration_s=duration_s,
        trace=trace,
        conn_tag=conn_tag,
        client_ping_every_s=client_ping_every_s,
        client_pong_timeout_s=client_pong_timeout_s,
        ws_ping_every_s=ws_ping_every_s,
        ws_pong_timeout_s=ws_pong_timeout_s,
        subs=subs,
        pub_subject=pub_subject,
        pub_every_s=pub_every_s,
        pub_payload=pub_payload,
        pub_split=pub_split,
        send_text=send_text,
    )


async def _run_one_aiohttp(
    url: str,
    *,
    user: str,
    password: str,
    duration_s: float,
    trace: bool,
    conn_tag: str,
    client_ping_every_s: float,
    client_pong_timeout_s: float,
    ws_ping_every_s: float,
    ws_pong_timeout_s: float,
    subs: list[str],
    pub_subject: str,
    pub_every_s: float,
    pub_payload: str,
    pub_split: bool,
    send_text: bool,
) -> RunResult:
    started_at = time.monotonic()
    errors: list[str] = []
    ws_pings_rx = 0
    ws_pongs_rx = 0
    client_ws_pings_tx = 0
    client_ws_pongs_rx = 0
    client_ws_pong_timeouts = 0
    nats_pings_rx = 0
    nats_pongs_tx = 0
    client_pings_tx = 0
    client_pongs_rx = 0
    client_pong_timeouts = 0
    pubs_tx = 0
    info_rx = 0

    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"Sec-WebSocket-Protocol": "nats"}
    if conn_tag:
        headers["X-AdaOS-Nats-Conn"] = conn_tag

    close_code: Optional[int] = None
    close_reason: Optional[str] = None

    async with aiohttp.ClientSession(timeout=timeout) as sess:
        try:
            url = _add_conn_tag(url, conn_tag)
            async with sess.ws_connect(
                url,
                headers=headers,
                autoping=False,
                autoclose=False,
                heartbeat=None,
                max_msg_size=0,
            ) as ws:
                # Send CONNECT as early as possible; the proxy buffers INFO if it arrives first.
                connect_obj = {
                    "verbose": False,
                    "pedantic": False,
                    "lang": "diag",
                    "version": "0.0",
                    "protocol": 1,
                    "echo": True,
                    "name": f"diag-{_rand_id()}",
                    "user": user,
                    "pass": password,
                }
                connect_bytes = b"CONNECT " + json.dumps(connect_obj).encode("utf-8") + b"\r\n"
                if send_text:
                    await ws.send_str(connect_bytes.decode("utf-8"))
                else:
                    await ws.send_bytes(connect_bytes)
                try:
                    sid = 1
                    for subj in subs or []:
                        s = str(subj or "").strip()
                        if not s:
                            continue
                        sub_bytes = f"SUB {s} {sid}\r\n".encode("utf-8")
                        if send_text:
                            await ws.send_str(sub_bytes.decode("utf-8"))
                        else:
                            await ws.send_bytes(sub_bytes)
                        sid += 1
                    if trace and subs:
                        print(f"[diag] SUB ok count={len([s for s in subs if str(s or '').strip()])}")
                except Exception as e:
                    errors.append(f"send SUB failed: {type(e).__name__}: {e}")

                deadline = time.monotonic() + duration_s
                pending_pongs: list[float] = []
                next_ping_at: Optional[float] = None
                if isinstance(client_ping_every_s, (int, float)) and client_ping_every_s > 0:
                    next_ping_at = time.monotonic() + float(client_ping_every_s)
                next_pub_at: Optional[float] = None
                pub_subject_s = str(pub_subject or "").strip()
                pub_payload_b = str(pub_payload or "").encode("utf-8")
                if pub_subject_s and isinstance(pub_every_s, (int, float)) and pub_every_s > 0:
                    next_pub_at = time.monotonic() + float(pub_every_s)
                pending_ws_pongs: list[float] = []
                next_ws_ping_at: Optional[float] = None
                if isinstance(ws_ping_every_s, (int, float)) and ws_ping_every_s > 0:
                    next_ws_ping_at = time.monotonic() + float(ws_ping_every_s)
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if next_pub_at is not None and now >= next_pub_at:
                        try:
                            header = f"PUB {pub_subject_s} {len(pub_payload_b)}\r\n".encode("utf-8")
                            if pub_split:
                                if send_text:
                                    await ws.send_str(header.decode("utf-8"))
                                    await ws.send_str(pub_payload_b.decode("utf-8"))
                                    await ws.send_str("\r\n")
                                else:
                                    await ws.send_bytes(header)
                                    await ws.send_bytes(pub_payload_b)
                                    await ws.send_bytes(b"\r\n")
                            else:
                                cmd = header + pub_payload_b + b"\r\n"
                                if send_text:
                                    await ws.send_str(cmd.decode("utf-8"))
                                else:
                                    await ws.send_bytes(cmd)
                            pubs_tx += 1
                            if trace and pubs_tx <= 3:
                                print(f"[diag] PUB tx #{pubs_tx} subj={pub_subject_s} len={len(pub_payload_b)} split={int(pub_split)}")
                        except Exception as e:
                            errors.append(f"send PUB failed: {type(e).__name__}: {e}")
                            break
                        next_pub_at = now + float(pub_every_s)
                    if next_ws_ping_at is not None and now >= next_ws_ping_at:
                        try:
                            await ws.ping(b"")
                            client_ws_pings_tx += 1
                            pending_ws_pongs.append(now + max(0.1, float(ws_pong_timeout_s)))
                            if trace:
                                print(f"[diag] ws PING tx #{client_ws_pings_tx}")
                        except Exception as e:
                            errors.append(f"send WS PING failed: {type(e).__name__}: {e}")
                            break
                        next_ws_ping_at = now + float(ws_ping_every_s)
                    if next_ping_at is not None and now >= next_ping_at:
                        try:
                            if send_text:
                                await ws.send_str(NATS_PING.decode("utf-8"))
                            else:
                                await ws.send_bytes(NATS_PING)
                            client_pings_tx += 1
                            pending_pongs.append(now + max(0.1, float(client_pong_timeout_s)))
                            if trace:
                                print(f"[diag] nats PING tx #{client_pings_tx}")
                        except Exception as e:
                            errors.append(f"send client PING failed: {type(e).__name__}: {e}")
                            break
                        next_ping_at = now + float(client_ping_every_s)
                    if pending_ws_pongs and now >= pending_ws_pongs[0]:
                        client_ws_pong_timeouts += 1
                        errors.append(f"client WS PONG timeout after PING #{client_ws_pings_tx} (timeouts={client_ws_pong_timeouts})")
                        break
                    if pending_pongs and now >= pending_pongs[0]:
                        client_pong_timeouts += 1
                        errors.append(f"client PONG timeout after PING #{client_pings_tx} (timeouts={client_pong_timeouts})")
                        break
                    left = max(deadline - time.monotonic(), 0.1)
                    try:
                        msg = await ws.receive(timeout=min(left, 0.5))
                    except asyncio.TimeoutError:
                        continue

                    if msg.type == aiohttp.WSMsgType.PING:
                        ws_pings_rx += 1
                        payload = msg.data if isinstance(msg.data, (bytes, bytearray)) else b""
                        await ws.pong(payload)
                        if trace:
                            print(f"[diag] ws ping rx len={len(payload)} -> pong")
                        continue

                    if msg.type == aiohttp.WSMsgType.PONG:
                        ws_pongs_rx += 1
                        if pending_ws_pongs:
                            pending_ws_pongs = pending_ws_pongs[1:]
                            client_ws_pongs_rx += 1
                        if trace:
                            payload = msg.data if isinstance(msg.data, (bytes, bytearray)) else b""
                            print(f"[diag] ws pong rx len={len(payload)}")
                        continue

                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                        try:
                            close_code = ws.close_code
                        except Exception:
                            close_code = None
                        try:
                            close_reason = getattr(ws, "close_reason", None)
                        except Exception:
                            close_reason = None
                        try:
                            exc = ws.exception()
                            if exc is not None:
                                errors.append(f"ws exception: {type(exc).__name__}: {exc}")
                        except Exception:
                            pass
                        break

                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        try:
                            close_code = ws.close_code
                        except Exception:
                            close_code = None
                        try:
                            exc = ws.exception()
                            if exc is not None:
                                errors.append(f"ws exception: {type(exc).__name__}: {exc}")
                        except Exception:
                            pass
                        break

                    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        data = msg.data
                        if isinstance(data, str):
                            raw = data.encode("utf-8")
                        else:
                            raw = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
                        if b"-ERR" in raw:
                            try:
                                line = raw.split(b"\r\n", 1)[0][:240]
                            except Exception:
                                line = raw[:240]
                            errors.append(f"server -ERR: {line.decode('utf-8', errors='replace')}")
                            if trace:
                                print(f"[diag] {errors[-1]}")
                        if raw.startswith(b"INFO "):
                            info_rx += 1
                            if trace:
                                line = raw.split(b"\r\n", 1)[0][:200]
                                print(f"[diag] nats INFO: {line!r}")
                        # Count protocol pongs from the server (responses to our client PINGs)
                        if b"PONG\r\n" in raw:
                            got = raw.count(NATS_PONG)
                            client_pongs_rx += got
                            if pending_pongs and got > 0:
                                pending_pongs = pending_pongs[got:]
                            if trace and got:
                                print(f"[diag] nats PONG rx +{got} (total={client_pongs_rx}) pending={len(pending_pongs)}")
                        # Count protocol pings and respond
                        if b"PING\r\n" in raw:
                            nats_pings_rx += raw.count(NATS_PING)
                            try:
                                if send_text:
                                    await ws.send_str(NATS_PONG.decode("utf-8"))
                                else:
                                    await ws.send_bytes(NATS_PONG)
                                nats_pongs_tx += 1
                            except Exception as e:
                                errors.append(f"send PONG failed: {type(e).__name__}: {e}")
                                break
                        continue

                    errors.append(f"unexpected msg type: {msg.type}")
        except Exception as e:
            errors.append(f"connect/run failed: {type(e).__name__}: {e}")

    seconds = time.monotonic() - started_at
    ok = not errors and seconds >= duration_s * 0.9
    return RunResult(
        url=url,
        ok=ok,
        seconds=seconds,
        close_code=close_code,
        close_reason=close_reason,
        ws_pings_rx=ws_pings_rx,
        ws_pongs_rx=ws_pongs_rx,
        client_ws_pings_tx=client_ws_pings_tx,
        client_ws_pongs_rx=client_ws_pongs_rx,
        client_ws_pong_timeouts=client_ws_pong_timeouts,
        nats_pings_rx=nats_pings_rx,
        nats_pongs_tx=nats_pongs_tx,
        client_pings_tx=client_pings_tx,
        client_pongs_rx=client_pongs_rx,
        client_pong_timeouts=client_pong_timeouts,
        pubs_tx=pubs_tx,
        info_rx=info_rx,
        errors=errors,
    )


async def _run_one_websockets(
    url: str,
    *,
    user: str,
    password: str,
    duration_s: float,
    trace: bool,
    conn_tag: str,
    client_ping_every_s: float,
    client_pong_timeout_s: float,
    ws_ping_every_s: float,
    ws_pong_timeout_s: float,
    subs: list[str],
    pub_subject: str,
    pub_every_s: float,
    pub_payload: str,
    pub_split: bool,
    send_text: bool,
) -> RunResult:
    started_at = time.monotonic()
    errors: list[str] = []
    ws_pings_rx = 0
    ws_pongs_rx = 0
    client_ws_pings_tx = 0
    client_ws_pongs_rx = 0
    client_ws_pong_timeouts = 0
    nats_pings_rx = 0
    nats_pongs_tx = 0
    client_pings_tx = 0
    client_pongs_rx = 0
    client_pong_timeouts = 0
    pubs_tx = 0
    info_rx = 0

    close_code: Optional[int] = None
    close_reason: Optional[str] = None

    # websockets manages Sec-WebSocket-Protocol via `subprotocols=`.
    headers: dict[str, str] = {}
    if conn_tag:
        headers["X-AdaOS-Nats-Conn"] = conn_tag

    # Import lazily so this tool still works in aiohttp-only environments.
    try:
        import websockets  # type: ignore
    except Exception as e:
        errors.append(f"websockets import failed: {type(e).__name__}: {e}")
        seconds = time.monotonic() - started_at
        return RunResult(
            url=url,
            ok=False,
            seconds=seconds,
            close_code=None,
            close_reason=None,
            ws_pings_rx=0,
            ws_pongs_rx=0,
            client_ws_pings_tx=0,
            client_ws_pongs_rx=0,
            client_ws_pong_timeouts=0,
            nats_pings_rx=0,
            nats_pongs_tx=0,
            client_pings_tx=0,
            client_pongs_rx=0,
            client_pong_timeouts=0,
            pubs_tx=0,
            info_rx=0,
            errors=errors,
        )

    ws = None
    try:
        url = _add_conn_tag(url, conn_tag)
        # Disable websockets keepalive pings; the proxy should provide its own keepalives.
        connect_kwargs: dict[str, Any] = {
            "subprotocols": ["nats"],
            "open_timeout": 10,
            "ping_interval": None,
            "ping_timeout": None,
            "close_timeout": 2.0,
            "max_size": None,
            "compression": None,
        }
        if headers:
            try:
                ws = await websockets.connect(url, additional_headers=headers, **connect_kwargs)
            except TypeError:
                ws = await websockets.connect(url, extra_headers=headers, **connect_kwargs)
        else:
            ws = await websockets.connect(url, **connect_kwargs)

        # Send CONNECT as early as possible; the proxy buffers INFO if it arrives first.
        connect_obj = {
            "verbose": False,
            "pedantic": False,
            "lang": "diag",
            "version": "0.0",
            "protocol": 1,
            "echo": True,
            "name": f"diag-{_rand_id()}",
            "user": user,
            "pass": password,
        }
        connect_bytes = b"CONNECT " + json.dumps(connect_obj).encode("utf-8") + b"\r\n"
        if send_text:
            await ws.send(connect_bytes.decode("utf-8"))
        else:
            await ws.send(connect_bytes)

        try:
            sid = 1
            for subj in subs or []:
                s = str(subj or "").strip()
                if not s:
                    continue
                sub_bytes = f"SUB {s} {sid}\r\n".encode("utf-8")
                if send_text:
                    await ws.send(sub_bytes.decode("utf-8"))
                else:
                    await ws.send(sub_bytes)
                sid += 1
            if trace and subs:
                print(f"[diag] SUB ok count={len([s for s in subs if str(s or '').strip()])}")
        except Exception as e:
            errors.append(f"send SUB failed: {type(e).__name__}: {e}")

        deadline = time.monotonic() + duration_s
        pending_pongs: list[float] = []
        next_ping_at: Optional[float] = None
        if isinstance(client_ping_every_s, (int, float)) and client_ping_every_s > 0:
            next_ping_at = time.monotonic() + float(client_ping_every_s)
        next_pub_at: Optional[float] = None
        pub_subject_s = str(pub_subject or "").strip()
        pub_payload_b = str(pub_payload or "").encode("utf-8")
        if pub_subject_s and isinstance(pub_every_s, (int, float)) and pub_every_s > 0:
            next_pub_at = time.monotonic() + float(pub_every_s)

        pending_ws_pongs: list[tuple[float, asyncio.Task]] = []
        next_ws_ping_at: Optional[float] = None
        if isinstance(ws_ping_every_s, (int, float)) and ws_ping_every_s > 0:
            next_ws_ping_at = time.monotonic() + float(ws_ping_every_s)

        while time.monotonic() < deadline:
            now = time.monotonic()
            if next_pub_at is not None and now >= next_pub_at:
                try:
                    header = f"PUB {pub_subject_s} {len(pub_payload_b)}\r\n".encode("utf-8")
                    if pub_split:
                        if send_text:
                            await ws.send(header.decode("utf-8"))
                            await ws.send(pub_payload_b.decode("utf-8"))
                            await ws.send("\r\n")
                        else:
                            await ws.send(header)
                            await ws.send(pub_payload_b)
                            await ws.send(b"\r\n")
                    else:
                        cmd = header + pub_payload_b + b"\r\n"
                        if send_text:
                            await ws.send(cmd.decode("utf-8"))
                        else:
                            await ws.send(cmd)
                    pubs_tx += 1
                    if trace and pubs_tx <= 3:
                        print(f"[diag] PUB tx #{pubs_tx} subj={pub_subject_s} len={len(pub_payload_b)} split={int(pub_split)}")
                except Exception as e:
                    errors.append(f"send PUB failed: {type(e).__name__}: {e}")
                    break
                next_pub_at = now + float(pub_every_s)

            if next_ws_ping_at is not None and now >= next_ws_ping_at:
                try:
                    # Track WS-level keepalive (best-effort). `websockets` resolves the awaitable when a PONG is received.
                    t = asyncio.create_task(ws.ping(b""))  # type: ignore[attr-defined]
                    pending_ws_pongs.append((now + max(0.1, float(ws_pong_timeout_s)), t))
                    client_ws_pings_tx += 1
                    if trace:
                        print(f"[diag] ws PING tx #{client_ws_pings_tx}")
                except Exception as e:
                    errors.append(f"send WS PING failed: {type(e).__name__}: {e}")
                    break
                next_ws_ping_at = now + float(ws_ping_every_s)

            # Drain completed WS ping tasks.
            if pending_ws_pongs:
                done = []
                for i, (dl, t) in enumerate(pending_ws_pongs):
                    if t.done():
                        done.append(i)
                        try:
                            _ = t.result()
                        except Exception:
                            pass
                        client_ws_pongs_rx += 1
                if done:
                    pending_ws_pongs = [x for j, x in enumerate(pending_ws_pongs) if j not in set(done)]

            # Time out WS ping tasks.
            if pending_ws_pongs and now >= pending_ws_pongs[0][0]:
                client_ws_pong_timeouts += 1
                errors.append(f"client WS PONG timeout after PING #{client_ws_pings_tx} (timeouts={client_ws_pong_timeouts})")
                break

            if next_ping_at is not None and now >= next_ping_at:
                try:
                    if send_text:
                        await ws.send(NATS_PING.decode("utf-8"))
                    else:
                        await ws.send(NATS_PING)
                    client_pings_tx += 1
                    pending_pongs.append(now + max(0.1, float(client_pong_timeout_s)))
                    if trace:
                        print(f"[diag] nats PING tx #{client_pings_tx}")
                except Exception as e:
                    errors.append(f"send client PING failed: {type(e).__name__}: {e}")
                    break
                next_ping_at = now + float(client_ping_every_s)

            if pending_pongs and now >= pending_pongs[0]:
                client_pong_timeouts += 1
                errors.append(f"client PONG timeout after PING #{client_pings_tx} (timeouts={client_pong_timeouts})")
                break

            left = max(deadline - time.monotonic(), 0.1)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=min(left, 0.5))
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                # Connection closed / broken
                try:
                    close_code = getattr(ws, "close_code", None)
                except Exception:
                    close_code = None
                try:
                    close_reason = getattr(ws, "close_reason", None)
                except Exception:
                    close_reason = None
                errors.append(f"ws recv failed: {type(e).__name__}: {e}")
                break

            if isinstance(msg, str):
                raw = msg.encode("utf-8")
            else:
                raw = bytes(msg) if isinstance(msg, (bytes, bytearray)) else b""

            if b"-ERR" in raw:
                try:
                    line = raw.split(b"\r\n", 1)[0][:240]
                except Exception:
                    line = raw[:240]
                errors.append(f"server -ERR: {line.decode('utf-8', errors='replace')}")
                if trace:
                    print(f"[diag] {errors[-1]}")

            if raw.startswith(b"INFO "):
                info_rx += 1
                if trace:
                    line = raw.split(b"\r\n", 1)[0][:200]
                    print(f"[diag] nats INFO: {line!r}")

            # Count protocol pongs from the server (responses to our client PINGs)
            if b"PONG\r\n" in raw:
                got = raw.count(NATS_PONG)
                client_pongs_rx += got
                if pending_pongs and got > 0:
                    pending_pongs = pending_pongs[got:]
                if trace and got:
                    print(f"[diag] nats PONG rx +{got} (total={client_pongs_rx}) pending={len(pending_pongs)}")

            # Count protocol pings and respond
            if b"PING\r\n" in raw:
                nats_pings_rx += raw.count(NATS_PING)
                try:
                    if send_text:
                        await ws.send(NATS_PONG.decode("utf-8"))
                    else:
                        await ws.send(NATS_PONG)
                    nats_pongs_tx += 1
                except Exception as e:
                    errors.append(f"send PONG failed: {type(e).__name__}: {e}")
                    break

    except Exception as e:
        errors.append(f"connect/run failed: {type(e).__name__}: {e}")
    finally:
        try:
            if ws is not None:
                close_code = close_code if close_code is not None else getattr(ws, "close_code", None)
                close_reason = close_reason if close_reason is not None else getattr(ws, "close_reason", None)
        except Exception:
            pass
        try:
            if ws is not None:
                await ws.close()
        except Exception:
            pass

    seconds = time.monotonic() - started_at
    ok = not errors and seconds >= duration_s * 0.9
    return RunResult(
        url=url,
        ok=ok,
        seconds=seconds,
        close_code=close_code,
        close_reason=close_reason,
        ws_pings_rx=ws_pings_rx,
        ws_pongs_rx=ws_pongs_rx,
        client_ws_pings_tx=client_ws_pings_tx,
        client_ws_pongs_rx=client_ws_pongs_rx,
        client_ws_pong_timeouts=client_ws_pong_timeouts,
        nats_pings_rx=nats_pings_rx,
        nats_pongs_tx=nats_pongs_tx,
        client_pings_tx=client_pings_tx,
        client_pongs_rx=client_pongs_rx,
        client_pong_timeouts=client_pong_timeouts,
        pubs_tx=pubs_tx,
        info_rx=info_rx,
        errors=errors,
    )


async def _amain() -> int:
    ap = argparse.ArgumentParser(description="Diagnose NATS-over-WebSocket connection to Root (ping/pong + NATS INFO/PING).")
    ap.add_argument("--node-yaml", default=".adaos/node.yaml", help="Path to bootstrap node.yaml (runtime NATS is read from sibling state/node_runtime.json)")
    ap.add_argument("--url", default="", help="Override WS URL (e.g. wss://api.inimatic.com/nats)")
    ap.add_argument(
        "--ws-lib",
        default="auto",
        choices=["auto", "aiohttp", "websockets"],
        help="WS client library (default: auto; on Windows prefers websockets).",
    )
    ap.add_argument("--tag", default="", help="Connection tag for Root logs (sets X-AdaOS-Nats-Conn + ?adaos_conn=...)")
    ap.add_argument("--duration", type=float, default=200.0, help="Run duration per URL in seconds (default: 200)")
    ap.add_argument("--ws-ping-every", type=float, default=0.0, help="Send WebSocket PING every N seconds (0=off)")
    ap.add_argument("--ws-pong-timeout", type=float, default=2.0, help="Timeout waiting for WS PONG after WS PING (seconds)")
    ap.add_argument("--client-ping-every", type=float, default=0.0, help="Send NATS protocol PING every N seconds (0=off)")
    ap.add_argument("--client-pong-timeout", type=float, default=2.0, help="Timeout waiting for PONG after client PING (seconds)")
    ap.add_argument("--sub", action="append", default=[], help="Subscribe to a subject (repeatable). Example: --sub route.to_hub.*")
    ap.add_argument("--pub-subject", default="", help="Publish subject (NATS PUB). Example: route.to_browser.<key> (empty=off)")
    ap.add_argument("--pub-every", type=float, default=0.0, help="Publish a message every N seconds (0=off)")
    ap.add_argument("--pub-payload", default="{}", help="Publish payload (UTF-8 string, default: {})")
    ap.add_argument("--pub-split", action="store_true", help="Send PUB as 3 WS messages (header/payload/CRLF) to simulate fragmentation")
    ap.add_argument("--send-text", action="store_true", help="Send NATS protocol frames as WS TEXT (default: binary)")
    ap.add_argument("--trace", action="store_true", help="Print live ping/pong/info lines")
    args = ap.parse_args()

    node = _load_node_yaml(args.node_yaml)
    runtime_nats = _load_runtime_nats(args.node_yaml)
    subnet_id = str(
        node.get("subnet_id")
        or ((node.get("subnet") or {}).get("id") if isinstance(node.get("subnet"), dict) else "")
        or ""
    )
    if not subnet_id:
        raise RuntimeError("node.yaml missing subnet_id")

    legacy_nats = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    password = str(runtime_nats.get("pass") or runtime_nats.get("token") or legacy_nats.get("pass") or legacy_nats.get("token") or "")
    if not password:
        raise RuntimeError("runtime state missing nats.pass (hub token)")

    # Hub uses canonical user for WS auth
    user = str(runtime_nats.get("user") or legacy_nats.get("user") or f"hub_{subnet_id}")

    urls = [args.url] if args.url else _extract_urls(node, runtime_nats)
    results: list[RunResult] = []
    for u in urls:
        u = _coerce_ws_url(u)
        if not u:
            continue
        tag = str(args.tag or "").strip()
        if not tag:
            tag = f"diag-{_rand_id()}"
        print(f"[diag] ws_lib={args.ws_lib} url={u} user={user} duration={args.duration:.1f}s")
        res = await _run_one(
            u,
            ws_lib=str(args.ws_lib or "auto"),
            user=user,
            password=password,
            duration_s=float(args.duration),
            trace=bool(args.trace),
            conn_tag=tag,
            client_ping_every_s=float(args.client_ping_every),
            client_pong_timeout_s=float(args.client_pong_timeout),
            ws_ping_every_s=float(args.ws_ping_every),
            ws_pong_timeout_s=float(args.ws_pong_timeout),
            subs=[str(s) for s in (args.sub or [])],
            pub_subject=str(args.pub_subject or ""),
            pub_every_s=float(args.pub_every),
            pub_payload=str(args.pub_payload or ""),
            pub_split=bool(args.pub_split),
            send_text=bool(args.send_text),
        )
        results.append(res)
        print(
            "[diag] result:",
            json.dumps(
                {
                    "url": res.url,
                    "ok": res.ok,
                    "seconds": round(res.seconds, 3),
                    "close_code": res.close_code,
                    "close_reason": res.close_reason,
                    "ws_pings_rx": res.ws_pings_rx,
                    "ws_pongs_rx": res.ws_pongs_rx,
                    "client_ws_pings_tx": res.client_ws_pings_tx,
                    "client_ws_pongs_rx": res.client_ws_pongs_rx,
                    "client_ws_pong_timeouts": res.client_ws_pong_timeouts,
                    "nats_pings_rx": res.nats_pings_rx,
                    "nats_pongs_tx": res.nats_pongs_tx,
                    "client_pings_tx": res.client_pings_tx,
                    "client_pongs_rx": res.client_pongs_rx,
                    "client_pong_timeouts": res.client_pong_timeouts,
                    "pubs_tx": res.pubs_tx,
                    "info_rx": res.info_rx,
                    "errors": res.errors,
                },
                ensure_ascii=True,
            ),
        )

    ok_any = any(r.ok for r in results) if results else False
    return 0 if ok_any else 2


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
