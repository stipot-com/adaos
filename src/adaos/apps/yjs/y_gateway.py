from __future__ import annotations

import asyncio
import json
import time
from typing import Dict

import y_py as Y
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect
from ypy_websocket.websocket import Websocket as YWebsocket
from ypy_websocket.websocket_server import WebsocketServer
from ypy_websocket.yroom import YRoom
from ypy_websocket.ystore import SQLiteYStore

from adaos.apps.workspaces.index import ensure_workspace
from .y_bootstrap import bootstrap_seed_if_empty
from .y_store import ystore_path_for_workspace

router = APIRouter()


class WorkspaceWebsocketServer(WebsocketServer):
    """
    WebsocketServer that binds each room to a workspace-backed SQLiteYStore.

    We use the websocket path as the workspace id (e.g. "default").
    """

    async def get_room(self, name: str) -> YRoom:  # type: ignore[override]
        if name not in self.rooms:
            workspace_id = name or "default"
            ensure_workspace(workspace_id)
            ystore = SQLiteYStore(str(ystore_path_for_workspace(workspace_id)))
            await bootstrap_seed_if_empty(ystore)
            room = YRoom(ready=self.rooms_ready, ystore=ystore, log=self.log)
            try:
                await ystore.apply_updates(room.ydoc)
            except Exception:
                # If there is nothing yet, the bootstrap already handled it
                pass
            self.rooms[name] = room
        room = self.rooms[name]
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
        await self._ws.send_bytes(message)

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


async def _update_device_presence(workspace_id: str, device_id: str) -> None:
    """
    Project basic device presence into the Yjs doc under devices/<device_id>.
    """
    room = await y_server.get_room(workspace_id)
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


async def _yws_impl(websocket: WebSocket) -> None:
    """
    Internal Yjs sync handler used by both /yws and /yws/<room> routes.
    """
    params: Dict[str, str] = dict(websocket.query_params)
    workspace_id = params.get("ws") or "default"

    await websocket.accept()
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=workspace_id)
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
      ws://host:port/yws?ws=<workspace_id>&dev=<device_id>
    """
    await _yws_impl(websocket)


@router.websocket("/yws/{room:path}")
async def yws_room(websocket: WebSocket, room: str):
    """
    Compatibility route for y-websocket default URL pattern, which appends
    the room name after the base path, e.g. /yws/desktop?ws=<workspace>&dev=..
    """
    _ = room  # room name is not used; workspace is driven by ?ws=..
    await _yws_impl(websocket)


@router.websocket("/ws")
async def events_ws(websocket: WebSocket):
    """
    JSON events websocket.

    Implements device.register in dev-mode and returns a workspace_id.
    """
    await websocket.accept()

    device_id: str | None = None
    workspace_id = "default"

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
                # Dev-only policy: single workspace "default" for now
                workspace_id = "default"

                try:
                    ensure_workspace(workspace_id)
                    await start_y_server()
                    await _update_device_presence(workspace_id, device_id)

                    await websocket.send_text(
                        json.dumps(
                            {
                                "ch": "events",
                                "t": "ack",
                                "id": cmd_id,
                                "ok": True,
                                "data": {"workspace_id": workspace_id},
                            }
                        )
                    )
                except Exception:
                    # In dev mode we still ack, but without guaranteeing presence.
                    await websocket.send_text(
                        json.dumps(
                            {
                                "ch": "events",
                                "t": "ack",
                                "id": cmd_id,
                                "ok": True,
                                "data": {"workspace_id": workspace_id},
                            }
                        )
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
