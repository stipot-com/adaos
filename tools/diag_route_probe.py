import argparse
import asyncio
import base64
import json
import os
import time
import uuid
from typing import Any, Optional


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


def _ws_diag(nc: Any) -> dict[str, Any]:
    try:
        tr = getattr(nc, "_transport", None)
        ws = getattr(tr, "_ws", None) if tr is not None else None
        closed = getattr(ws, "closed", None) if ws is not None else None
        close_code = getattr(ws, "close_code", None) if ws is not None else None
        close_reason = getattr(ws, "close_reason", None) if ws is not None else None
        proto = None
        try:
            proto = getattr(ws, "protocol", None) if ws is not None else None
        except Exception:
            proto = None
        if not proto:
            try:
                proto = (
                    getattr(ws, "_response", None).headers.get("Sec-WebSocket-Protocol")
                    if ws is not None and getattr(ws, "_response", None) is not None
                    else None
                )
            except Exception:
                proto = None
        exc = None
        try:
            exf = getattr(ws, "exception", None)
            exc = exf() if callable(exf) else None
        except Exception:
            exc = None
        return {
            "ws_closed": closed,
            "ws_close_code": close_code,
            "ws_close_reason": close_reason,
            "ws_proto": proto,
            "ws_exc": str(exc) if exc else None,
        }
    except Exception:
        return {}


