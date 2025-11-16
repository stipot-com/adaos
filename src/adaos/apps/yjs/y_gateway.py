from __future__ import annotations

import asyncio
import json
import time
import logging
from typing import Dict

import y_py as Y
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect
from ypy_websocket.websocket import Websocket as YWebsocket
from ypy_websocket.websocket_server import WebsocketServer
from ypy_websocket.yroom import YRoom
from ypy_websocket.ystore import SQLiteYStore

from adaos.apps.workspaces.index import ensure_workspace
from .y_bootstrap import ensure_webspace_seeded_from_scenario
from .y_store import ystore_path_for_webspace
from adaos.services.weather.observer import ensure_weather_observer

router = APIRouter()
_log = logging.getLogger("adaos.events_ws")


class WorkspaceWebsocketServer(WebsocketServer):
    """
    WebsocketServer that binds each room to a webspace-backed SQLiteYStore.

    We use the websocket path as the webspace id (e.g. "default").
    """

    async def get_room(self, name: str) -> YRoom:  # type: ignore[override]
        webspace_id = name or "default"
        if name not in self.rooms:
            ensure_workspace(webspace_id)
            ystore = SQLiteYStore(str(ystore_path_for_webspace(webspace_id)))
            await ensure_webspace_seeded_from_scenario(ystore, webspace_id=webspace_id)
            room = YRoom(ready=self.rooms_ready, ystore=ystore, log=self.log)
            try:
                await ystore.apply_updates(room.ydoc)
            except Exception:
                # If there is nothing yet, the bootstrap already handled it
                pass
            self.rooms[name] = room
        room = self.rooms[name]
        ensure_weather_observer(webspace_id, room.ydoc)
        await self.start_room(room)
        return room


y_server = WorkspaceWebsocketServer(auto_clean_rooms=False)
_y_server_started = False


async def start_y_server() -> None:
    """
    Ensure the shared Y websocket server background task is running.
    """
    global _y_server_started
    if _y_server_started:
        return
    _y_server_started = True

    async def _runner() -> None:
        await y_server.start()

    asyncio.create_task(_runner(), name="adaos-yjs-websocket-server")
    await y_server.started.wait()


class FastAPIWebsocketAdapter:
    """
    Adapt FastAPI's WebSocket to the minimal protocol expected by ypy-websocket.
    """

    def __init__(self, ws: WebSocket, path: str):
        self._ws = ws
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration()

    async def send(self, message: bytes) -> None:
        try:
            await self._ws.send_bytes(message)
        except WebSocketDisconnect:
            # клиент уже ушёл – тихо выходим
            pass

    async def recv(self) -> bytes:
        msg = await self._ws.receive()
        msg_type = msg.get("type")
        if msg_type == "websocket.receive":
            if msg.get("bytes") is not None:
                return msg["bytes"]
            if msg.get("text") is not None:
                return msg["text"].encode("utf-8")
        if msg_type == "websocket.disconnect":
            raise RuntimeError("websocket disconnected")
        return b""


async def _update_device_presence(webspace_id: str, device_id: str) -> None:
    """
    Project basic device presence into the Yjs doc under devices/<device_id>.
    """
    room = await y_server.get_room(webspace_id)
    ydoc = room.ydoc
    now_ms = int(time.time() * 1000)

    with ydoc.begin_transaction() as txn:
        devices = ydoc.get_map("devices")
        current = devices.get(device_id)
        node = dict(current or {}) if isinstance(current, dict) else {}

        meta = dict(node.get("meta") or {})
        if "created_at" not in meta:
            meta["created_at"] = now_ms
        meta["kind"] = "browser"

        presence = dict(node.get("presence") or {})
        presence["online"] = True
        presence.setdefault("since", now_ms)
        presence["lastSeen"] = now_ms

        node["meta"] = meta
        node["presence"] = presence

        devices.set(txn, device_id, node)


async def _yws_impl(websocket: WebSocket, room: str | None) -> None:
    """
    Internal Yjs sync handler used by both /yws and /yws/<room> routes.

    Dev policy:
      - if a room segment is present in the path, it is treated as webspace_id;
      - otherwise, fallback to ?ws=<webspace_id> query param;
      - default is "default".
    """
    params: Dict[str, str] = dict(websocket.query_params)
    webspace_id = (room or params.get("ws")) or "default"

    await websocket.accept()
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=webspace_id)
    try:
        await y_server.serve(adapter)
    except RuntimeError:
        # Normal disconnect / shutdown
        return


@router.websocket("/yws")
async def yws(websocket: WebSocket):
    """
    Binary Yjs sync endpoint backed by ypy-websocket.

    Frontend connects via y-websocket with:
      ws://host:port/yws/<webspace_id>?dev=<device_id>
    """
    await _yws_impl(websocket, room=None)


@router.websocket("/yws/{room:path}")
async def yws_room(websocket: WebSocket, room: str):
    """
    Route compatible with y-websocket default URL pattern:
      ws://host:port/yws/<webspace_id>?dev=<device_id>
    """
    await _yws_impl(websocket, room=room)


@router.websocket("/ws")
async def events_ws(websocket: WebSocket):
    """
    JSON events websocket.

    Implements device.register in dev-mode and returns a webspace_id.
    """
    await websocket.accept()

    device_id: str | None = None
    webspace_id = "default"

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            ch = msg.get("ch")
            t = msg.get("t")
            if ch != "events" or t != "cmd":
                continue

            cmd_id = msg.get("id")
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if kind == "device.register":
                device_id = payload.get("device_id") or "dev-unknown"
                # Dev-only policy: single webspace "default" for now
                webspace_id = "default"

                try:
                    ensure_workspace(webspace_id)
                    await start_y_server()
                    await _update_device_presence(webspace_id, device_id)

                    await websocket.send_text(
                        json.dumps(
                            {
                                "ch": "events",
                                "t": "ack",
                                "id": cmd_id,
                                "ok": True,
                                "data": {"webspace_id": webspace_id},
                            }
                        )
                    )
                    _log.debug("device.register acknowledged webspace=%s device=%s", webspace_id, device_id)
                except Exception:
                    # In dev mode we still ack, but without guaranteeing presence.
                    await websocket.send_text(
                        json.dumps(
                            {
                                "ch": "events",
                                "t": "ack",
                                "id": cmd_id,
                                "ok": True,
                                "data": {"webspace_id": webspace_id},
                            }
                        )
                    )
                continue

            if kind == "desktop.toggleInstall":
                # Command to toggle app/widget installation in the current webspace.
                try:
                    from adaos.domain import Event as _Ev
                    from adaos.services.agent_context import get_ctx as _get_ctx

                    ctx = _get_ctx()
                    ev = _Ev(
                        type="desktop.toggleInstall",
                        payload={
                            "webspace_id": webspace_id,
                            "type": payload.get("type"),
                            "id": payload.get("id"),
                        },
                        source="events_ws",
                        ts=time.time(),
                    )
                    ctx.bus.publish(ev)
                    _log.debug("desktop.toggleInstall webspace=%s type=%s id=%s", webspace_id, payload.get("type"), payload.get("id"))
                except Exception:
                    pass

                await websocket.send_text(
                    json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True})
                )
                continue

            # Default ack for other commands (no-op for now)
            await websocket.send_text(
                json.dumps(
                    {
                        "ch": "events",
                        "t": "ack",
                        "id": cmd_id,
                        "ok": True,
                    }
                )
            )
    finally:
        # Basic offline marking could be added later if needed
        _ = device_id
