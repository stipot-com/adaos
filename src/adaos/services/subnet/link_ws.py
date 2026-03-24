from __future__ import annotations

import base64
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from adaos.services.agent_context import get_ctx
from adaos.services.subnet.link_manager import get_hub_link_manager

router = APIRouter()
_log = logging.getLogger("adaos.subnet.ws")


def _extract_token(websocket: WebSocket) -> str | None:
    try:
        auth = websocket.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip()
    except Exception:
        pass
    try:
        tok = websocket.headers.get("x-adaos-token")
        if tok:
            return tok.strip()
    except Exception:
        pass
    try:
        return (websocket.query_params.get("token") or "").strip() or None
    except Exception:
        return None


@router.websocket("/ws/subnet")
async def subnet_ws(websocket: WebSocket) -> None:
    """
    Member -> Hub persistent link (P2P in-subnet).

    Auth: X-AdaOS-Token header (or Authorization: Bearer).
    First message must be `{"t":"hello", ...}`.
    """
    conf = get_ctx().config
    if conf.role != "hub":
        await websocket.close(code=1008)
        return

    # Accept first; we can still close with 1008 on auth failure.
    await websocket.accept()

    token = _extract_token(websocket)
    expected = conf.token or "dev-local-token"
    if token != expected:
        await websocket.send_json({"t": "hello.ack", "ok": False, "error": "invalid_token"})
        await websocket.close(code=1008)
        return

    mgr = get_hub_link_manager()
    node_id: str | None = None
    try:
        raw = await websocket.receive_json()
        if not isinstance(raw, dict) or raw.get("t") != "hello":
            await websocket.send_json({"t": "hello.ack", "ok": False, "error": "hello_required"})
            await websocket.close(code=1002)
            return
        node_id = str(raw.get("node_id") or "").strip()
        subnet_id = str(raw.get("subnet_id") or "").strip()
        if not node_id or not subnet_id:
            await websocket.send_json({"t": "hello.ack", "ok": False, "error": "node_id_and_subnet_id_required"})
            await websocket.close(code=1002)
            return
        if subnet_id != conf.subnet_id:
            await websocket.send_json({"t": "hello.ack", "ok": False, "error": "subnet_mismatch"})
            await websocket.close(code=1008)
            return
        hostname = raw.get("hostname")
        roles = raw.get("roles") or []
        node_names = raw.get("node_names") or []
        link = await mgr.register(
            node_id,
            websocket,
            hostname=str(hostname) if hostname else None,
            roles=list(roles) if isinstance(roles, list) else [],
            node_names=list(node_names) if isinstance(node_names, list) else [],
        )
        await link.send_json(
            {"t": "hello.ack", "ok": True, "hub_node_id": conf.node_id, "subnet_id": conf.subnet_id, "server_time": time.time()}
        )

        while True:
            try:
                msg: Any = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue

            t = msg.get("t")
            try:
                await mgr.note_member_activity(node_id, message_type=str(t or ""))
            except Exception:
                pass
            if t == "ping":
                try:
                    await link.send_json({"t": "pong", "ts": time.time()})
                except Exception:
                    pass
                continue

            if t == "rpc.res":
                try:
                    await mgr.handle_rpc_response(node_id, msg)
                except Exception:
                    pass
                continue

            if t == "bus.emit":
                ev = msg.get("event")
                if isinstance(ev, dict):
                    await mgr.ingest_member_bus_event(node_id=node_id, event=ev)
                continue

            if t == "node.meta":
                node_names = msg.get("node_names") or []
                if isinstance(node_names, list):
                    await mgr.update_member_metadata(node_id, node_names=list(node_names))
                continue

            if t == "node.snapshot":
                snapshot = msg.get("snapshot")
                if isinstance(snapshot, dict):
                    await mgr.update_member_snapshot(node_id, snapshot=snapshot)
                continue

            if t == "yjs.update":
                try:
                    webspace_id = str(msg.get("webspace_id") or "default")
                    b64 = msg.get("update_b64") or ""
                    if not isinstance(b64, str) or not b64:
                        continue
                    update = base64.b64decode(b64.encode("ascii"), validate=False)
                    await mgr.ingest_member_yjs_update(node_id=node_id, webspace_id=webspace_id, update=update)
                except Exception:
                    continue
                continue

            _ = link
    finally:
        if node_id:
            try:
                await mgr.unregister(node_id)
            except Exception:
                pass
