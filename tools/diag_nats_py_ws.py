import argparse
import asyncio
import json
import os
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Optional


def _mask(s: str) -> str:
    if not s:
        return s
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]


def _load_from_runtime_state(path: str) -> dict[str, str]:
    runtime_path = Path(path).expanduser().resolve().parent / "state" / "node_runtime.json"
    try:
        payload = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    nats = payload.get("nats")
    if not isinstance(nats, dict):
        return {}

    out: dict[str, str] = {}
    for k_src, k_out in [("ws_url", "url"), ("user", "user"), ("pass", "password")]:
        v = nats.get(k_src)
        if isinstance(v, str) and v.strip():
            out[k_out] = v.strip()
    return out


def _load_from_node_yaml(path: str) -> dict[str, str]:
    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"PyYAML not available: {type(e).__name__}: {e}") from e

    with open(path, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}

    nats = y.get("nats", {}) if isinstance(y, dict) else {}
    if not isinstance(nats, dict):
        nats = {}

    out: dict[str, str] = {}
    for k_src, k_out in [("ws_url", "url"), ("user", "user"), ("pass", "password")]:
        v = nats.get(k_src)
        if isinstance(v, str) and v.strip():
            out[k_out] = v.strip()
    return out


def _add_conn_tag(url: str, tag: str) -> str:
    if not tag or not str(url or "").startswith("ws"):
        return url
    try:
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        q.setdefault("adaos_conn", tag)
        return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))
    except Exception:
        return url


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


def _patch_ws_transport(*, heartbeat_s: Optional[float], max_msg_size: int, verbose: bool) -> None:
    try:
        from nats.aio import transport as _nats_transport  # type: ignore
    except Exception:
        return

    try:
        import aiohttp  # type: ignore
    except Exception:
        return

    ws_tr = getattr(_nats_transport, "WebSocketTransport", None)
    if ws_tr is None:
        return

    orig_connect = getattr(ws_tr, "connect", None)
    orig_connect_tls = getattr(ws_tr, "connect_tls", None)
    if not callable(orig_connect) or not callable(orig_connect_tls):
        return

    if getattr(orig_connect, "_adaos_diag_patch", False) or getattr(orig_connect_tls, "_adaos_diag_patch", False):
        return

    async def _connect(self, uri, buffer_size, connect_timeout):  # type: ignore[no-redef]
        headers = self._get_custom_headers()
        hb = heartbeat_s if (isinstance(heartbeat_s, (int, float)) and heartbeat_s > 0.0) else None
        mms = int(max_msg_size) if isinstance(max_msg_size, int) else 0
        if mms < 0:
            mms = 0
        if verbose:
            print(f"[diag] ws_connect uri={uri.geturl()} heartbeat={hb} max_msg_size={mms}")
        self._ws = await self._client.ws_connect(
            uri.geturl(),
            timeout=connect_timeout,
            headers=headers,
            protocols=("nats",),
            autoping=True,
            autoclose=True,
            heartbeat=hb,
            max_msg_size=mms,
        )
        self._using_tls = False

    async def _connect_tls(self, uri, ssl_context, buffer_size, connect_timeout):  # type: ignore[no-redef]
        headers = self._get_custom_headers()
        hb = heartbeat_s if (isinstance(heartbeat_s, (int, float)) and heartbeat_s > 0.0) else None
        mms = int(max_msg_size) if isinstance(max_msg_size, int) else 0
        if mms < 0:
            mms = 0
        u = uri if isinstance(uri, str) else uri.geturl()
        if verbose:
            print(f"[diag] ws_connect_tls uri={u} heartbeat={hb} max_msg_size={mms}")
        self._ws = await self._client.ws_connect(
            u,
            ssl=ssl_context,
            timeout=connect_timeout,
            headers=headers,
            protocols=("nats",),
            autoping=True,
            autoclose=True,
            heartbeat=hb,
            max_msg_size=mms,
        )
        self._using_tls = True

    try:
        setattr(_connect, "_adaos_diag_patch", True)
        setattr(_connect_tls, "_adaos_diag_patch", True)
    except Exception:
        pass
    try:
        setattr(ws_tr, "connect", _connect)
        setattr(ws_tr, "connect_tls", _connect_tls)
    except Exception:
        # If patching fails, fall back to stock behavior.
        return


