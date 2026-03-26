from __future__ import annotations

"""
Yjs websocket gateway implementation (service layer).
"""

import asyncio
import inspect
import json
import time
import logging
import threading
import os
from typing import TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from typing import Awaitable, Callable

from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

try:
    from ypy_websocket.websocket import Websocket as YWebsocket
    from ypy_websocket.websocket_server import WebsocketServer
    from ypy_websocket.yroom import YRoom
except ImportError as exc:  # pragma: no cover - import guard for dev envs
    raise RuntimeError("ypy_websocket is required for AdaOS realtime collaboration. " "Install dependencies via `pip install -e .[dev]` or `pip install ypy-websocket`.") from exc

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
_TRANSPORT_LOCK = threading.RLock()
_ACTIVE_YWS_LOCK = threading.RLock()
_TRANSPORT_STATE: dict[str, dict[str, Any]] = {
    "ws": {
        "active_connections": 0,
        "open_total": 0,
        "close_total": 0,
        "last_open_at": 0.0,
        "last_close_at": 0.0,
    },
    "yws": {
        "active_connections": 0,
        "open_total": 0,
        "close_total": 0,
        "last_open_at": 0.0,
        "last_close_at": 0.0,
    },
}
_ACTIVE_YWS_CONNECTIONS: dict[str, list[WebSocket]] = {}


def _transport_mark_open(name: str) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _TRANSPORT_LOCK:
        entry = _TRANSPORT_STATE.setdefault(
            key,
            {
                "active_connections": 0,
                "open_total": 0,
                "close_total": 0,
                "last_open_at": 0.0,
                "last_close_at": 0.0,
            },
        )
        entry["active_connections"] = int(entry.get("active_connections") or 0) + 1
        entry["open_total"] = int(entry.get("open_total") or 0) + 1
        entry["last_open_at"] = now


