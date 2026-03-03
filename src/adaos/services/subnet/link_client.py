from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Callable

import websockets  # type: ignore

from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.capacity import get_local_capacity
from adaos.services.skill.manager import SkillManager
from adaos.services.yjs.doc import apply_update_to_live_room
from adaos.services.yjs.store import add_ystore_write_listener, get_ystore_for_webspace, suppress_ystore_write_notifications

_log = logging.getLogger("adaos.subnet.client")


def _to_ws_url(http_base: str, path: str) -> str:
    u = urllib.parse.urlparse(str(http_base or "").strip())
    if u.scheme in ("http", "https"):
        scheme = "wss" if u.scheme == "https" else "ws"
        netloc = u.netloc
        base_path = u.path
    else:
        # tolerate bare host:port or host
        scheme = "ws"
        netloc = u.path
        base_path = ""
    full_path = (base_path.rstrip("/") + "/" + path.lstrip("/")).rstrip("/")
    return urllib.parse.urlunparse((scheme, netloc, full_path, "", "", ""))


class MemberLinkClient:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self._task: asyncio.Task | None = None
        self._remove_ystore_listener: Callable[[], None] | None = None
        self._bus_subscribed = False
        self._yjs_enabled = os.getenv("ADAOS_SUBNET_YJS_REPLICATION", "1").strip().lower() not in ("0", "false", "no")
        self._bus_prefixes = self._parse_bus_prefixes(os.getenv("ADAOS_SUBNET_BUS_FORWARD_PREFIXES", "io.out.,ui."))

    @staticmethod
    def _parse_bus_prefixes(raw: str | None) -> list[str] | None:
        txt = str(raw or "").strip()
        if not txt:
            return ["io.out.", "ui."]
        if txt in ("*", "all"):
            return None
        parts = [p.strip() for p in txt.split(",") if p.strip()]
        return parts or ["io.out.", "ui."]

    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="subnet-link-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        try:
            if self._remove_ystore_listener:
                self._remove_ystore_listener()
        except Exception:
            pass

    def _install_ystore_listener(self) -> None:
        if not self._yjs_enabled:
            return
        if self._remove_ystore_listener:
            return

        def _on_write(webspace_id: str, update: bytes) -> None:
            if not update:
                return
            if not self._connected.is_set():
                return
            try:
                b64 = base64.b64encode(update).decode("ascii")
            except Exception:
                return
            msg = {
                "t": "yjs.update",
                "webspace_id": webspace_id or "default",
                "update_b64": b64,
                "ts": time.time(),
            }
            try:
                self._out_q.put_nowait(msg)
            except Exception:
                return

        self._remove_ystore_listener = add_ystore_write_listener(_on_write)

    def _ensure_bus_subscription(self) -> None:
        if self._bus_subscribed:
            return

        def _on_ev(ev: Any) -> None:
            # Forward only a small subset; expand via env later if needed.
            try:
                if not self._connected.is_set():
                    return
                typ = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
                if not isinstance(typ, str) or not typ:
                    return
                if self._bus_prefixes is not None and not any(typ.startswith(p) for p in self._bus_prefixes):
                    return
                payload = getattr(ev, "payload", None) if hasattr(ev, "payload") else (ev.get("payload") if isinstance(ev, dict) else None)
                source = getattr(ev, "source", None) if hasattr(ev, "source") else (ev.get("source") if isinstance(ev, dict) else None)
                ts = getattr(ev, "ts", None) if hasattr(ev, "ts") else (ev.get("ts") if isinstance(ev, dict) else None)
                msg = {
                    "t": "bus.emit",
                    "event": {
                        "type": typ,
                        "payload": payload if isinstance(payload, dict) else {"value": payload},
                        "source": str(source or "member"),
                        "ts": float(ts or time.time()),
                    },
                }
                self._out_q.put_nowait(msg)
            except Exception:
                return

        try:
            get_ctx().bus.subscribe("*", _on_ev)
            self._bus_subscribed = True
        except Exception:
            pass

    async def _run(self) -> None:
        conf = get_ctx().config
        if conf.role != "member":
            return
        if not conf.hub_url:
            _log.warning("subnet link: hub_url is not set for member")
            return

        self._install_ystore_listener()
        self._ensure_bus_subscription()

        ws_url = _to_ws_url(conf.hub_url, "/ws/subnet")
        headers = [("X-AdaOS-Token", conf.token or "dev-local-token")]

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    max_size=None,
                    ping_interval=None,
                ) as ws:
                    self._connected.set()
                    backoff = 1.0

                    hello = {
                        "t": "hello",
                        "node_id": conf.node_id,
                        "subnet_id": conf.subnet_id,
                        "hostname": None,
                        "roles": ["member"],
                        "base_url": None,
                        "capacity": get_local_capacity(),
                    }
                    await ws.send(json.dumps(hello))
                    try:
                        _ = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except Exception:
                        pass

                    async def _sender() -> None:
                        while True:
                            msg = await self._out_q.get()
                            try:
                                await ws.send(json.dumps(msg))
                            except Exception:
                                return

                    async def _receiver() -> None:
                        while True:
                            raw = await ws.recv()
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if not isinstance(msg, dict):
                                continue
                            t = msg.get("t")
                            if t == "yjs.update":
                                if self._yjs_enabled:
                                    await self._on_yjs_update(msg)
                                continue
                            if t == "rpc.req":
                                await self._on_rpc(ws, msg)
                                continue

                    sender_t = asyncio.create_task(_sender(), name="subnet-link-sender")
                    receiver_t = asyncio.create_task(_receiver(), name="subnet-link-receiver")
                    ping_t = asyncio.create_task(self._ping_loop(ws), name="subnet-link-ping")
                    done, pending = await asyncio.wait([sender_t, receiver_t, ping_t], return_when=asyncio.FIRST_COMPLETED)
                    for p in pending:
                        p.cancel()
                    for d in done:
                        _ = d
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.debug("subnet link connect failed ws=%s err=%s", ws_url, exc)
            finally:
                self._connected.clear()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 15.0)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(10.0)
            try:
                await ws.send(json.dumps({"t": "ping", "ts": time.time()}))
            except Exception:
                return

    async def _on_yjs_update(self, msg: dict[str, Any]) -> None:
        try:
            ws_id = str(msg.get("webspace_id") or "default")
            b64 = str(msg.get("update_b64") or "")
            if not b64:
                return
            upd = base64.b64decode(b64.encode("ascii"), validate=False)
            store = get_ystore_for_webspace(ws_id)
            async with suppress_ystore_write_notifications():
                await store.write(upd)
            apply_update_to_live_room(ws_id, upd)
        except Exception:
            return

    async def _on_rpc(self, ws, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(rid, str) or not rid:
            return
        if method != "tools.call":
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": "unknown_method"}))
            return

        tool = (params or {}).get("tool")
        arguments = (params or {}).get("arguments") or {}
        timeout = (params or {}).get("timeout")
        dev = bool((params or {}).get("dev", False))
        if not isinstance(tool, str) or ":" not in tool:
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": "invalid_tool"}))
            return

        try:
            result = await asyncio.to_thread(self._run_tool, tool, arguments, timeout, dev)
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": True, "result": result}))
        except Exception as exc:
            await ws.send(json.dumps({"t": "rpc.res", "id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}))

    @staticmethod
    def _run_tool(tool: str, arguments: dict[str, Any], timeout: Any, dev: bool) -> Any:
        ctx = get_ctx()
        skill_name, public_tool = tool.split(":", 1)
        mgr = SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
            settings=ctx.settings,
        )
        if dev:
            return mgr.run_dev_tool(skill_name, public_tool, arguments or {}, timeout=timeout)
        return mgr.run_tool(skill_name, public_tool, arguments or {}, timeout=timeout)


_MEMBER_CLIENT: MemberLinkClient | None = None


def get_member_link_client() -> MemberLinkClient:
    global _MEMBER_CLIENT
    if _MEMBER_CLIENT is None:
        _MEMBER_CLIENT = MemberLinkClient()
    return _MEMBER_CLIENT
