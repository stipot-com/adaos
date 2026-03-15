from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import WebSocket

from adaos.domain import Event as DomainEvent
from adaos.services.agent_context import get_ctx
from adaos.services.yjs.doc import apply_update_to_live_room
from adaos.services.yjs.store import get_ystore_for_webspace, suppress_ystore_write_notifications

_log = logging.getLogger("adaos.subnet.link")


@dataclass
class HubMemberLink:
    node_id: str
    websocket: WebSocket
    hostname: str | None = None
    roles: list[str] = field(default_factory=list)
    connected_at: float = field(default_factory=lambda: time.time())
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_rpc: Dict[str, asyncio.Future] = field(default_factory=dict)

    async def send_json(self, msg: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(msg)


class HubLinkManager:
    """
    Hub-side manager for member WebSocket links.

    Responsibilities:
    - Track online members connected via `/ws/subnet`
    - Provide RPC (hub -> member) used by tool routing
    - Relay Yjs updates between members and the hub's YStore
    - Ingest selected bus events (member -> hub)
    """

    def __init__(self) -> None:
        self._links: dict[str, HubMemberLink] = {}
        self._lock = asyncio.Lock()

    async def register(self, node_id: str, ws: WebSocket, *, hostname: str | None, roles: list[str] | None) -> HubMemberLink:
        link = HubMemberLink(node_id=node_id, websocket=ws, hostname=hostname, roles=list(roles or []))
        async with self._lock:
            # replace existing link if reconnecting
            prev = self._links.get(node_id)
            self._links[node_id] = link
        if prev is not None:
            try:
                for rid, fut in list(prev.pending_rpc.items()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("link_replaced"))
            except Exception:
                pass
        return link

    async def unregister(self, node_id: str) -> None:
        async with self._lock:
            link = self._links.pop(node_id, None)
        if not link:
            return
        try:
            for rid, fut in list(link.pending_rpc.items()):
                if not fut.done():
                    fut.set_exception(ConnectionError("link_closed"))
        except Exception:
            pass

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._links

    async def _get_link(self, node_id: str) -> HubMemberLink | None:
        async with self._lock:
            return self._links.get(node_id)

    async def handle_rpc_response(self, node_id: str, msg: dict[str, Any]) -> bool:
        rid = msg.get("id")
        if not isinstance(rid, str) or not rid:
            return False
        link = await self._get_link(node_id)
        if not link:
            return False
        fut = link.pending_rpc.pop(rid, None)
        if not fut:
            return False
        if fut.done():
            return True
        ok = bool(msg.get("ok", False))
        if ok:
            fut.set_result(msg.get("result"))
        else:
            err = msg.get("error") or "rpc_failed"
            fut.set_exception(RuntimeError(str(err)))
        return True

    async def rpc_tools_call(self, node_id: str, *, tool: str, arguments: dict[str, Any] | None, timeout: float | None, dev: bool) -> Any:
        link = await self._get_link(node_id)
        if not link:
            raise ConnectionError("member_not_connected")
        rid = f"rpc_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        link.pending_rpc[rid] = fut
        await link.send_json(
            {
                "t": "rpc.req",
                "id": rid,
                "method": "tools.call",
                "params": {
                    "tool": tool,
                    "arguments": arguments or {},
                    "timeout": timeout,
                    "dev": bool(dev),
                },
            }
        )
        try:
            if timeout is None:
                return await asyncio.wait_for(fut, timeout=30.0)
            return await asyncio.wait_for(fut, timeout=float(timeout) + 5.0)
        finally:
            link.pending_rpc.pop(rid, None)

    async def broadcast_yjs_update(self, *, webspace_id: str, update: bytes, origin_node_id: str | None) -> None:
        """
        Broadcast an update to all connected members except the origin.
        """
        if not update:
            return
        b64 = base64.b64encode(update).decode("ascii")
        async with self._lock:
            links = list(self._links.values())
        for link in links:
            if origin_node_id and link.node_id == origin_node_id:
                continue
            try:
                await link.send_json(
                    {
                        "t": "yjs.update",
                        "webspace_id": webspace_id,
                        "update_b64": b64,
                        "origin_node_id": origin_node_id,
                        "ts": time.time(),
                    }
                )
            except Exception:
                # best-effort
                continue

    async def ingest_member_yjs_update(self, *, node_id: str, webspace_id: str, update: bytes) -> None:
        """
        Apply member-provided Yjs update to hub, then fan it out to other members.
        """
        if not update:
            return
        store = get_ystore_for_webspace(webspace_id)
        async with suppress_ystore_write_notifications():
            await store.write(update)
        apply_update_to_live_room(webspace_id, update)
        await self.broadcast_yjs_update(webspace_id=webspace_id, update=update, origin_node_id=node_id)

    async def ingest_member_bus_event(self, *, node_id: str, event: dict[str, Any]) -> None:
        """
        Publish a member event on hub local bus so hub router/UI can react.
        """
        try:
            typ = event.get("type")
            payload = event.get("payload") or {}
            source = event.get("source") or "subnet.member"
            ts = float(event.get("ts") or time.time())
            if not isinstance(typ, str) or not typ:
                return
            if not isinstance(payload, dict):
                payload = {"value": payload}
            meta = payload.get("_meta") if isinstance(payload, dict) else None
            if not isinstance(meta, dict):
                meta = {}
            payload["_meta"] = {**meta, "subnet_origin_node_id": node_id}
            get_ctx().bus.publish(DomainEvent(type=typ, payload=payload, source=str(source), ts=ts))
        except Exception:
            _log.debug("failed to ingest member bus event node_id=%s", node_id, exc_info=True)


_HUB_MANAGER: HubLinkManager | None = None


def get_hub_link_manager() -> HubLinkManager:
    global _HUB_MANAGER
    if _HUB_MANAGER is None:
        _HUB_MANAGER = HubLinkManager()
    return _HUB_MANAGER

