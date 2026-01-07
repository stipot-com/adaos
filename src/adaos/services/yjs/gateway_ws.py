from __future__ import annotations

"""
Yjs websocket gateway implementation (service layer).
"""

import asyncio
import json
import time
import logging
import threading
from typing import Dict, Any

import y_py as Y
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect
try:
    from ypy_websocket.websocket import Websocket as YWebsocket
    from ypy_websocket.websocket_server import WebsocketServer
    from ypy_websocket.yroom import YRoom
except ImportError as exc:  # pragma: no cover - import guard for dev envs
    raise RuntimeError(
        "ypy_websocket is required for AdaOS realtime collaboration. "
        "Install dependencies via `pip install -e .[dev]` or `pip install ypy-websocket`."
    ) from exc

from adaos.services.workspaces import ensure_workspace, get_workspace
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.scheduler import get_scheduler
from adaos.services.yjs.observers import attach_room_observers
from adaos.domain import Event as DomainEvent
from adaos.services.agent_context import get_ctx as get_agent_ctx

router = APIRouter()
_log = logging.getLogger("adaos.events_ws")
_ylog = logging.getLogger("adaos.yjs.gateway")


class WorkspaceWebsocketServer(WebsocketServer):
    """
    WebsocketServer that binds each room to a webspace-backed SQLiteYStore.

    We use the websocket path as the webspace id (e.g. "default").
    """

    async def get_room(self, name: str) -> YRoom:  # type: ignore[override]
        webspace_id = name or "default"

        def _is_dev_space(ws_id: str) -> bool:
            try:
                row = get_workspace(ws_id)
                if not row or not row.display_name:
                    return False
                title = row.display_name.strip()
                return title.upper().startswith("DEV:")
            except Exception:
                return False

        if name not in self.rooms:
            _ylog.info("creating YRoom for webspace=%s", webspace_id)
            ensure_workspace(webspace_id)
            ystore = get_ystore_for_webspace(webspace_id)
            space = "dev" if _is_dev_space(webspace_id) else "workspace"
            await ensure_webspace_seeded_from_scenario(ystore, webspace_id=webspace_id, space=space)
            # Ensure periodic in-memory snapshotting for this webspace.
            try:
                sched = get_scheduler()
                await sched.ensure_every(
                    name=f"ystores.backup.{webspace_id}",
                    interval=6000.0,
                    topic="sys.ystore.backup",
                    payload={"webspace_id": webspace_id},
                )
            except Exception:
                _ylog.warning("failed to register YStore backup job for webspace=%s", webspace_id, exc_info=True)
            room = YRoom(ready=self.rooms_ready, ystore=ystore, log=self.log)
            room._thread_id = threading.get_ident()
            room._loop = asyncio.get_running_loop()
            try:
                await ystore.apply_updates(room.ydoc)
            except BaseException:
                _ylog.warning("apply_updates failed for webspace=%s", webspace_id, exc_info=True)
            self.rooms[name] = room
        room = self.rooms[name]
        room._thread_id = getattr(room, "_thread_id", threading.get_ident())
        room._loop = getattr(room, "_loop", asyncio.get_running_loop())
        try:
            attach_room_observers(webspace_id, room.ydoc)
        except Exception:
            _ylog.warning("attach_room_observers failed for webspace=%s", webspace_id, exc_info=True)
        await self.start_room(room)
        try:
            ui_map = room.ydoc.get_map("ui")
            data_map = room.ydoc.get_map("data")
            _ylog.debug(
                "YRoom ready webspace=%s ui keys=%s data keys=%s",
                webspace_id,
                list(ui_map.keys()),
                list(data_map.keys()),
            )
        except Exception:
            _ylog.warning("failed to inspect YDoc for webspace=%s", webspace_id, exc_info=True)
        return room


y_server = WorkspaceWebsocketServer(auto_clean_rooms=False)
_y_server_started = False
_y_server_task: asyncio.Task[None] | None = None


async def start_y_server() -> None:
    """
    Ensure the shared Y websocket server background task is running.
    """
    global _y_server_started, _y_server_task
    if _y_server_started:
        return
    _y_server_started = True

    async def _runner() -> None:
        await y_server.start()

    _y_server_task = asyncio.create_task(_runner(), name="adaos-yjs-websocket-server")
    await y_server.started.wait()


async def stop_y_server() -> None:
    """
    Stop the shared Y websocket server background task.

    Without an explicit stop, the anyio task group inside ypy-websocket can
    keep the process alive after FastAPI/uvicorn shutdown.
    """
    global _y_server_started, _y_server_task
    if not _y_server_started:
        return
    try:
        y_server.stop()
    except Exception:
        pass
    task = _y_server_task
    _y_server_task = None
    _y_server_started = False
    if task is None:
        return
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # shutdown path: ignore
        pass