async def _run(
    url: str,
    user: str,
    password: str,
    duration_s: float,
    *,
    verbose: bool,
    ws_heartbeat_s: Optional[float],
    ws_max_msg_size: int,
    nats_ping_interval_s: float,
    tag: str,
    subject: str,
    publish_every_s: float,
    payload: str,
    report_every_s: float,
) -> int:
    try:
        import nats  # type: ignore
    except Exception as e:  # pragma: no cover
        print(f"[diag] failed to import nats-py: {type(e).__name__}: {e}")
        return 2

    _patch_ws_transport(heartbeat_s=ws_heartbeat_s, max_msg_size=ws_max_msg_size, verbose=verbose)

    nc: Any = None
    started = time.monotonic()
    last_err: Optional[Exception] = None
    disconnected = asyncio.Event()
    rx_count = 0
    tx_count = 0

    async def _message_cb(msg: Any) -> None:
        nonlocal rx_count
        rx_count += 1
        if verbose:
            try:
                print(f"[diag] rx subject={msg.subject} bytes={len(msg.data or b'')}")
            except Exception:
                pass

    async def _error_cb(err: Exception) -> None:
        nonlocal last_err
        last_err = err
        d = _ws_diag(nc) if nc is not None else {}
        print(f"[diag] error_cb: {type(err).__name__}: {err} ({', '.join([f'{k}={v}' for k,v in d.items()])})")
        disconnected.set()

    async def _disconnected_cb() -> None:
        d = _ws_diag(nc) if nc is not None else {}
        le = None
        try:
            le = getattr(nc, "last_error", None) if nc is not None else None
        except Exception:
            le = None
        print(f"[diag] disconnected_cb last_error={type(le).__name__ if isinstance(le, Exception) else None} ({', '.join([f'{k}={v}' for k,v in d.items()])})")
        disconnected.set()

    try:
        nc = nats.aio.client.Client()
        url = _add_conn_tag(url, tag)
        if verbose:
            print(
                f"[diag] connecting url={url} user={user} pass={_mask(password)} "
                f"tag={tag} duration_s={duration_s}"
            )
        await nc.connect(
            servers=[url],
            user=user,
            password=password,
            name="diag-nats-py-ws",
            ws_connection_headers=(
                {
                    "X-AdaOS-Nats-Conn": [tag],
                    "X-AdaOS-Runtime-Instance": [f"diag-stock-{tag}"],
                    "X-AdaOS-Runtime-Role": ["diagnostic"],
                }
                if tag
                else None
            ),
            allow_reconnect=False,
            connect_timeout=5.0,
            error_cb=_error_cb,
            disconnected_cb=_disconnected_cb,
            # Keep nats-py pings effectively off; we want to observe the raw transport stability first.
            ping_interval=max(1.0, float(nats_ping_interval_s)),
            max_outstanding_pings=10,
        )
        if verbose:
            try:
                srv = getattr(nc, "connected_url", None) or getattr(nc, "connected_server", None)
                print(f"[diag] connected server={srv}")
            except Exception:
                print("[diag] connected")
        subject_s = str(subject or "").strip()
        if subject_s:
            await nc.subscribe(subject_s, cb=_message_cb)
            await nc.flush()
            if verbose:
                print(f"[diag] subscribed subject={subject_s}")

        deadline = time.monotonic() + max(0.0, duration_s)
        next_pub_at = time.monotonic() + float(publish_every_s) if subject_s and publish_every_s > 0 else None
        next_report_at = time.monotonic()
        payload_b = str(payload or "").encode("utf-8")
        while time.monotonic() < deadline:
            if disconnected.is_set():
                print("[diag] disconnected before timeout")
                return 1
            now = time.monotonic()
            if next_pub_at is not None and now >= next_pub_at:
                await nc.publish(subject_s, payload_b)
                fp = getattr(nc, "_flush_pending", None)
                if callable(fp):
                    try:
                        await fp(force_flush=True)
                    except TypeError:
                        await fp()
                tx_count += 1
                if verbose:
                    print(f"[diag] tx subject={subject_s} bytes={len(payload_b)} n={tx_count}")
                next_pub_at = now + float(publish_every_s)
            if now >= next_report_at:
                d = _ws_diag(nc)
                print(
                    "[diag] report "
                    + json.dumps(
                        {
                            "rx_count": rx_count,
                            "tx_count": tx_count,
                            "last_err": type(last_err).__name__ if last_err else None,
                            **d,
                        },
                        ensure_ascii=False,
                    )
                )
                next_report_at = now + max(1.0, float(report_every_s))
            await asyncio.sleep(0.2)
        return 0
    except Exception as e:
        print(f"[diag] connect/run failed: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            if nc is not None:
                await nc.close()
        except Exception:
            pass
        try:
            tr = getattr(nc, "_transport", None) if nc is not None else None
            ws = getattr(tr, "_ws", None) if tr is not None else None
            if ws is not None and not getattr(ws, "closed", True):
                await ws.close()
        except Exception:
            pass
        try:
            tr = getattr(nc, "_transport", None) if nc is not None else None
            client = getattr(tr, "_client", None) if tr is not None else None
            if client is not None and not getattr(client, "closed", True):
                await client.close()
        except Exception:
            pass
        took = time.monotonic() - started
        if verbose:
            extra = _ws_diag(nc) if nc is not None else {}
            print(
                f"[diag] done took_s={took:.2f} rx={rx_count} tx={tx_count} "
                f"last_err={type(last_err).__name__ if last_err else None} "
                f"({', '.join([f'{k}={v}' for k,v in extra.items()])})"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose NATS-over-WebSocket using nats-py (hub-like).")
    ap.add_argument("--node-yaml", default="", help="Load bootstrap identity from node.yaml and runtime NATS credentials from sibling state/node_runtime.json")
    ap.add_argument("--url", default=os.getenv("NATS_WS_URL", "wss://api.inimatic.com/nats"), help="WS URL, e.g. wss://api.inimatic.com/nats")
    ap.add_argument("--user", default=os.getenv("NATS_USER", ""), help="NATS user (e.g. hub_<subnet_id>)")
    ap.add_argument("--pass", dest="password", default=os.getenv("NATS_PASS", ""), help="NATS password/token")
    ap.add_argument("--duration", type=float, default=120.0, help="Seconds to stay connected")
    ap.add_argument("--ws-heartbeat", type=float, default=float(os.getenv("HUB_NATS_WS_HEARTBEAT_S", "0") or "0"), help="aiohttp ws_connect heartbeat seconds (0=off)")
    ap.add_argument("--ws-max-msg-size", type=int, default=int(os.getenv("HUB_NATS_WS_MAX_MSG_SIZE", "0") or "0"), help="aiohttp ws_connect max_msg_size (0=unlimited)")
    ap.add_argument("--nats-ping-interval", type=float, default=3600.0, help="nats-py ping_interval seconds")
    ap.add_argument("--tag", default="", help="Connection tag for Root logs (sets X-AdaOS-Nats-Conn + ?adaos_conn=...)")
    ap.add_argument("--subject", default="", help="Subscribe and publish to this subject (empty=connect-only)")
    ap.add_argument("--publish-every", type=float, default=0.0, help="Publish a message every N seconds (0=off)")
    ap.add_argument("--payload", default="{}", help="Publish payload")
    ap.add_argument("--report-every", type=float, default=10.0, help="Report interval in seconds")
    ap.add_argument("--verbose", action="store_true", help="Verbose output")
    args = ap.parse_args()

    if args.node_yaml and args.node_yaml.strip():
        try:
            loaded = _load_from_runtime_state(args.node_yaml.strip())
            if not loaded:
                loaded = _load_from_node_yaml(args.node_yaml.strip())
        except Exception as e:
            print(f"[diag] failed to load --node-yaml: {type(e).__name__}: {e}")
            return 2
        if "url" in loaded and (not args.url or args.url == ap.get_default("url")):
            args.url = loaded["url"]
        if "user" in loaded and (not args.user):
            args.user = loaded["user"]
        if "password" in loaded and (not args.password):
            args.password = loaded["password"]

    if not args.user or not args.password:
        print("[diag] missing credentials: provide --user/--pass or --node-yaml (.adaos/node.yaml + sibling state/node_runtime.json)")
        return 2
    if not args.tag:
        args.tag = f"diag-stock-{uuid.uuid4().hex[:10]}"

    return asyncio.run(
        _run(
            args.url,
            args.user,
            args.password,
            float(args.duration),
            verbose=bool(args.verbose),
            ws_heartbeat_s=float(args.ws_heartbeat),
            ws_max_msg_size=int(args.ws_max_msg_size),
            nats_ping_interval_s=float(args.nats_ping_interval),
            tag=str(args.tag or ""),
            subject=str(args.subject or ""),
            publish_every_s=float(args.publish_every),
            payload=str(args.payload or ""),
            report_every_s=float(args.report_every),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