async def _run(
    *,
    url: str,
    user: str,
    password: str,
    hub_id: str,
    method: str,
    path: str,
    timeout_s: float,
    verbose: bool,
) -> int:
    try:
        import nats  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"nats-py not available: {type(e).__name__}: {e}") from e

    nc: Any = None
    key = f"{hub_id}--http--diag-{uuid.uuid4().hex[:10]}"
    to_hub = f"route.to_hub.{key}"
    to_browser = f"route.to_browser.{key}"

    done = asyncio.Event()
    reply: Optional[dict[str, Any]] = None
    last_err: Optional[Exception] = None
    stage = "init"

    def _fmt_ws(d: dict[str, Any]) -> str:
        try:
            if not d:
                return ""
            parts = []
            for k in ("ws_closed", "ws_close_code", "ws_close_reason", "ws_proto", "ws_exc"):
                if k in d and d.get(k) is not None and d.get(k) != "":
                    parts.append(f"{k}={d.get(k)!s}")
            return " ".join(parts)
        except Exception:
            return ""

    async def _error_cb(err: Exception) -> None:
        nonlocal last_err
        last_err = err
        if verbose:
            d = _ws_diag(nc) if nc is not None else {}
            extra = _fmt_ws(d)
            print(f"[diag] nats error_cb: {type(err).__name__}: {err}{(' ' + extra) if extra else ''}")

    async def _disconnected_cb() -> None:
        if verbose:
            d = _ws_diag(nc) if nc is not None else {}
            extra = _fmt_ws(d)
            print(f"[diag] nats disconnected_cb{(' ' + extra) if extra else ''}")

    async def _closed_cb() -> None:
        if verbose:
            d = _ws_diag(nc) if nc is not None else {}
            extra = _fmt_ws(d)
            print(f"[diag] nats closed_cb{(' ' + extra) if extra else ''}")

    async def _cb(msg: Any) -> None:
        nonlocal reply
        try:
            raw = bytes(getattr(msg, "data", b"") or b"")
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                if verbose:
                    print(f"[diag] ignore reply: not a dict (type={type(obj).__name__})")
                return
            if obj.get("t") != "http_resp":
                if verbose:
                    print(f"[diag] ignore reply: t={obj.get('t')!r}")
                return
            if obj.get("status") is None:
                if verbose:
                    print("[diag] ignore reply: missing status")
                return
            reply = obj
            done.set()
        except Exception as e:
            if verbose:
                print(f"[diag] ignore reply: {type(e).__name__}: {e}")
            return

    async def _flush(stage_name: str) -> None:
        if nc is None:
            return
        try:
            fp = getattr(nc, "_flush_pending", None)
            if callable(fp):
                try:
                    await asyncio.wait_for(fp(force_flush=True), timeout=2.0)
                except TypeError:
                    try:
                        await asyncio.wait_for(fp(True), timeout=2.0)
                    except TypeError:
                        await asyncio.wait_for(fp(), timeout=2.0)
            else:
                await nc.flush(timeout=2.0)
        except Exception as e:
            if verbose:
                d = _ws_diag(nc)
                extra = _fmt_ws(d)
                print(
                    f"[diag] flush failed stage={stage_name} err={type(e).__name__}: {e}"
                    + ((" " + extra) if extra else "")
                )
            raise

    payload = {
        "t": "http",
        "method": method.upper(),
        "path": path,
        "search": "",
        "headers": {},
        "body_b64": None,
    }

    started = time.monotonic()
    try:
        if verbose:
            print(f"[diag] connect url={url} user={user} pass={_mask(password)}")
        stage = "connect"
        nc = await nats.connect(
            servers=[url],
            user=user,
            password=password,
            name="diag-route-probe",
            allow_reconnect=False,
            connect_timeout=5.0,
            error_cb=_error_cb,
            disconnected_cb=_disconnected_cb,
            closed_cb=_closed_cb,
        )
        stage = "subscribe"
        await nc.subscribe(to_browser, cb=_cb)
        stage = "flush_sub"
        await _flush("flush_sub")

        if verbose:
            print(f"[diag] publish {to_hub} -> wait {to_browser} timeout={timeout_s:.1f}s")
        stage = "publish"
        await nc.publish(to_hub, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        stage = "flush_pub"
        await _flush("flush_pub")

        stage = "wait_reply"
        await asyncio.wait_for(done.wait(), timeout=max(0.1, timeout_s))
    except asyncio.TimeoutError:
        # Note: can also happen on NATS flush() if server PONGs stop arriving.
        d = _ws_diag(nc) if nc is not None else {}
        extra = _fmt_ws(d)
        le = f"{type(last_err).__name__}: {last_err}" if isinstance(last_err, Exception) else None
        print(
            f"[diag] timeout stage={stage} key={key} path={path}"
            + (f" last_err={le}" if le else "")
            + ((" " + extra) if extra else "")
        )
        if stage != "wait_reply":
            print("[diag] note: timeout happened before reply wait (likely flush/connect issue)")
        return 2
    except Exception as e:
        d = _ws_diag(nc) if nc is not None else {}
        extra = _fmt_ws(d)
        le = f"{type(last_err).__name__}: {last_err}" if isinstance(last_err, Exception) else None
        print(
            f"[diag] error stage={stage} key={key} path={path} err={type(e).__name__}: {e}"
            + (f" last_err={le}" if le else "")
            + ((" " + extra) if extra else "")
        )
        return 2
    finally:
        try:
            if nc is not None:
                await nc.close()
        except Exception:
            pass

    took = time.monotonic() - started
    if not reply:
        print(f"[diag] no reply decoded (took_s={took:.3f})")
        return 2

    status = int(reply.get("status") or 0)
    body_b64 = reply.get("body_b64") if isinstance(reply.get("body_b64"), str) else ""
    body_raw = b""
    try:
        if body_b64:
            body_raw = base64.b64decode(body_b64.encode("ascii"))
    except Exception:
        body_raw = b""

    # Print a safe summary (no request headers/search, no secrets).
    print(
        "[diag] ok",
        json.dumps(
            {
                "key": key,
                "path": path,
                "status": status,
                "body_bytes": len(body_raw),
                "took_s": round(took, 3),
                "truncated": bool(reply.get("truncated") is True),
                "err": (reply.get("err") if isinstance(reply.get("err"), str) else "")[:200],
            },
            ensure_ascii=True,
        ),
    )

    # Best-effort decode JSON response body.
    try:
        if body_raw:
            obj = json.loads(body_raw.decode("utf-8"))
            if isinstance(obj, dict):
                slim = {k: obj.get(k) for k in ("ready", "subnet_id", "node_id", "role", "ok", "ts") if k in obj}
                print("[diag] body", json.dumps(slim, ensure_ascii=True))
    except Exception:
        pass

    return 0 if status else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Send a synthetic Root route-proxy HTTP probe via NATS and wait for reply.")
    ap.add_argument("--node-yaml", default=".adaos/node.yaml", help="Path to node.yaml (default: .adaos/node.yaml)")
    ap.add_argument("--url", default="", help="Override NATS WS URL (e.g. wss://nats.inimatic.com/nats)")
    ap.add_argument("--user", default="", help="NATS user (default: hub_<subnet_id>)")
    ap.add_argument("--pass", dest="password", default="", help="NATS password/token")
    ap.add_argument("--hub-id", default="", help="Hub ID/subnet_id (default: from node.yaml)")
    ap.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    ap.add_argument("--path", default="/api/node/status", help="HTTP path (default: /api/node/status)")
    ap.add_argument("--timeout", type=float, default=6.0, help="Seconds to wait for reply (default: 6)")
    ap.add_argument("--verbose", action="store_true", help="Verbose output")
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

    return asyncio.run(
        _run(
            url=url,
            user=user,
            password=password,
            hub_id=hub_id,
            method=str(args.method or "GET"),
            path=str(args.path or "/api/node/status"),
            timeout_s=float(args.timeout),
            verbose=bool(args.verbose),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
