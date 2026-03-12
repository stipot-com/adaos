import argparse
import asyncio
import json
import os
import time
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

import nats as _nats

from adaos.services.nats_config import normalize_nats_ws_url, order_nats_ws_candidates
from adaos.services.nats_ws_transport import install_nats_ws_transport_patch


def _load_node_yaml(path: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed; cannot parse node.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected node.yaml type: {type(data).__name__}")
    return data


def _nats_config(node: dict[str, Any]) -> tuple[list[str], str, str]:
    nats_cfg = node.get("nats") if isinstance(node.get("nats"), dict) else {}
    explicit_url = normalize_nats_ws_url(str(nats_cfg.get("ws_url") or "").strip(), fallback=None)
    user = str(nats_cfg.get("user") or "").strip()
    password = str(nats_cfg.get("pass") or "").strip()
    if not user or not password:
        raise RuntimeError("node.yaml nats.user / nats.pass are required")
    candidates = order_nats_ws_candidates(
        [item for item in [explicit_url, "wss://nats.inimatic.com/nats", "wss://api.inimatic.com/nats"] if item],
        explicit_url=explicit_url,
        prefer_dedicated=os.getenv("HUB_NATS_PREFER_DEDICATED", "1"),
    )
    if not candidates:
        raise RuntimeError("no NATS WS candidates resolved")
    return candidates, user, password


def _transport_diag(tr: Any) -> dict[str, Any]:
    now = time.monotonic()

    def _ago(attr: str) -> float | None:
        try:
            value = getattr(tr, attr, None)
            if isinstance(value, (int, float)):
                return round(now - float(value), 3)
        except Exception:
            return None
        return None

    return {
        "transport": type(tr).__name__ if tr is not None else None,
        "ws_url": getattr(tr, "_adaos_ws_url", None) if tr is not None else None,
        "ws_tag": getattr(tr, "_adaos_ws_tag", None) if tr is not None else None,
        "ws_proto": getattr(tr, "_adaos_ws_proto", None) if tr is not None else None,
        "ws_hb_s": getattr(tr, "_adaos_ws_heartbeat", None) if tr is not None else None,
        "ws_hb_mode": getattr(tr, "_adaos_ws_heartbeat_mode", None) if tr is not None else None,
        "ws_data_hb_s": getattr(tr, "_adaos_ws_data_heartbeat", None) if tr is not None else None,
        "ws_recv_timeout_s": getattr(tr, "_adaos_ws_recv_timeout", None) if tr is not None else None,
        "last_rx_ago_s": _ago("_adaos_last_rx_at"),
        "last_tx_ago_s": _ago("_adaos_last_tx_at"),
        "last_ping_rx_ago_s": _ago("_adaos_last_ping_rx_at"),
        "last_pong_tx_ago_s": _ago("_adaos_last_pong_tx_at"),
        "last_ws_ping_tx_ago_s": _ago("_adaos_last_ws_ping_tx_at"),
        "ka_pings_rx": getattr(tr, "_adaos_pings_rx", None) if tr is not None else None,
        "ka_pongs_tx": getattr(tr, "_adaos_pongs_tx", None) if tr is not None else None,
        "ws_pings_tx": getattr(tr, "_adaos_ws_pings_tx", None) if tr is not None else None,
        "last_tx_kind": getattr(tr, "_adaos_last_tx_kind", None) if tr is not None else None,
        "last_tx_subj": getattr(tr, "_adaos_last_tx_subj", None) if tr is not None else None,
        "last_recv_err": type(getattr(tr, "_adaos_last_recv_error", None)).__name__
        if getattr(tr, "_adaos_last_recv_error", None) is not None
        else None,
    }


async def _run(args: argparse.Namespace) -> int:
    node = _load_node_yaml(args.node)
    servers, user, password = _nats_config(node)
    if args.server:
        servers = [str(args.server).strip()]

    if str(os.getenv("HUB_NATS_WS_IMPL", "websockets") or "websockets").strip().lower() in {"aiohttp", "aio"}:
        os.environ.setdefault("HUB_NATS_WS_PATCH_AIOHTTP", "1")
    active_impl = install_nats_ws_transport_patch(verbose=True)
    print(
        f"[diag-client] impl={active_impl} servers={servers} user={user} duration={float(args.duration):.1f}s"
    )

    ping_interval = int(os.getenv("HUB_NATS_PING_INTERVAL_S", "120") or "120")
    if ping_interval <= 0:
        ping_interval = 120
    max_outstanding_pings = int(os.getenv("HUB_NATS_MAX_OUTSTANDING_PINGS", "10") or "10")

    errors: list[str] = []
    rx_count = 0
    tx_count = 0
    nc = _nats.aio.client.Client()

    async def _error_cb(exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        errors.append(msg)
        print(f"[diag-client] error_cb={msg}")

    async def _message_cb(msg) -> None:
        nonlocal rx_count
        rx_count += 1
        if args.trace:
            try:
                print(
                    f"[diag-client] rx subject={msg.subject} reply={msg.reply or ''} bytes={len(msg.data or b'')}"
                )
            except Exception:
                pass

    await nc.connect(
        servers=servers,
        user=user,
        password=password,
        name=f"diag-client-{int(time.time())}",
        allow_reconnect=False,
        ping_interval=ping_interval,
        max_outstanding_pings=max_outstanding_pings,
        connect_timeout=5.0,
        error_cb=_error_cb,
    )
    try:
        tr = getattr(nc, "_transport", None)
        if tr is not None:
            try:
                setattr(tr, "_adaos_nc", nc)
            except Exception:
                pass
        try:
            disable_ping_task = str(os.getenv("HUB_NATS_DISABLE_PING_INTERVAL_TASK", "1") or "").strip() != "0"
            if disable_ping_task:
                pt = getattr(nc, "_ping_interval_task", None)
                if isinstance(pt, asyncio.Task):
                    if not pt.done():
                        pt.cancel()
                    setattr(nc, "_ping_interval_task", None)
                    print("[diag-client] nats ping interval task disabled")
        except Exception:
            pass
        print(f"[diag-client] connected server={getattr(nc, 'connected_url', None)}")

        subject = str(args.subject or "").strip()
        if subject:
            await nc.subscribe(subject, cb=_message_cb)
            await nc.flush()
            print(f"[diag-client] subscribed subject={subject}")

        deadline = time.monotonic() + float(args.duration)
        report_every = max(1.0, float(args.report_every))
        next_report_at = time.monotonic()
        next_pub_at = time.monotonic() + max(0.1, float(args.publish_every)) if args.publish_every > 0 else None
        payload = str(args.payload or "").encode("utf-8")
        while time.monotonic() < deadline:
            now = time.monotonic()
            if next_pub_at is not None and now >= next_pub_at and subject:
                await nc.publish(subject, payload)
                await nc.flush()
                tx_count += 1
                if args.trace:
                    print(f"[diag-client] tx subject={subject} bytes={len(payload)} n={tx_count}")
                next_pub_at = now + float(args.publish_every)
            if now >= next_report_at:
                tr = getattr(nc, "_transport", None)
                print(
                    "[diag-client] "
                    + json.dumps(
                        {
                            "rx_count": rx_count,
                            "tx_count": tx_count,
                            "errors": list(errors),
                            "diag": _transport_diag(tr),
                        },
                        ensure_ascii=False,
                    )
                )
                next_report_at = now + report_every
            await asyncio.sleep(0.2)
    finally:
        tr = getattr(nc, "_transport", None)
        try:
            await nc.drain()
        except Exception:
            try:
                await nc.close()
            except Exception:
                pass
        try:
            if tr is not None and callable(getattr(tr, "close", None)):
                tr.close()
        except Exception:
            pass
        try:
            if tr is not None and callable(getattr(tr, "wait_closed", None)):
                await asyncio.wait_for(tr.wait_closed(), timeout=5.0)
        except Exception:
            pass
    print("[diag-client] done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a standalone NATS-over-WS client using AdaOS transport patches.")
    ap.add_argument("--node", default=os.getenv("ADAOS_NODE_YAML", ".adaos/node.yaml"), help="Path to node.yaml")
    ap.add_argument("--server", default="", help="Override NATS WS URL")
    ap.add_argument("--duration", type=float, default=90.0, help="Run duration in seconds")
    ap.add_argument("--report-every", type=float, default=5.0, help="Diagnostics interval in seconds")
    ap.add_argument("--subject", default="", help="Optional subject to subscribe/publish")
    ap.add_argument("--publish-every", type=float, default=0.0, help="Publish interval to --subject (0 disables)")
    ap.add_argument("--payload", default="diag", help="Publish payload")
    ap.add_argument("--trace", action="store_true", help="Verbose RX/TX tracing")
    args = ap.parse_args()
    try:
        return int(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
