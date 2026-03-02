import argparse
import asyncio
import base64
import json
import time
import uuid
from typing import Any


def _mask(s: str) -> str:
    if not s:
        return s
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]


def _load_node_yaml(path: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"PyYAML not available: {type(e).__name__}: {e}") from e

    with open(path, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}
    if not isinstance(y, dict):
        raise RuntimeError(f"unexpected node.yaml type: {type(y).__name__}")
    return y


async def _amain() -> int:
    ap = argparse.ArgumentParser(description="Minimal route.to_hub.* responder (debug tool).")
    ap.add_argument("--node-yaml", default=".adaos/node.yaml", help="Path to node.yaml (default: .adaos/node.yaml)")
    ap.add_argument("--url", default="", help="Override NATS WS URL (e.g. wss://nats.inimatic.com/nats)")
    ap.add_argument("--hub-id", default="", help="Hub ID/subnet_id (default: from node.yaml)")
    ap.add_argument("--user", default="", help="NATS user (default: hub_<subnet_id>)")
    ap.add_argument("--pass", dest="password", default="", help="NATS password/token (default: from node.yaml)")
    ap.add_argument("--duration", type=float, default=30.0, help="Run duration in seconds (default: 30)")
    ap.add_argument("--verbose", action="store_true", help="Verbose output (no secrets)")
    args = ap.parse_args()

    node = _load_node_yaml(args.node_yaml)
    hub_id = str(args.hub_id or node.get("subnet_id") or "").strip()
    if not hub_id:
        print("[diag] missing hub_id: pass --hub-id or ensure node.yaml has subnet_id")
        return 2

    nats_cfg = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    url = str(args.url or nats_cfg.get("ws_url") or "").strip()
    if not url:
        print("[diag] missing url: pass --url or ensure node.yaml has nats.ws_url")
        return 2

    user = str(args.user or nats_cfg.get("user") or f"hub_{hub_id}").strip()
    password = str(args.password or nats_cfg.get("pass") or "").strip()
    if not password:
        print("[diag] missing password: pass --pass or ensure node.yaml has nats.pass")
        return 2

    try:
        import nats  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"nats-py not available: {type(e).__name__}: {e}") from e

    if args.verbose:
        print(f"[diag] connect url={url} user={user} pass={_mask(password)}")
        print("[diag] subscribe route.to_hub.* -> reply route.to_browser.<key> for t=http (/api/node/status)")

    started_at = time.monotonic()
    nc = await nats.connect(
        servers=[url],
        user=user,
        password=password,
        name="diag-route-responder",
        allow_reconnect=False,
        connect_timeout=5.0,
    )

    async def _reply(key: str, payload: dict[str, Any]) -> None:
        await nc.publish(
            f"route.to_browser.{key}",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        # Avoid relying on NATS PING/PONG for flush: some NATS-over-WS proxies can get PONG-stuck,
        # making `flush()` time out even though the socket is healthy.
        try:
            fp = getattr(nc, "_flush_pending", None)
            if callable(fp):
                try:
                    await asyncio.wait_for(fp(force_flush=True), timeout=1.0)
                except TypeError:
                    # Older clients: best-effort positional arg.
                    try:
                        await asyncio.wait_for(fp(True), timeout=1.0)
                    except TypeError:
                        await asyncio.wait_for(fp(), timeout=1.0)
            else:
                await nc.flush(timeout=2.0)
        except Exception:
            # Best-effort; responder is for debugging only.
            pass

    async def _cb(msg: Any) -> None:
        try:
            subject = str(getattr(msg, "subject", "") or "")
            if not subject.startswith("route.to_hub."):
                return
            key = subject.split(".", 2)[2]
            if not key.startswith(f"{hub_id}--"):
                return
            raw = bytes(getattr(msg, "data", b"") or b"")
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                return
            t = obj.get("t")
            if t != "http":
                return
            path = str(obj.get("path") or "")
            if path.rstrip("/") != "/api/node/status":
                return

            payload0 = {"ok": True, "ready": True, "subnet_id": hub_id, "ts": time.time()}
            body = json.dumps(payload0, ensure_ascii=False).encode("utf-8")
            resp = {
                "t": "http_resp",
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body_b64": base64.b64encode(body).decode("ascii"),
                "truncated": False,
            }
            await _reply(key, resp)
            if args.verbose:
                print(f"[diag] replied key={key}")
        except Exception as e:
            if args.verbose:
                print(f"[diag] cb error: {type(e).__name__}: {e}")

    await nc.subscribe("route.to_hub.*", cb=_cb)
    await nc.flush(timeout=2.0)

    # Keep alive for a short window.
    deadline = time.monotonic() + max(0.1, float(args.duration))
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    try:
        await nc.close()
    except Exception:
        pass

    if args.verbose:
        took = time.monotonic() - started_at
        print(f"[diag] done (took_s={took:.3f})")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
