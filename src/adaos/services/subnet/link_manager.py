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
    node_names: list[str] = field(default_factory=list)
    connected_at: float = field(default_factory=lambda: time.time())
    last_message_at: float = field(default_factory=lambda: time.time())
    last_hub_event_at: float | None = None
    last_hub_event_type: str | None = None
    last_hub_core_update_state: str | None = None
    last_hub_core_update_action: str | None = None
    last_control_request_id: str | None = None
    last_control_request_at: float | None = None
    last_control_action: str | None = None
    last_control_reason: str | None = None
    last_control_result_at: float | None = None
    last_control_result: dict[str, Any] = field(default_factory=dict)
    last_snapshot_at: float | None = None
    node_snapshot: dict[str, Any] = field(default_factory=dict)
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
        self._hub_event_total = 0
        self._hub_core_update_broadcast_total = 0

    async def register(
        self,
        node_id: str,
        ws: WebSocket,
        *,
        hostname: str | None,
        roles: list[str] | None,
        node_names: list[str] | None = None,
    ) -> HubMemberLink:
        link = HubMemberLink(
            node_id=node_id,
            websocket=ws,
            hostname=hostname,
            roles=list(roles or []),
            node_names=list(node_names or []),
        )
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
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.link.up",
                    payload={
                        "node_id": node_id,
                        "hostname": hostname,
                        "roles": list(roles or []),
                        "node_names": list(node_names or []),
                    },
                    source="subnet.link",
                    ts=time.time(),
                )
            )
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
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.link.down",
                    payload={"node_id": node_id},
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._links

    async def _get_link(self, node_id: str) -> HubMemberLink | None:
        async with self._lock:
            return self._links.get(node_id)

    async def note_member_activity(self, node_id: str, *, message_type: str | None = None) -> None:
        link = await self._get_link(node_id)
        if not link:
            return
        link.last_message_at = time.time()

    async def update_member_metadata(self, node_id: str, *, node_names: list[str] | None = None) -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "error": "member_not_connected"}
        if node_names is not None:
            link.node_names = list(node_names)
        link.last_message_at = time.time()
        payload = {
            "node_id": node_id,
            "node_names": list(link.node_names),
        }
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.meta.changed",
                    payload=payload,
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass
        return {"ok": True, **payload}

    async def update_member_snapshot(self, node_id: str, *, snapshot: dict[str, Any]) -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "error": "member_not_connected"}
        snap = dict(snapshot or {})
        node_names = snap.get("node_names")
        if isinstance(node_names, list):
            link.node_names = [str(item or "").strip() for item in node_names if str(item or "").strip()]
        link.node_snapshot = snap
        link.last_snapshot_at = time.time()
        link.last_message_at = link.last_snapshot_at
        update_status = snap.get("update_status")
        if isinstance(update_status, dict):
            state = str(update_status.get("state") or "").strip()
            action = str(update_status.get("action") or "").strip()
            if state:
                link.last_hub_core_update_state = state
            if action:
                link.last_hub_core_update_action = action
        payload = {
            "node_id": node_id,
            "node_names": list(link.node_names),
            "snapshot": dict(link.node_snapshot),
            "captured_at": link.last_snapshot_at,
        }
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.snapshot.changed",
                    payload=payload,
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass
        return {"ok": True, **payload}

    async def set_member_node_names(self, node_id: str, *, node_names: list[str]) -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "error": "member_not_connected"}
        await link.send_json({"t": "node.names.set", "node_names": list(node_names)})
        return {"ok": True, "node_id": node_id, "node_names": list(node_names)}

    async def request_member_snapshot(self, node_id: str, *, reason: str = "manual_refresh") -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "accepted": False, "error": "member_not_connected", "node_id": node_id}
        await link.send_json(
            {
                "t": "node.snapshot.request",
                "reason": str(reason or "manual_refresh"),
                "ts": time.time(),
            }
        )
        link.last_hub_event_at = time.time()
        link.last_hub_event_type = "node.snapshot.request"
        payload = {
            "node_id": node_id,
            "reason": str(reason or "manual_refresh"),
        }
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.snapshot.requested",
                    payload=payload,
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass
        return {"ok": True, "accepted": True, **payload}

    async def request_member_update(
        self,
        node_id: str,
        *,
        action: str,
        target_rev: str = "",
        target_version: str = "",
        countdown_sec: float | None = None,
        drain_timeout_sec: float | None = None,
        signal_delay_sec: float | None = None,
        reason: str = "hub.member_control",
    ) -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "accepted": False, "error": "member_not_connected", "node_id": node_id}
        action_norm = str(action or "").strip().lower()
        if action_norm == "start":
            action_norm = "update"
        if action_norm not in {"update", "cancel", "rollback", "drain"}:
            return {"ok": False, "accepted": False, "error": "invalid_action", "node_id": node_id, "action": action_norm}
        request_id = f"member_update_{uuid.uuid4().hex}"
        msg = {
            "t": "core.update.request",
            "request_id": request_id,
            "action": action_norm,
            "target_rev": str(target_rev or ""),
            "target_version": str(target_version or ""),
            "reason": str(reason or "hub.member_control"),
            "ts": time.time(),
        }
        if countdown_sec is not None:
            msg["countdown_sec"] = float(countdown_sec)
        if drain_timeout_sec is not None:
            msg["drain_timeout_sec"] = float(drain_timeout_sec)
        if signal_delay_sec is not None:
            msg["signal_delay_sec"] = float(signal_delay_sec)
        await link.send_json(msg)
        link.last_hub_event_at = time.time()
        link.last_hub_event_type = "core.update.request"
        link.last_control_request_id = request_id
        link.last_control_request_at = time.time()
        link.last_control_action = action_norm
        link.last_control_reason = str(reason or "hub.member_control")
        link.last_control_result_at = None
        link.last_control_result = {"ok": None, "state": "requested", "request_id": request_id}
        payload = {
            "node_id": node_id,
            "request_id": request_id,
            "action": action_norm,
            "target_rev": str(target_rev or ""),
            "target_version": str(target_version or ""),
            "reason": str(reason or "hub.member_control"),
        }
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.update.requested",
                    payload=payload,
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass
        return {"ok": True, "accepted": True, **payload}

    async def update_member_control_result(self, node_id: str, *, result: dict[str, Any]) -> dict[str, Any]:
        link = await self._get_link(node_id)
        if not link:
            return {"ok": False, "error": "member_not_connected", "node_id": node_id}
        payload = dict(result or {})
        link.last_control_result_at = time.time()
        link.last_control_result = payload
        request_id = str(payload.get("request_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        if request_id:
            link.last_control_request_id = request_id
        if action:
            link.last_control_action = action
        outbound = {
            "node_id": node_id,
            "result": dict(link.last_control_result),
            "captured_at": link.last_control_result_at,
        }
        try:
            get_ctx().bus.publish(
                DomainEvent(
                    type="subnet.member.update.result",
                    payload=outbound,
                    source="subnet.link",
                    ts=time.time(),
                )
            )
        except Exception:
            pass
        return {"ok": True, **outbound}

    async def broadcast_event(self, *, event_type: str, payload: dict[str, Any], source: str = "hub") -> dict[str, Any]:
        event_type_norm = str(event_type or "").strip()
        if not event_type_norm:
            return {"sent": 0, "failed": 0}
        msg = {
            "t": "hub.event",
            "event": {
                "type": event_type_norm,
                "payload": payload if isinstance(payload, dict) else {"value": payload},
                "source": str(source or "hub"),
                "ts": time.time(),
            },
        }
        async with self._lock:
            links = list(self._links.values())
        sent = 0
        failed = 0
        for link in links:
            try:
                await link.send_json(msg)
                link.last_hub_event_at = time.time()
                link.last_hub_event_type = event_type_norm
                if event_type_norm == "core.update.status":
                    link.last_hub_core_update_state = str((payload or {}).get("state") or "").strip() or None
                    link.last_hub_core_update_action = str((payload or {}).get("action") or "").strip() or None
                sent += 1
            except Exception:
                failed += 1
        self._hub_event_total += sent
        if event_type_norm == "core.update.status":
            self._hub_core_update_broadcast_total += sent
        return {"sent": sent, "failed": failed}

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        items: list[dict[str, Any]] = []
        for link in sorted(self._links.values(), key=lambda item: item.node_id):
            items.append(
                {
                    "node_id": link.node_id,
                    "hostname": link.hostname,
                    "roles": list(link.roles),
                    "node_names": list(link.node_names),
                    "connected_at": link.connected_at,
                    "connected_ago_s": round(max(0.0, now - float(link.connected_at or now)), 3),
                    "last_message_ago_s": round(max(0.0, now - float(link.last_message_at or now)), 3),
                    "last_hub_event_ago_s": (
                        round(max(0.0, now - float(link.last_hub_event_at)), 3)
                        if link.last_hub_event_at
                        else None
                    ),
                    "last_snapshot_ago_s": (
                        round(max(0.0, now - float(link.last_snapshot_at)), 3)
                        if link.last_snapshot_at
                        else None
                    ),
                    "last_hub_event_type": link.last_hub_event_type,
                    "last_hub_core_update_state": link.last_hub_core_update_state,
                    "last_hub_core_update_action": link.last_hub_core_update_action,
                    "last_control_request_id": link.last_control_request_id,
                    "last_control_request_ago_s": (
                        round(max(0.0, now - float(link.last_control_request_at)), 3)
                        if link.last_control_request_at
                        else None
                    ),
                    "last_control_action": link.last_control_action,
                    "last_control_reason": link.last_control_reason,
                    "last_control_result_ago_s": (
                        round(max(0.0, now - float(link.last_control_result_at)), 3)
                        if link.last_control_result_at
                        else None
                    ),
                    "last_control_result": dict(link.last_control_result) if isinstance(link.last_control_result, dict) else {},
                    "node_snapshot": dict(link.node_snapshot) if isinstance(link.node_snapshot, dict) else {},
                    "pending_rpc": len(link.pending_rpc),
                    "connected": True,
                }
            )
        return {
            "role": "hub",
            "member_total": len(items),
            "connected_total": len(items),
            "hub_event_total": self._hub_event_total,
            "hub_core_update_broadcast_total": self._hub_core_update_broadcast_total,
            "members": items,
            "updated_at": now,
        }

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


def hub_link_manager_snapshot() -> dict[str, Any]:
    return get_hub_link_manager().snapshot()
