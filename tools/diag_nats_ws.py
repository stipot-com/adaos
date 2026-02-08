import argparse
import asyncio
import json
import os
import random
import string
import time
from dataclasses import dataclass
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


def _extract_urls(node: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    nats = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    base = nats.get("ws_url") or nats.get("ws") or nats.get("url") or ""
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
    nats_pings_rx: int
    nats_pongs_tx: int
    info_rx: int
    errors: list[str]


async def _run_one(url: str, user: str, password: str, duration_s: float, trace: bool) -> RunResult:
    started_at = time.monotonic()
    errors: list[str] = []
    ws_pings_rx = 0
    ws_pongs_rx = 0
    nats_pings_rx = 0
    nats_pongs_tx = 0
    info_rx = 0

    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"Sec-WebSocket-Protocol": "nats"}

    close_code: Optional[int] = None
    close_reason: Optional[str] = None

    async with aiohttp.ClientSession(timeout=timeout) as sess:
        try:
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
                await ws.send_bytes(b"CONNECT " + json.dumps(connect_obj).encode("utf-8") + b"\r\n")

                deadline = time.monotonic() + duration_s
                while time.monotonic() < deadline:
                    left = max(deadline - time.monotonic(), 0.1)
                    try:
                        msg = await ws.receive(timeout=min(left, 5.0))
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
                        break

                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        try:
                            close_code = ws.close_code
                        except Exception:
                            close_code = None
                        break

                    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        data = msg.data
                        if isinstance(data, str):
                            raw = data.encode("utf-8")
                        else:
                            raw = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
                        if raw.startswith(b"INFO "):
                            info_rx += 1
                            if trace:
                                line = raw.split(b"\r\n", 1)[0][:200]
                                print(f"[diag] nats INFO: {line!r}")
                        # Count protocol pings and respond
                        if b"PING\r\n" in raw:
                            nats_pings_rx += raw.count(NATS_PING)
                            try:
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
        nats_pings_rx=nats_pings_rx,
        nats_pongs_tx=nats_pongs_tx,
        info_rx=info_rx,
        errors=errors,
    )


async def _amain() -> int:
    ap = argparse.ArgumentParser(description="Diagnose NATS-over-WebSocket connection to Root (ping/pong + NATS INFO/PING).")
    ap.add_argument("--node-yaml", default=".adaos/node.yaml", help="Path to node.yaml (default: .adaos/node.yaml)")
    ap.add_argument("--url", default="", help="Override WS URL (e.g. wss://api.inimatic.com/nats)")
    ap.add_argument("--duration", type=float, default=70.0, help="Run duration per URL in seconds (default: 70)")
    ap.add_argument("--trace", action="store_true", help="Print live ping/pong/info lines")
    args = ap.parse_args()

    node = _load_node_yaml(args.node_yaml)
    subnet_id = str(node.get("subnet_id") or "")
    if not subnet_id:
        raise RuntimeError("node.yaml missing subnet_id")

    nats = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    password = str(nats.get("pass") or nats.get("token") or "")
    if not password:
        raise RuntimeError("node.yaml missing nats.pass (hub token)")

    # Hub uses canonical user for WS auth
    user = f"hub_{subnet_id}"

    urls = [args.url] if args.url else _extract_urls(node)
    results: list[RunResult] = []
    for u in urls:
        u = _coerce_ws_url(u)
        if not u:
            continue
        print(f"[diag] url={u} user={user} duration={args.duration:.1f}s")
        res = await _run_one(u, user=user, password=password, duration_s=float(args.duration), trace=bool(args.trace))
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
                    "nats_pings_rx": res.nats_pings_rx,
                    "nats_pongs_tx": res.nats_pongs_tx,
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
