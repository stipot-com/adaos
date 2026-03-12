import argparse
import asyncio
import json
import os
import random
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


def _rand_id(n: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _load_node_yaml(path: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError(f"PyYAML not available: {type(e).__name__}: {e}") from e

    with open(path, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}
    if not isinstance(y, dict):
        raise RuntimeError(f"unexpected node.yaml type: {type(y).__name__}")
    return y


def _pick_hub_id(node: dict[str, Any]) -> str:
    try:
        hub_id = str(node.get("subnet_id") or node.get("hub_id") or "").strip()
        return hub_id
    except Exception:
        return ""


def _pick_root_base(node: dict[str, Any]) -> str:
    try:
        root = node.get("root") if isinstance(node.get("root"), dict) else {}
        base = str(root.get("base_url") or "").strip()
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    return "https://api.inimatic.com"


def _http_get(url: str, *, session_jwt: str | None) -> tuple[int, bytes]:
    headers = {"user-agent": "adaos-diag-root-hub-proxy/1"}
    if session_jwt:
        headers["authorization"] = f"Bearer {session_jwt}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            data = resp.read() or b""
            return status, data
    except urllib.error.HTTPError as e:  # non-2xx
        try:
            data = e.read() or b""
        except Exception:
            data = b""
        return int(getattr(e, "code", 0) or 0), data


async def _ws_probe(url: str) -> tuple[bool, str]:
    """
    Open and immediately close a WS connection.
    Returns (ok, details).
    """
    try:
        import websockets  # type: ignore
    except Exception as e:
        return False, f"websockets import failed: {type(e).__name__}: {e}"

    try:
        async with websockets.connect(url, open_timeout=7, close_timeout=2, ping_interval=20, ping_timeout=20) as ws:
            try:
                # Some servers don't like immediate close before the handshake settles; give it a tiny tick.
                await asyncio.sleep(0.05)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _run(*, root_base: str, hub_id: str, session_jwt: str | None, ws_kind: str, ws_room: str) -> int:
    root_base = (root_base or "").rstrip("/")
    hub_id = str(hub_id or "").strip()
    if not root_base:
        print("[diag] error: root base URL is empty", file=sys.stderr)
        return 2
    if not hub_id:
        print("[diag] error: hub_id is empty", file=sys.stderr)
        return 2

    def _u(path: str) -> str:
        return f"{root_base}{path}"

    # 1) Unauthenticated reachability probes (do not prove hub connectivity)
    ping_url = _u(f"/hubs/{urllib.parse.quote(hub_id)}/api/ping")
    healthz_url = _u(f"/hubs/{urllib.parse.quote(hub_id)}/healthz")
    for name, url in [("api/ping", ping_url), ("healthz", healthz_url)]:
        st, body = _http_get(url, session_jwt=None)
        tail = body[:240].decode("utf-8", errors="replace").replace("\n", "\\n").replace("\r", "\\r")
        print(f"[diag] root proxy {name}: status={st} body_head={tail!r}")

    # 2) Authenticated hub status probe (proves Root<->Hub route proxy over NATS works)
    status_url = _u(f"/hubs/{urllib.parse.quote(hub_id)}/api/node/status")
    if session_jwt:
        st, body = _http_get(status_url, session_jwt=session_jwt)
        tail = body[:500].decode("utf-8", errors="replace").replace("\n", "\\n").replace("\r", "\\r")
        print(f"[diag] hub status via root: status={st} body_head={tail!r}")
    else:
        print("[diag] hub status via root: skipped (no --session-jwt provided)")

    # 3) WS upgrade probe (requires session token)
    if session_jwt:
        ws_kind = str(ws_kind or "yws").strip() or "yws"
        ws_room = ("/" + str(ws_room).lstrip("/")) if str(ws_room).strip() else ""
        ws_url = root_base.replace("https://", "wss://").replace("http://", "ws://")
        query = urllib.parse.urlencode(
            {
                "token": session_jwt,
                "dev": f"diag_{_rand_id(8)}",
            }
        )
        ws_full = f"{ws_url}/hubs/{urllib.parse.quote(hub_id)}/{ws_kind}{ws_room}?{query}"
        ok, details = await _ws_probe(ws_full)
        print(f"[diag] ws upgrade via root ({ws_kind}{ws_room}): ok={int(ok)} details={details}")
    else:
        print("[diag] ws upgrade via root: skipped (no --session-jwt provided)")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose browser->hub connectivity through Root (/hubs/<hubId>/...).")
    ap.add_argument("--node", default=os.getenv("ADAOS_NODE_YAML", ".adaos/node.yaml"), help="Path to node.yaml")
    ap.add_argument("--root", default="", help="Root base URL (default: from node.yaml root.base_url or https://api.inimatic.com)")
    ap.add_argument("--hub-id", default="", help="Hub id (default: from node.yaml subnet_id)")
    ap.add_argument(
        "--session-jwt",
        default=os.getenv("ADAOS_SESSION_JWT", ""),
        help="Owner session JWT for authenticated /hubs/... requests and WS upgrades",
    )
    ap.add_argument("--ws-kind", default="yws", choices=["ws", "yws"], help="Which WS proxy path to probe")
    ap.add_argument("--ws-room", default="te1", help="Room suffix for ws kind (e.g. te1)")
    args = ap.parse_args()

    node: dict[str, Any] = {}
    try:
        if args.node:
            node = _load_node_yaml(str(args.node))
    except Exception as e:
        print(f"[diag] warning: cannot read node.yaml: {type(e).__name__}: {e}", file=sys.stderr)
        node = {}

    root_base = str(args.root or "").strip() or _pick_root_base(node)
    hub_id = str(args.hub_id or "").strip() or _pick_hub_id(node)
    session_jwt = str(args.session_jwt or "").strip() or None

    try:
        return int(
            asyncio.run(
                _run(
                    root_base=root_base,
                    hub_id=hub_id,
                    session_jwt=session_jwt,
                    ws_kind=str(args.ws_kind or "yws"),
                    ws_room=str(args.ws_room or "te1"),
                )
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