async def ensure_webspace_ready(webspace_id: str, scenario_id: str | None = None) -> None:
    ensure_workspace(webspace_id)
    ystore = get_ystore_for_webspace(webspace_id)

    def _is_dev_space(ws_id: str) -> bool:
        try:
            row = get_workspace(ws_id)
            if not row or not row.display_name:
                return False
            title = row.display_name.strip()
            return title.upper().startswith("DEV:")
        except Exception:
            return False

    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=scenario_id or "web_desktop",
            space="dev" if _is_dev_space(webspace_id) else "workspace",
        )
    finally:
        try:
            await ystore.stop()
        except Exception:
            pass


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
        except (WebSocketDisconnect, RuntimeError):
            # клиент уже ушёл – тихо выходим
            return

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
    dev_id = params.get("dev") or "unknown"

    _ylog.info("yws connection open webspace=%s dev=%s", webspace_id, dev_id)
    await websocket.accept()
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=webspace_id)
    try:
        await y_server.serve(adapter)
    except RuntimeError:
        return
    finally:
        _ylog.info("yws connection closed webspace=%s dev=%s", webspace_id, dev_id)


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

    def _publish_bus(topic: str, extra: Dict[str, Any] | None = None) -> None:
        data = dict(extra or {})
        # Prefer an explicit webspace_id from the payload (UI commands) when
        # present, otherwise fall back to the connection-scoped webspace_id.
        effective_ws = str(data.get("webspace_id") or webspace_id)
        data.setdefault("webspace_id", effective_ws)
        meta = dict(data.get("_meta") or {})
        meta.setdefault("webspace_id", effective_ws)
        if device_id:
            meta.setdefault("device_id", device_id)
        data["_meta"] = meta
        try:
            ctx = get_agent_ctx()
            ev = DomainEvent(type=topic, payload=data, source="events_ws", ts=time.time())
            ctx.bus.publish(ev)
        except Exception:
            _log.warning("failed to publish %s", topic, exc_info=True)

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
                requested_webspace = payload.get("webspace_id") or payload.get("id") or "default"
                webspace_id = str(requested_webspace or "default")

                async def _post_register() -> None:
                    try:
                        await ensure_webspace_ready(webspace_id)
                        await start_y_server()
                        await _update_device_presence(webspace_id, device_id or "dev-unknown")
                        _log.debug(
                            "device.register post steps ok webspace=%s device=%s",
                            webspace_id,
                            device_id,
                        )
                    except Exception:
                        _log.warning(
                            "device.register post steps failed webspace=%s device=%s",
                            webspace_id,
                            device_id,
                            exc_info=True,
                        )

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

                try:
                    asyncio.create_task(_post_register(), name=f"device-register-{webspace_id}-{device_id}")
                except Exception:
                    # Best-effort; failures are already logged inside _post_register.
                    pass
                continue

            if kind == "desktop.toggleInstall":
                _publish_bus(
                    "desktop.toggleInstall",
                    {
                        "type": payload.get("type"),
                        "id": payload.get("id"),
                        "webspace_id": payload.get("webspace_id"),
                    },
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.create":
                # Forward dev flag (if present) so that core runtime can
                # distinguish workspace vs dev webspaces on creation.
                _publish_bus(
                    "desktop.webspace.create",
                    {
                        "id": payload.get("id"),
                        "title": payload.get("title"),
                        "scenario_id": payload.get("scenario_id"),
                        "dev": payload.get("dev"),
                    },
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.rename":
                _publish_bus(
                    "desktop.webspace.rename",
                    {"id": payload.get("id"), "title": payload.get("title")},
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.delete":
                _publish_bus("desktop.webspace.delete", {"id": payload.get("id")})
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.refresh":
                _publish_bus("desktop.webspace.refresh", payload)
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.use":
                target = payload.get("id") or payload.get("webspace_id")
                if not target:
                    await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": "webspace_id required"}))
                    continue
                new_webspace = str(target)
                try:
                    await ensure_webspace_ready(new_webspace, scenario_id=payload.get("scenario_id"))
                    webspace_id = new_webspace
                    await _update_device_presence(webspace_id, device_id or "dev-unknown")
                    _publish_bus("desktop.webspace.refresh", {"webspace_id": webspace_id})
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
                except Exception:
                    await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": "webspace_unavailable"}))
                continue

            if kind == "weather.city_changed":
                _publish_bus(
                    "weather.city_changed",
                    {
                        "city": payload.get("city"),
                        "webspace_id": payload.get("webspace_id"),
                    },
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "voice.chat.open":
                _publish_bus(
                    "voice.chat.open",
                    {
                        "webspace_id": payload.get("webspace_id"),
                    },
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "voice.chat.user":
                _publish_bus(
                    "voice.chat.user",
                    {
                        "text": payload.get("text"),
                        "webspace_id": payload.get("webspace_id"),
                    },
                )
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.webspace.reload":
                _publish_bus("desktop.webspace.reload", payload)
                await websocket.send_text(json.dumps({"ch": "events", "t": "ack", "id": cmd_id, "ok": True}))
                continue

            if kind == "desktop.scenario.set":
                target = (payload or {}).get("scenario_id")
                if not target:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "ch": "events",
                                "t": "ack",
                                "id": cmd_id,
                                "ok": False,
                                "error": "scenario_id required",
                            }
                        )
                    )
                else:
                    _publish_bus("desktop.scenario.set", payload)
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
                continue

            if kind == "scenario.workflow.action":
                _publish_bus("scenario.workflow.action", payload)
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
                continue

            if kind == "scenario.workflow.set_state":
                _publish_bus("scenario.workflow.set_state", payload)
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
