import argparse
import asyncio
import json
import random
import string
import time
import urllib.parse
from pathlib import Path
from typing import Any


NATS_PING = b"PING\r\n"
NATS_PONG = b"PONG\r\n"


def _rand_id(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _load_runtime_nats(node_yaml: str) -> dict[str, Any]:
    node_path = Path(node_yaml)
    runtime_path = node_path.parent / "state" / "node_runtime.json"
    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    nats = payload.get("nats")
    if not isinstance(nats, dict):
        raise RuntimeError(f"{runtime_path} missing nats object")
    return nats


def _add_conn_tag(url: str, tag: str) -> str:
    if not tag:
        return url
    u = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    qs["adaos_conn"] = [tag]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(qs, doseq=True), u.fragment))


def _connect_command(user: str, password: str) -> bytes:
    obj = {
        "echo": True,
        "headers": True,
        "lang": "python3",
        "name": "diag-raw-concurrent",
        "no_responders": True,
        "pass": password,
        "pedantic": False,
        "protocol": 1,
        "user": user,
        "verbose": False,
        "version": "diag",
    }
    return b"CONNECT " + json.dumps(obj, sort_keys=True).encode("utf-8") + b"\r\n"


async def _run(args: argparse.Namespace) -> int:
    import websockets  # type: ignore

    runtime = _load_runtime_nats(str(args.node_yaml))
    url = str(args.url or runtime.get("ws_url") or "wss://api.inimatic.com/nats")
    user = str(runtime.get("user") or "")
    password = str(runtime.get("pass") or runtime.get("token") or "")
    if not user or not password:
        raise RuntimeError("runtime nats credentials are incomplete")
    tag = str(args.tag or f"diag-concurrent-{_rand_id()}").strip()
    url = _add_conn_tag(url, tag)
    subject = str(args.subject or f"diag.concurrent.{_rand_id()}").strip()
    payload = str(args.payload or "diag").encode("utf-8")
    duration_s = max(1.0, float(args.duration))
    publish_every_s = max(0.05, float(args.publish_every))

    headers = {
        "X-AdaOS-Nats-Conn": tag,
        "X-AdaOS-Runtime-Instance": f"diag-raw-concurrent-{tag}",
        "X-AdaOS-Runtime-Role": "diagnostic",
    }
    started = time.monotonic()
    deadline = started + duration_s
    rx_msg = 0
    tx_pub = 0
    rx_ping = 0
    tx_pong = 0
    rx_pong = 0
    errors: list[str] = []
    stop = asyncio.Event()

    async with websockets.connect(
        url,
        additional_headers=headers,
        subprotocols=["nats"],
        open_timeout=10,
        close_timeout=2,
        ping_interval=None,
        ping_timeout=None,
        max_size=None,
        compression=None,
    ) as ws:
        raw_info = await asyncio.wait_for(ws.recv(), timeout=5)
        if args.trace:
            data = raw_info.encode("utf-8") if isinstance(raw_info, str) else bytes(raw_info)
            print(f"[diag] INFO {data[:180]!r}")
        await ws.send(_connect_command(user, password))
        await ws.send(NATS_PING)
        first_pong = await asyncio.wait_for(ws.recv(), timeout=5)
        first_pong_b = first_pong.encode("utf-8") if isinstance(first_pong, str) else bytes(first_pong)
        if NATS_PONG not in first_pong_b:
            raise RuntimeError(f"initial PING did not receive PONG: {first_pong_b[:120]!r}")
        rx_pong += first_pong_b.count(NATS_PONG)
        await ws.send(f"SUB {subject}  1\r\n".encode("utf-8"))

        async def reader() -> None:
            nonlocal rx_msg, rx_ping, tx_pong, rx_pong
            while time.monotonic() < deadline and not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    errors.append(f"recv failed: {type(e).__name__}: {e}")
                    stop.set()
                    return
                data = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
                if b"MSG " in data:
                    got = data.count(b"MSG ")
                    rx_msg += got
                    if args.trace:
                        print(f"[diag] MSG +{got} rx={rx_msg} head={data[:140]!r}")
                if NATS_PING in data:
                    got = data.count(NATS_PING)
                    rx_ping += got
                    for _ in range(got):
                        await ws.send(NATS_PONG)
                        tx_pong += 1
                    if args.trace:
                        print(f"[diag] PING +{got} -> PONG tx_pong={tx_pong}")
                if NATS_PONG in data:
                    rx_pong += data.count(NATS_PONG)

        async def writer() -> None:
            nonlocal tx_pub
            next_at = time.monotonic() + publish_every_s
            while time.monotonic() < deadline and not stop.is_set():
                now = time.monotonic()
                if now >= next_at:
                    frame = f"PUB {subject}  {len(payload)}\r\n".encode("utf-8") + payload + b"\r\n"
                    try:
                        await ws.send(frame)
                    except Exception as e:
                        errors.append(f"send failed: {type(e).__name__}: {e}")
                        stop.set()
                        return
                    tx_pub += 1
                    if args.trace and tx_pub <= 5:
                        print(f"[diag] PUB tx={tx_pub}")
                    next_at = now + publish_every_s
                await asyncio.sleep(0.02)

        await asyncio.gather(reader(), writer())

    result = {
        "ok": not errors and tx_pub > 0 and rx_msg >= max(1, tx_pub - 1),
        "seconds": round(time.monotonic() - started, 3),
        "tag": tag,
        "subject": subject,
        "tx_pub": tx_pub,
        "rx_msg": rx_msg,
        "rx_ping": rx_ping,
        "tx_pong": tx_pong,
        "rx_pong": rx_pong,
        "errors": errors,
    }
    print("[diag] result:", json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Raw NATS-over-WS concurrent reader/writer diagnostic.")
    ap.add_argument("--node-yaml", default=".adaos/node.yaml")
    ap.add_argument("--url", default="wss://api.inimatic.com/nats")
    ap.add_argument("--tag", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--publish-every", type=float, default=1.0)
    ap.add_argument("--payload", default='{"probe":"concurrent"}')
    ap.add_argument("--trace", action="store_true")
    return int(asyncio.run(_run(ap.parse_args())))


if __name__ == "__main__":
    raise SystemExit(main())