def _transport_mark_close(name: str) -> None:
    key = str(name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _TRANSPORT_LOCK:
        entry = _TRANSPORT_STATE.setdefault(
            key,
            {
                "active_connections": 0,
                "open_total": 0,
                "close_total": 0,
                "last_open_at": 0.0,
                "last_close_at": 0.0,
            },
        )
        active = int(entry.get("active_connections") or 0) - 1
        entry["active_connections"] = max(0, active)
        entry["close_total"] = int(entry.get("close_total") or 0) + 1
        entry["last_close_at"] = now


def _track_yws_connection(webspace_id: str, websocket: WebSocket) -> None:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.setdefault(key, [])
        if websocket not in items:
            items.append(websocket)


def _untrack_yws_connection(webspace_id: str, websocket: WebSocket) -> None:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.get(key)
        if not items:
            return
        try:
            items.remove(websocket)
        except ValueError:
            pass
        if not items:
            _ACTIVE_YWS_CONNECTIONS.pop(key, None)


async def close_webspace_yws_connections(
    webspace_id: str,
    *,
    code: int = 1012,
    reason: str = "webspace_reload",
) -> int:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        sockets = list(_ACTIVE_YWS_CONNECTIONS.get(key) or [])
    closed = 0
    close_reason = str(reason or "webspace_reload")[:120]
    for websocket in sockets:
        try:
            await websocket.close(code=code, reason=close_reason)
            closed += 1
        except Exception:
            pass
    if closed:
        await asyncio.sleep(0)
    return closed


async def reset_live_webspace_room(
    webspace_id: str,
    *,
    close_reason: str = "webspace_reload",
) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    closed_connections = await close_webspace_yws_connections(
        key,
        code=1012,
        reason=close_reason,
    )
    if closed_connections:
        # Let the active serve() coroutines observe disconnect and run cleanup before
        # a new room is created for the same webspace.
        await asyncio.sleep(0.15)

    room = y_server.rooms.pop(key, None)
    _room_locks.pop(key, None)
    room_stopped = False
    ystore_stopped = False

    if room is not None:
        stop_room = getattr(room, "stop", None)
        if callable(stop_room):
            try:
                result = stop_room()
                if inspect.isawaitable(result):
                    await result
                room_stopped = True
            except Exception:
                room_stopped = False
        ystore = getattr(room, "ystore", None)
        stop_ystore = getattr(ystore, "stop", None)
        if callable(stop_ystore):
            try:
                result = stop_ystore()
                if inspect.isawaitable(result):
                    await result
                ystore_stopped = True
            except Exception:
                ystore_stopped = False

    return {
        "webspace_id": key,
        "closed_connections": closed_connections,
        "room_dropped": room is not None,
        "room_stopped": room_stopped,
        "ystore_stopped": ystore_stopped,
    }


def gateway_transport_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _TRANSPORT_LOCK:
        state = json.loads(json.dumps(_TRANSPORT_STATE))
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        last_open_at = entry.get("last_open_at")
        last_close_at = entry.get("last_close_at")
        entry["last_open_ago_s"] = (
            round(max(0.0, now - float(last_open_at)), 3)
            if isinstance(last_open_at, (int, float)) and float(last_open_at) > 0.0
            else None
        )
        entry["last_close_ago_s"] = (
            round(max(0.0, now - float(last_close_at)), 3)
            if isinstance(last_close_at, (int, float)) and float(last_close_at) > 0.0
            else None
        )
    return {"transports": state, "updated_at": now}


def _ws_trace_enabled() -> bool:
    return os.getenv("HUB_WS_TRACE", "0") == "1"


def _ws_client_str(websocket: WebSocket) -> str:
    try:
        client = getattr(websocket, "client", None)
        if client and getattr(client, "host", None) is not None:
            return f"{client.host}:{client.port}"
    except Exception:
        pass
    try:
        scope = getattr(websocket, "scope", None) or {}
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and len(client) >= 2:
            return f"{client[0]}:{client[1]}"
    except Exception:
        pass
    return "unknown"


class WorkspaceWebsocketServer(WebsocketServer):
    """
    WebsocketServer that binds each room to a webspace-backed SQLiteYStore.

    We use the websocket path as the webspace id (e.g. "default").
    """

    async def get_room(self, name: str) -> YRoom:  # type: ignore[override]
        webspace_id = name or "default"

        def _space_mode(ws_id: str) -> str:
            try:
                row = get_workspace(ws_id)
                if not row:
                    return "workspace"
                return row.effective_source_mode
            except Exception:
                return "workspace"

        # Double-checked locking to prevent concurrent room creation.
        # Without this, multiple concurrent get_room() calls can both pass
        # the `if name not in self.rooms` check and create duplicate rooms,
        # causing the second room to overwrite the first and orphan clients.
        if name not in self.rooms:
            lock = _room_locks.setdefault(webspace_id, asyncio.Lock())
            async with lock:
                # Second check after acquiring lock - another coroutine may
                # have already created the room while we were waiting.
                if name not in self.rooms:
                    _ylog.info("creating YRoom for webspace=%s", webspace_id)
                    ensure_workspace(webspace_id)
                    ystore = get_ystore_for_webspace(webspace_id)
                    row = get_workspace(webspace_id)
                    space = _space_mode(webspace_id)
                    await ensure_webspace_seeded_from_scenario(
                        ystore,
                        webspace_id=webspace_id,
                        default_scenario_id=(row.effective_home_scenario if row and row.home_scenario else "web_desktop"),
                        space=space,
                    )
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
_room_locks: dict[str, asyncio.Lock] = {}


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
    row = get_workspace(webspace_id)
    space = row.effective_source_mode if row else "workspace"
    base_scenario = str(scenario_id or "").strip()
    if not base_scenario and row and row.home_scenario:
        base_scenario = row.effective_home_scenario
    if not base_scenario:
        base_scenario = "web_desktop"

    try:
        await ensure_webspace_seeded_from_scenario(
            ystore,
            webspace_id=webspace_id,
            default_scenario_id=base_scenario,
            space=space,
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
        while True:
            msg = await self._ws.receive()
            msg_type = msg.get("type")
            if msg_type == "websocket.receive":
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    if data:
                        return data
                    continue
                if msg.get("text") is not None:
                    data = msg["text"].encode("utf-8")
                    if data:
                        return data
                    continue
                continue
            if msg_type == "websocket.disconnect":
                raise RuntimeError("websocket disconnected")
            raise RuntimeError(f"unexpected websocket event: {msg_type}")


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

    if _ws_trace_enabled():
        try:
            token_present = "token" in params
            _ylog.info(
                "yws trace open client=%s webspace=%s dev=%s token=%s",
                _ws_client_str(websocket),
                webspace_id,
                dev_id,
                token_present,
            )
        except Exception:
            pass
    _ylog.info("yws connection open webspace=%s dev=%s", webspace_id, dev_id)
    await websocket.accept()
    _track_yws_connection(webspace_id, websocket)
    _transport_mark_open("yws")
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=webspace_id)
    try:
        await y_server.serve(adapter)
    except RuntimeError:
        return
    finally:
        _untrack_yws_connection(webspace_id, websocket)
        _transport_mark_close("yws")
        _ylog.info("yws connection closed webspace=%s dev=%s", webspace_id, dev_id)
        if _ws_trace_enabled():
            try:
                code = getattr(websocket, "close_code", None)
                _ylog.info(
                    "yws trace closed client=%s webspace=%s dev=%s code=%s",
                    _ws_client_str(websocket),
                    webspace_id,
                    dev_id,
                    code,
                )
            except Exception:
                pass


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


def _make_publish_bus(
    device_id_ref: Callable[[], str | None],
    webspace_id_ref: Callable[[], str],
) -> Callable[[str, Dict[str, Any] | None], None]:
    """Create a ``_publish_bus`` closure bound to mutable connection state."""

    def _publish_bus(topic: str, extra: Dict[str, Any] | None = None) -> None:
        data = dict(extra or {})
        effective_ws = str(data.get("webspace_id") or webspace_id_ref())
        data.setdefault("webspace_id", effective_ws)
        meta = dict(data.get("_meta") or {})
        meta.setdefault("webspace_id", effective_ws)
        did = device_id_ref()
        if did:
            meta.setdefault("device_id", did)
        data["_meta"] = meta
        try:
            ctx = get_agent_ctx()
            ev = DomainEvent(type=topic, payload=data, source="events_ws", ts=time.time())
            ctx.bus.publish(ev)
        except Exception:
            _log.warning("failed to publish %s", topic, exc_info=True)

    return _publish_bus


async def process_events_command(
    kind: str,
    cmd_id: str,
    payload: dict[str, Any],
    device_id: str,
    webspace_id: str,
    send_response: Callable[[dict[str, Any]], Awaitable[None]],
) -> str | None:
    """
    Process a single events-channel command and send ack via *send_response*.

    Returns the **new** ``webspace_id`` when the command changed it (e.g.
    ``device.register``, ``desktop.webspace.use``), or ``None`` if unchanged.

    This function is shared between the ``/ws`` WebSocket endpoint and the
    WebRTC events DataChannel so that both transports execute the same logic.
    """

    _publish_bus = _make_publish_bus(lambda: device_id, lambda: webspace_id)

    async def _ack(ok: bool = True, *, data: dict[str, Any] | None = None, error: str | None = None) -> None:
        msg: dict[str, Any] = {"ch": "events", "t": "ack", "id": cmd_id, "ok": ok}
        if data is not None:
            msg["data"] = data
        if error is not None:
            msg["error"] = error
        await send_response(msg)

    if kind == "device.register":
        new_device = payload.get("device_id") or "dev-unknown"
        requested_webspace = payload.get("webspace_id") or payload.get("id") or "default"
        new_webspace = str(requested_webspace or "default")

        captured_device = new_device
        captured_ws = new_webspace

        async def _post_register() -> None:
            try:
                await start_y_server()
                await _update_device_presence(captured_ws, captured_device)
                # Sync webspace listing directly to the live room's YDoc.
                # This ensures the frontend sees data.webspaces immediately.
                try:
                    from adaos.services.scenario.webspace_runtime import _webspace_listing

                    room = y_server.rooms.get(captured_ws)
                    if room:
                        listing = _webspace_listing()
                        with room.ydoc.begin_transaction() as txn:
                            data_map = room.ydoc.get_map("data")
                            data_map.set(txn, "webspaces", {"items": listing})
                        _log.debug("wrote webspaces listing to room webspace=%s items=%d", captured_ws, len(listing))
                except Exception:
                    _log.debug("webspace listing sync failed", exc_info=True)
                _log.debug("device.register post steps ok webspace=%s device=%s", captured_ws, captured_device)
            except Exception:
                _log.warning("device.register post steps failed webspace=%s device=%s", captured_ws, captured_device, exc_info=True)

        try:
            # Ensure room is created and seeded BEFORE sending ack.
            # This prevents race condition where frontend connects Yjs provider
            # before room is ready, causing empty webspaces on first connection.
            await _post_register()
            await _ack(data={"webspace_id": new_webspace})
        except Exception:
            # Best-effort: still send ack even if post-register fails
            await _ack(data={"webspace_id": new_webspace})
        return new_webspace

    if kind == "desktop.toggleInstall":
        _publish_bus("desktop.toggleInstall", {"type": payload.get("type"), "id": payload.get("id"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "desktop.webspace.create":
        _publish_bus("desktop.webspace.create", {"id": payload.get("id"), "title": payload.get("title"), "scenario_id": payload.get("scenario_id"), "dev": payload.get("dev")})
        await _ack()
        return None

    if kind == "desktop.webspace.rename":
        _publish_bus("desktop.webspace.rename", {"id": payload.get("id"), "title": payload.get("title")})
        await _ack()
        return None

    if kind == "desktop.webspace.delete":
        _publish_bus("desktop.webspace.delete", {"id": payload.get("id")})
        await _ack()
        return None

    if kind == "desktop.webspace.refresh":
        _publish_bus("desktop.webspace.refresh", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.go_home":
        _publish_bus("desktop.webspace.go_home", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.set_home":
        target = (payload or {}).get("scenario_id")
        if not target:
            await _ack(False, error="scenario_id required")
        else:
            _publish_bus("desktop.webspace.set_home", payload)
            await _ack()
        return None

    if kind == "desktop.webspace.use":
        target = payload.get("id") or payload.get("webspace_id")
        if not target:
            await _ack(False, error="webspace_id required")
            return None
        new_webspace = str(target)
        try:
            await ensure_webspace_ready(new_webspace, scenario_id=payload.get("scenario_id"))
            await _update_device_presence(new_webspace, device_id or "dev-unknown")
            _publish_bus("desktop.webspace.refresh", {"webspace_id": new_webspace})
            await _ack(data={"webspace_id": new_webspace})
            return new_webspace
        except Exception:
            await _ack(False, error="webspace_unavailable")
            return None

    if kind == "weather.city_changed":
        _publish_bus("weather.city_changed", {"city": payload.get("city"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "voice.chat.open":
        _publish_bus("voice.chat.open", {"webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "voice.chat.user":
        _publish_bus("voice.chat.user", {"text": payload.get("text"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "desktop.webspace.reload":
        _publish_bus("desktop.webspace.reload", payload)
        await _ack()
        return None

    if kind == "desktop.webspace.reset":
        _publish_bus("desktop.webspace.reset", payload)
        await _ack()
        return None

    if kind == "desktop.scenario.set":
        target = (payload or {}).get("scenario_id")
        if not target:
            await _ack(False, error="scenario_id required")
        else:
            _publish_bus("desktop.scenario.set", payload)
            await _ack()
        return None

    if kind == "skills.update":
        # Trigger a best-effort skill source refresh (git pull / monorepo sparse pull)
        # and acknowledge with updated version if available.
        try:
            from adaos.services.agent_context import get_ctx as _get_ctx
            from adaos.services.skill.update import SkillUpdateService

            ctx = _get_ctx()
            skill_name = str(payload.get("name") or payload.get("skill") or "").strip()
            dry_run = bool(payload.get("dry_run", False))
            if not skill_name:
                await _ack(False, error="name required")
                return None
            result = SkillUpdateService(ctx).request_update(skill_name, dry_run=dry_run)
            _publish_bus("skills.updated", {"name": skill_name, "version": result.version, "updated": result.updated})
            await _ack(True, data={"name": skill_name, "updated": result.updated, "version": result.version})
        except FileNotFoundError:
            await _ack(False, error="skill_not_installed")
        except PermissionError as exc:
            await _ack(False, error=str(exc) or "fs_readonly")
        except Exception as exc:
            await _ack(False, error=str(exc) or "update_failed")
        return None

    if kind == "nlp.teacher.candidate.apply":
        _publish_bus("nlp.teacher.candidate.apply", {"candidate_id": payload.get("candidate_id"), "target": payload.get("target"), "webspace_id": payload.get("webspace_id")})
        await _ack()
        return None

    if kind == "nlp.teacher.revision.apply":
        _publish_bus(
            "nlp.teacher.revision.apply",
            {
                "revision_id": payload.get("revision_id"),
                "intent": payload.get("intent"),
                "examples": payload.get("examples"),
                "slots": payload.get("slots"),
                "webspace_id": payload.get("webspace_id"),
            },
        )
        await _ack()
        return None

    if kind == "nlp.teacher.regex_rule.apply":
        _publish_bus(
            "nlp.teacher.regex_rule.apply",
            {
                "candidate_id": payload.get("candidate_id"),
                "intent": payload.get("intent"),
                "pattern": payload.get("pattern"),
                "target": payload.get("target"),
                "webspace_id": payload.get("webspace_id"),
            },
        )
        await _ack()
        return None

    if kind == "scenario.workflow.action":
        _publish_bus("scenario.workflow.action", payload)
        await _ack()
        return None

    if kind == "scenario.workflow.set_state":
        _publish_bus("scenario.workflow.set_state", payload)
        await _ack()
        return None

    # Default behaviour for declarative host actions: publish unknown command
    # kinds to the local bus so skills can subscribe to their own UI events.
    if isinstance(kind, str) and kind.strip():
        _publish_bus(kind, payload)
    await _ack()
    return None


@router.websocket("/ws")
async def events_ws(websocket: WebSocket):
    """
    JSON events websocket.

    Implements device.register, desktop/voice/scenario commands, and WebRTC
    signaling (``rtc.offer``, ``rtc.ice``).
    """
    await websocket.accept()
    _transport_mark_open("ws")
    if _ws_trace_enabled():
        try:
            params: Dict[str, str] = dict(websocket.query_params)
            token_present = "token" in params
            _log.info(
                "ws trace open client=%s token=%s params=%s",
                _ws_client_str(websocket),
                token_present,
                ",".join(sorted(params.keys())) if params else "",
            )
        except Exception:
            pass

    device_id: str | None = None
    webspace_id = "default"

    async def _ws_send(msg: dict[str, Any]) -> None:
        try:
            await websocket.send_text(json.dumps(msg))
        except (WebSocketDisconnect, RuntimeError):
            # Connection closed - silently return
            return

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

            # -- WebRTC signaling (rtc.offer / rtc.ice) -----------------------
            if kind == "rtc.offer":
                try:
                    from adaos.services.webrtc.peer import handle_rtc_offer

                    async def _send_ice_via_ws(candidate: dict[str, Any]) -> None:
                        try:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "ch": "events",
                                        "t": "evt",
                                        "kind": "rtc.ice",
                                        "payload": {"candidate": candidate},
                                    }
                                )
                            )
                        except (WebSocketDisconnect, RuntimeError):
                            # Connection closed - silently return
                            return

                    answer = await handle_rtc_offer(
                        offer_sdp=payload.get("sdp", ""),
                        offer_type=payload.get("type", "offer"),
                        device_id=device_id or "unknown",
                        webspace_id=webspace_id,
                        send_ice_cb=_send_ice_via_ws,
                    )
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": True, "data": answer})
                except Exception as e:
                    _log.error(f"rtc.offer failed: {e!r}", exc_info=True)
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": f"rtc_offer_failed: {e}"})
                continue

            if kind == "rtc.ice":
                try:
                    from adaos.services.webrtc.peer import handle_remote_ice

                    await handle_remote_ice(device_id or "unknown", payload.get("candidate"))
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": True})
                except Exception as e:
                    _log.error(f"rtc.ice failed: {e!r}", exc_info=True)
                    await _ws_send({"ch": "events", "t": "ack", "id": cmd_id, "ok": False, "error": f"rtc_ice_failed: {e}"})
                continue

            # -- Standard commands via extracted dispatcher --------------------
            new_ws = await process_events_command(
                kind=kind,
                cmd_id=cmd_id,
                payload=payload,
                device_id=device_id or "dev-unknown",
                webspace_id=webspace_id,
                send_response=_ws_send,
            )
            # Update connection-scoped state when a command changed it.
            if new_ws is not None:
                webspace_id = new_ws
            if kind == "device.register":
                device_id = payload.get("device_id") or "dev-unknown"
    finally:
        _transport_mark_close("ws")
        _ = device_id
        if _ws_trace_enabled():
            try:
                code = getattr(websocket, "close_code", None)
                _log.info(
                    "ws trace closed client=%s device=%s webspace=%s code=%s",
                    _ws_client_str(websocket),
                    device_id,
                    webspace_id,
                    code,
                )
            except Exception:
                pass
