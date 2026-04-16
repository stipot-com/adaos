from __future__ import annotations

"""
Yjs websocket gateway implementation (service layer).
"""

import asyncio
from collections import deque
import gc
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
from adaos.services.yjs.observers import attach_room_observers, forget_room_observers
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.scheduler import get_scheduler
from adaos.domain import Event as DomainEvent
from adaos.services.agent_context import get_ctx as get_agent_ctx

router = APIRouter()
_log = logging.getLogger("adaos.events_ws")
_ylog = logging.getLogger("adaos.yjs.gateway")
_TRANSPORT_LOCK = threading.RLock()
_ACTIVE_YWS_LOCK = threading.RLock()
_YWS_STORM_LOCK = threading.RLock()
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
_ACTIVE_YWS_CLIENTS: dict[str, dict[str, int]] = {}
_YWS_OPEN_HISTORY: deque[float] = deque(maxlen=512)
_YWS_CLIENT_OPEN_HISTORY: dict[str, deque[float]] = {}
_YROOM_LIFECYCLE_LOCK = threading.RLock()
_YROOM_LIFECYCLE: dict[str, dict[str, Any]] = {}
_WS_EVENT_SUBSCRIPTIONS_LOCK = threading.RLock()
_WS_EVENT_SUBSCRIBERS: dict[int, dict[str, Any]] = {}
_WS_EVENT_FORWARDER_INSTALLED = False


def _is_websocket_accept_race(exc: BaseException) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return (
        "websocket.accept" in text
        and "websocket.close" in text
    ) or "close message has been sent" in text


async def _stop_ystore_maybe_async(ystore: Any) -> None:
    try:
        result = ystore.stop()
    except Exception:
        return
    if inspect.isawaitable(result):
        try:
            await result
        except Exception:
            return


def _seconds_ago(value: Any, now: float) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    if stamp <= 0.0:
        return None
    return round(max(0.0, now - stamp), 3)


def _memory_stream_statistics(stream: Any) -> dict[str, Any]:
    stats = getattr(stream, "statistics", None)
    if not callable(stats):
        return {}
    try:
        snapshot = stats()
    except Exception:
        return {}
    return {
        "current_buffer_used": int(getattr(snapshot, "current_buffer_used", 0) or 0),
        "max_buffer_size": int(getattr(snapshot, "max_buffer_size", 0) or 0),
        "open_send_streams": int(getattr(snapshot, "open_send_streams", 0) or 0),
        "open_receive_streams": int(getattr(snapshot, "open_receive_streams", 0) or 0),
        "tasks_waiting_send": int(getattr(snapshot, "tasks_waiting_send", 0) or 0),
        "tasks_waiting_receive": int(getattr(snapshot, "tasks_waiting_receive", 0) or 0),
    }


def _mark_room_created(webspace_id: str, room: Any) -> None:
    key = str(webspace_id or "").strip() or "default"
    ydoc = getattr(room, "ydoc", None)
    now = time.time()
    with _YROOM_LIFECYCLE_LOCK:
        entry = _YROOM_LIFECYCLE.setdefault(key, {})
        entry["generation"] = int(entry.get("generation") or 0) + 1
        entry["create_total"] = int(entry.get("create_total") or 0) + 1
        entry["last_created_at"] = now
        entry["last_room_object_id"] = id(room)
        entry["last_ydoc_object_id"] = id(ydoc) if ydoc is not None else None


def _mark_room_reset(
    webspace_id: str,
    *,
    close_reason: str,
    room: Any | None,
    room_dropped: bool,
    closed_connections: int,
    closed_webrtc_peers: int,
) -> None:
    key = str(webspace_id or "").strip() or "default"
    ydoc = getattr(room, "ydoc", None) if room is not None else None
    now = time.time()
    with _YROOM_LIFECYCLE_LOCK:
        entry = _YROOM_LIFECYCLE.setdefault(key, {})
        entry["reset_total"] = int(entry.get("reset_total") or 0) + 1
        entry["last_reset_at"] = now
        entry["last_reset_reason"] = str(close_reason or "").strip() or "webspace_reload"
        entry["last_reset_closed_connections"] = int(closed_connections or 0)
        entry["last_reset_closed_webrtc_peers"] = int(closed_webrtc_peers or 0)
        entry["last_reset_room_dropped"] = bool(room_dropped)
        if room is not None:
            entry["last_reset_room_object_id"] = id(room)
        if ydoc is not None:
            entry["last_reset_ydoc_object_id"] = id(ydoc)
        if room_dropped:
            entry["drop_total"] = int(entry.get("drop_total") or 0) + 1
            entry["last_dropped_at"] = now


def _room_debug_snapshot(webspace_id: str, room: Any | None, now: float) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    with _YROOM_LIFECYCLE_LOCK:
        meta = dict(_YROOM_LIFECYCLE.get(key) or {})

    ydoc = getattr(room, "ydoc", None) if room is not None else None
    ystore = getattr(room, "ystore", None) if room is not None else None
    clients = getattr(room, "clients", None) if room is not None else None
    send_stream_stats = _memory_stream_statistics(getattr(room, "_update_send_stream", None) if room is not None else None)
    recv_stream_stats = _memory_stream_statistics(getattr(room, "_update_receive_stream", None) if room is not None else None)
    started_event = getattr(room, "_started", None) if room is not None else None
    task_group = getattr(room, "_task_group", None) if room is not None else None

    return {
        "webspace_id": key,
        "active": bool(room is not None),
        "generation": int(meta.get("generation") or 0),
        "create_total": int(meta.get("create_total") or 0),
        "reset_total": int(meta.get("reset_total") or 0),
        "drop_total": int(meta.get("drop_total") or 0),
        "last_created_at": meta.get("last_created_at"),
        "last_created_ago_s": _seconds_ago(meta.get("last_created_at"), now),
        "last_reset_at": meta.get("last_reset_at"),
        "last_reset_ago_s": _seconds_ago(meta.get("last_reset_at"), now),
        "last_dropped_at": meta.get("last_dropped_at"),
        "last_dropped_ago_s": _seconds_ago(meta.get("last_dropped_at"), now),
        "last_reset_reason": str(meta.get("last_reset_reason") or "").strip() or None,
        "last_reset_closed_connections": int(meta.get("last_reset_closed_connections") or 0),
        "last_reset_closed_webrtc_peers": int(meta.get("last_reset_closed_webrtc_peers") or 0),
        "last_reset_room_dropped": bool(meta.get("last_reset_room_dropped")),
        "room_object_id": id(room) if room is not None else meta.get("last_room_object_id"),
        "ydoc_object_id": id(ydoc) if ydoc is not None else meta.get("last_ydoc_object_id"),
        "client_total": len(clients) if isinstance(clients, list) else 0,
        "ready": bool(getattr(room, "_ready", False)) if room is not None else False,
        "started": bool(getattr(started_event, "is_set", lambda: False)()) if started_event is not None else False,
        "task_group_active": bool(task_group is not None),
        "ystore_attached": bool(ystore is not None),
        "update_send_stream": send_stream_stats,
        "update_receive_stream": recv_stream_stats,
    }


def _room_debug_snapshot_all(now: float) -> tuple[dict[str, Any], dict[str, int]]:
    room_keys = set()
    try:
        room_keys.update(str(key) for key in getattr(y_server, "rooms", {}).keys())
    except Exception:
        pass
    with _YROOM_LIFECYCLE_LOCK:
        room_keys.update(str(key) for key in _YROOM_LIFECYCLE.keys())

    room_details: dict[str, Any] = {}
    aggregated = {
        "active_room_total": 0,
        "room_create_total": 0,
        "room_reset_total": 0,
        "room_drop_total": 0,
        "room_generation_max": 0,
        "update_stream_buffer_used_total": 0,
        "update_stream_waiting_send_total": 0,
        "update_stream_waiting_receive_total": 0,
    }
    for key in sorted(room_keys):
        room = getattr(y_server, "rooms", {}).get(key)
        snapshot = _room_debug_snapshot(key, room, now)
        room_details[key] = snapshot
        aggregated["active_room_total"] += 1 if snapshot.get("active") else 0
        aggregated["room_create_total"] += int(snapshot.get("create_total") or 0)
        aggregated["room_reset_total"] += int(snapshot.get("reset_total") or 0)
        aggregated["room_drop_total"] += int(snapshot.get("drop_total") or 0)
        aggregated["room_generation_max"] = max(
            aggregated["room_generation_max"],
            int(snapshot.get("generation") or 0),
        )
        send_stream = snapshot.get("update_send_stream") if isinstance(snapshot.get("update_send_stream"), dict) else {}
        aggregated["update_stream_buffer_used_total"] += int(send_stream.get("current_buffer_used") or 0)
        aggregated["update_stream_waiting_send_total"] += int(send_stream.get("tasks_waiting_send") or 0)
        aggregated["update_stream_waiting_receive_total"] += int(send_stream.get("tasks_waiting_receive") or 0)
    return room_details, aggregated


async def _close_room_stream_maybe(stream: Any) -> bool:
    if stream is None:
        return False
    closed = False
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
            closed = True
        except Exception:
            closed = False
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        try:
            result = aclose()
            if inspect.isawaitable(result):
                await result
            closed = True
        except Exception:
            pass
    return closed


async def _release_room_refs(webspace_id: str, room: Any) -> bool:
    released = False
    ydoc = getattr(room, "ydoc", None)
    if ydoc is not None:
        try:
            forget_room_observers(webspace_id, ydoc)
        except Exception:
            pass
        try:
            from adaos.services.weather.observer import forget_weather_room_observer

            forget_weather_room_observer(webspace_id, id(ydoc))
        except Exception:
            pass

    for attr in ("_update_send_stream", "_update_receive_stream"):
        try:
            stream = getattr(room, attr, None)
        except Exception:
            stream = None
        try:
            released = await _close_room_stream_maybe(stream) or released
        except Exception:
            pass

    clients = getattr(room, "clients", None)
    if isinstance(clients, list):
        try:
            clients.clear()
            released = True
        except Exception:
            pass

    for attr in (
        "awareness",
        "_on_message",
        "_started",
        "_exit_stack",
        "_task_group",
        "ydoc",
        "ystore",
        "_loop",
        "_thread_id",
        "ready",
        "log",
    ):
        if not hasattr(room, attr):
            continue
        try:
            setattr(room, attr, None)
            released = True
        except Exception:
            continue
    return released


async def _accept_websocket(websocket: WebSocket, *, channel: str) -> bool:
    try:
        await websocket.accept()
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as exc:
        if _is_websocket_accept_race(exc):
            _ylog.info(
                "%s websocket accept skipped because handshake was already closed client=%s",
                channel,
                _ws_client_str(websocket),
            )
            return False
        raise


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


def _publish_runtime_event(topic: str, payload: dict[str, Any] | None = None, *, source: str = "yjs.gateway") -> None:
    try:
        ctx = get_agent_ctx()
        ctx.bus.publish(DomainEvent(type=topic, payload=dict(payload or {}), source=source, ts=time.time()))
    except Exception:
        _log.debug("failed to publish runtime event topic=%s", topic, exc_info=True)


def _normalize_ws_event_topics(raw_topics: Any) -> set[str]:
    if not isinstance(raw_topics, list):
        return set()
    return {
        topic
        for topic in (str(raw or "").strip() for raw in raw_topics)
        if topic
    }


def _ws_event_topic_matches(subscription: str, event_type: str) -> bool:
    topic = str(subscription or "").strip()
    event = str(event_type or "").strip()
    if not topic or not event:
        return False
    if topic in {"*", ""}:
        return True
    if topic.endswith("*"):
        return event.startswith(topic[:-1])
    return event == topic


def _build_ws_event_message(
    event_type: str,
    payload: Any,
    *,
    source: str = "events_ws",
    ts: float | None = None,
) -> dict[str, Any]:
    return {
        "ch": "events",
        "t": "evt",
        "kind": str(event_type or "").strip(),
        "payload": payload if isinstance(payload, dict) else {"value": payload},
        "source": str(source or "events_ws").strip() or "events_ws",
        "ts": float(ts or time.time()),
    }


async def _send_ws_event_message(websocket: WebSocket, message: dict[str, Any]) -> None:
    try:
        await websocket.send_text(json.dumps(message))
    except (WebSocketDisconnect, RuntimeError):
        _unregister_ws_event_subscriptions(websocket)
        raise


def _iter_initial_ws_event_messages(topics: set[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if any(_ws_event_topic_matches(topic, "node.status") for topic in topics):
        try:
            from adaos.services.bootstrap import load_config as _load_config
            from adaos.services.system_model.service import (
                current_node_status_push_payload as _current_node_status_push_payload,
            )

            conf = _load_config()
            if str(getattr(conf, "role", "") or "").strip().lower() == "hub":
                messages.append(
                    _build_ws_event_message(
                        "node.status",
                        _current_node_status_push_payload(),
                        source="node.status",
                    )
                )
        except Exception:
            _ylog.debug("failed to snapshot node.status for ws subscriber", exc_info=True)
    if any(_ws_event_topic_matches(topic, "core.update.status") for topic in topics):
        try:
            from adaos.services.core_update import read_status as _read_core_update_status

            messages.append(
                _build_ws_event_message(
                    "core.update.status",
                    _read_core_update_status() or {},
                    source="core.update.status",
                )
            )
        except Exception:
            _ylog.debug("failed to snapshot core.update.status for ws subscriber", exc_info=True)
    return messages


async def _send_initial_ws_event_messages(websocket: WebSocket, topics: set[str]) -> None:
    for message in _iter_initial_ws_event_messages(topics):
        try:
            await _send_ws_event_message(websocket, message)
        except (WebSocketDisconnect, RuntimeError):
            return


def _ensure_ws_event_forwarder() -> None:
    global _WS_EVENT_FORWARDER_INSTALLED
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        if _WS_EVENT_FORWARDER_INSTALLED:
            return
        ctx = get_agent_ctx()
        ctx.bus.subscribe("*", _forward_ws_bus_event)
        _WS_EVENT_FORWARDER_INSTALLED = True


def _register_ws_event_subscriptions(
    websocket: WebSocket,
    loop: asyncio.AbstractEventLoop,
    raw_topics: Any,
) -> set[str]:
    topics = _normalize_ws_event_topics(raw_topics)
    if not topics:
        return set()
    _ensure_ws_event_forwarder()
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        entry = _WS_EVENT_SUBSCRIBERS.setdefault(
            id(websocket),
            {
                "websocket": websocket,
                "loop": loop,
                "topics": set(),
            },
        )
        entry["loop"] = loop
        tracked = entry.setdefault("topics", set())
        added = set(topics) - set(tracked)
        tracked.update(topics)
    return added


def _unregister_ws_event_subscriptions(websocket: WebSocket) -> None:
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        _WS_EVENT_SUBSCRIBERS.pop(id(websocket), None)


def _forward_ws_bus_event(ev: DomainEvent) -> None:
    event_type = str(getattr(ev, "type", "") or "").strip()
    if not event_type:
        return
    with _WS_EVENT_SUBSCRIPTIONS_LOCK:
        subscribers = [
            dict(entry)
            for entry in _WS_EVENT_SUBSCRIBERS.values()
            if any(_ws_event_topic_matches(topic, event_type) for topic in entry.get("topics", set()))
        ]
    if not subscribers:
        return
    message = _build_ws_event_message(
        event_type,
        getattr(ev, "payload", {}) or {},
        source=str(getattr(ev, "source", "") or "events_ws"),
        ts=float(getattr(ev, "ts", 0.0) or time.time()),
    )
    for entry in subscribers:
        websocket = entry.get("websocket")
        loop = entry.get("loop")
        if websocket is None or not isinstance(loop, asyncio.AbstractEventLoop):
            continue
        try:
            asyncio.run_coroutine_threadsafe(
                _send_ws_event_message(websocket, message),
                loop,
            )
        except Exception:
            _unregister_ws_event_subscriptions(websocket)


def _track_yws_connection(webspace_id: str, websocket: WebSocket, *, device_id: str | None = None) -> None:
    key = str(webspace_id or "").strip() or "default"
    device_key = str(device_id or "").strip() or "unknown"
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.setdefault(key, [])
        if websocket not in items:
            items.append(websocket)
        clients = _ACTIVE_YWS_CLIENTS.setdefault(key, {})
        clients[device_key] = int(clients.get(device_key) or 0) + 1


def _record_yws_open(webspace_id: str, dev_id: str) -> None:
    now = time.time()
    key = f"{str(webspace_id or '').strip() or 'default'}::{str(dev_id or '').strip() or 'unknown'}"
    with _YWS_STORM_LOCK:
        _YWS_OPEN_HISTORY.append(now)
        items = _YWS_CLIENT_OPEN_HISTORY.setdefault(key, deque(maxlen=64))
        items.append(now)
        cutoff = now - 60.0
        stale_keys: list[str] = []
        for client_key, queue in _YWS_CLIENT_OPEN_HISTORY.items():
            while queue and queue[0] < cutoff:
                queue.popleft()
            if not queue:
                stale_keys.append(client_key)
        for client_key in stale_keys:
            _YWS_CLIENT_OPEN_HISTORY.pop(client_key, None)
        recent_15s = sum(1 for ts in items if ts >= now - 15.0)
    if recent_15s >= 8:
        _ylog.warning(
            "yws reconnect storm detected webspace=%s dev=%s opens_15s=%s",
            str(webspace_id or "").strip() or "default",
            str(dev_id or "").strip() or "unknown",
            recent_15s,
        )


def _yws_storm_snapshot(now: float) -> dict[str, Any]:
    with _YWS_STORM_LOCK:
        recent_10s = sum(1 for ts in _YWS_OPEN_HISTORY if ts >= now - 10.0)
        recent_60s = sum(1 for ts in _YWS_OPEN_HISTORY if ts >= now - 60.0)
        hot_clients: list[dict[str, Any]] = []
        for key, queue in _YWS_CLIENT_OPEN_HISTORY.items():
            recent_15s = sum(1 for ts in queue if ts >= now - 15.0)
            if recent_15s <= 0:
                continue
            webspace_id, _, dev_id = key.partition("::")
            hot_clients.append(
                {
                    "webspace_id": webspace_id or "default",
                    "dev_id": dev_id or "unknown",
                    "open_15s": recent_15s,
                }
            )
    hot_clients.sort(key=lambda item: (-int(item.get("open_15s") or 0), str(item.get("dev_id") or "")))
    return {
        "recent_open_10s": recent_10s,
        "recent_open_60s": recent_60s,
        "storm_detected": recent_10s >= 8,
        "hot_clients": hot_clients[:3],
    }


def _untrack_yws_connection(webspace_id: str, websocket: WebSocket) -> None:
    key = str(webspace_id or "").strip() or "default"
    with _ACTIVE_YWS_LOCK:
        items = _ACTIVE_YWS_CONNECTIONS.get(key)
        if not items:
            device_key = None
        else:
            try:
                items.remove(websocket)
            except ValueError:
                pass
        if not items:
            _ACTIVE_YWS_CONNECTIONS.pop(key, None)
        params = getattr(websocket, "query_params", {}) or {}
        device_key = str(params.get("dev") or "unknown").strip() or "unknown"
        clients = _ACTIVE_YWS_CLIENTS.get(key)
        if clients:
            remaining = int(clients.get(device_key) or 0) - 1
            if remaining > 0:
                clients[device_key] = remaining
            else:
                clients.pop(device_key, None)
            if not clients:
                _ACTIVE_YWS_CLIENTS.pop(key, None)


def active_browser_session_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _ACTIVE_YWS_LOCK:
        clients = {
            webspace_id: dict(device_counts)
            for webspace_id, device_counts in _ACTIVE_YWS_CLIENTS.items()
            if isinstance(device_counts, dict)
        }
    peers: list[dict[str, Any]] = []
    for webspace_id, device_counts in clients.items():
        for device_id, session_count in sorted(device_counts.items()):
            token = str(device_id or "").strip()
            if not token:
                continue
            peers.append(
                {
                    "device_id": token,
                    "webspace_id": str(webspace_id or "").strip() or "default",
                    "connection_state": "connected",
                    "yjs_channel_state": "open",
                    "session_count": int(session_count or 0),
                    "source": "yws_gateway",
                }
            )
    return {
        "peer_total": len(peers),
        "peers": peers,
        "updated_at": now,
    }


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


async def close_webspace_webrtc_peers(
    webspace_id: str,
    *,
    reason: str = "webspace_reload",
) -> int:
    try:
        from adaos.services.webrtc.peer import close_peers_for_webspace
    except Exception:
        return 0
    try:
        return int(await close_peers_for_webspace(webspace_id, reason=reason) or 0)
    except Exception:
        _ylog.debug(
            "failed to close webrtc peers for webspace=%s reason=%s",
            webspace_id,
            reason,
            exc_info=True,
        )
        return 0


async def reset_live_webspace_room(
    webspace_id: str,
    *,
    close_reason: str = "webspace_reload",
) -> dict[str, Any]:
    key = str(webspace_id or "").strip() or "default"
    closed_webrtc_peers = await close_webspace_webrtc_peers(
        key,
        reason=close_reason,
    )
    closed_connections = await close_webspace_yws_connections(
        key,
        code=1012,
        reason=close_reason,
    )
    if closed_connections or closed_webrtc_peers:
        # Let the active serve() coroutines observe disconnect and run cleanup before
        # a new room is created for the same webspace.
        await asyncio.sleep(0.15)

    room = y_server.rooms.pop(key, None)
    _mark_room_reset(
        key,
        close_reason=close_reason,
        room=room,
        room_dropped=room is not None,
        closed_connections=closed_connections,
        closed_webrtc_peers=closed_webrtc_peers,
    )
    _room_locks.pop(key, None)
    room_stopped = False
    ystore_stopped = False
    runtime_compaction_requested = False
    room_refs_released = False
    gc_collected = 0

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
        if ystore is not None:
            try:
                await _stop_ystore_maybe_async(ystore)
                ystore_stopped = True
            except Exception:
                ystore_stopped = False
            request_compaction = getattr(ystore, "request_runtime_compaction", None)
            if callable(request_compaction):
                try:
                    result = request_compaction(reason="room_reset")
                    runtime_compaction_requested = bool(await result) if inspect.isawaitable(result) else bool(result)
                except Exception:
                    runtime_compaction_requested = False
        room_refs_released = await _release_room_refs(key, room)
        if room_refs_released:
            try:
                gc_collected = int(gc.collect() or 0)
            except Exception:
                gc_collected = 0

    return {
        "webspace_id": key,
        "closed_webrtc_peers": closed_webrtc_peers,
        "closed_connections": closed_connections,
        "room_dropped": room is not None,
        "room_stopped": room_stopped,
        "ystore_stopped": ystore_stopped,
        "runtime_compaction_requested": runtime_compaction_requested,
        "room_refs_released": room_refs_released,
        "gc_collected": gc_collected,
    }


def _y_server_runtime_snapshot() -> dict[str, Any]:
    task = _y_server_task
    requested = bool(_y_server_started)
    started_event = bool(getattr(y_server, "started", None) and y_server.started.is_set())
    task_running = bool(task is not None and not task.done())
    task_done = bool(task is not None and task.done())
    task_cancelled = bool(task is not None and task.cancelled())
    rooms = getattr(y_server, "rooms", None)
    room_total = len(rooms) if isinstance(rooms, dict) else 0
    error: str | None = None
    if task_done and not task_cancelled:
        try:
            exc = task.exception()
        except Exception as exc:  # pragma: no cover - defensive runtime snapshot
            error = f"{type(exc).__name__}: {exc}"
        else:
            if exc is not None:
                error = f"{type(exc).__name__}: {exc}"
    ready = bool(requested and started_event and task_running and not error)
    return {
        "requested": requested,
        "started_event": started_event,
        "task_running": task_running,
        "task_done": task_done,
        "task_cancelled": task_cancelled,
        "room_total": room_total,
        "ready": ready,
        "error": error,
    }


def _gateway_lifecycle_manager() -> str:
    token = str(os.getenv("ADAOS_SUPERVISOR_ENABLED", "0") or "").strip().lower()
    return "supervisor" if token in {"1", "true", "yes", "on"} else "runtime"


def _gateway_transport_ownership_snapshot() -> dict[str, dict[str, Any]]:
    lifecycle_manager = _gateway_lifecycle_manager()
    try:
        from adaos.services import realtime_sidecar as _realtime_sidecar_mod

        route_contract = _realtime_sidecar_mod.realtime_sidecar_route_tunnel_contract()
    except Exception:
        route_contract = {}
    ws_contract = route_contract.get("ws") if isinstance(route_contract.get("ws"), dict) else {}
    yws_contract = route_contract.get("yws") if isinstance(route_contract.get("yws"), dict) else {}
    return {
        "ws": {
            "current_owner": ws_contract.get("current_owner") or "runtime",
            "lifecycle_manager": ws_contract.get("lifecycle_manager") or lifecycle_manager,
            "planned_owner": ws_contract.get("planned_owner") or "sidecar",
            "migration_phase": ws_contract.get("migration_phase") or "phase_2_route_tunnel_ownership",
            "logical_channels": list(
                ws_contract.get("logical_channels")
                or [
                    "hub_member.command",
                    "hub_member.event",
                    "hub_member.presence",
                ]
            ),
            "current_support": ws_contract.get("current_support") or "planned",
            "delegation_mode": ws_contract.get("delegation_mode") or "not_implemented",
            "listener_ready": bool(ws_contract.get("listener_ready")),
            "handoff_ready": bool(ws_contract.get("handoff_ready")),
            "handoff_blockers": list(
                ws_contract.get("blockers")
                or [
                    "browser route websocket still terminates in the runtime FastAPI app",
                ]
            ),
        },
        "yws": {
            "current_owner": yws_contract.get("current_owner") or "runtime",
            "lifecycle_manager": yws_contract.get("lifecycle_manager") or lifecycle_manager,
            "planned_owner": yws_contract.get("planned_owner") or "sidecar",
            "migration_phase": yws_contract.get("migration_phase") or "phase_2_route_tunnel_ownership",
            "logical_channels": list(
                yws_contract.get("logical_channels")
                or [
                    "hub_member.sync",
                ]
            ),
            "current_support": yws_contract.get("current_support") or "planned",
            "delegation_mode": yws_contract.get("delegation_mode") or "not_implemented",
            "listener_ready": bool(yws_contract.get("listener_ready")),
            "handoff_ready": bool(yws_contract.get("handoff_ready")),
            "handoff_blockers": list(
                yws_contract.get("blockers")
                or [
                    "Yjs websocket/session ownership still lives in the runtime gateway",
                ]
            ),
        },
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
    yws_state = state.get("yws") if isinstance(state.get("yws"), dict) else None
    if yws_state is not None:
        yws_state.update(_yws_storm_snapshot(now))
    room_details, room_aggregates = _room_debug_snapshot_all(now)
    if yws_state is not None:
        yws_state.update(room_aggregates)
    return {
        "transports": state,
        "servers": {
            "yws": _y_server_runtime_snapshot(),
        },
        "rooms": room_details,
        "ownership": _gateway_transport_ownership_snapshot(),
        "updated_at": now,
    }


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
                    _mark_room_created(webspace_id, room)
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
            await _stop_ystore_maybe_async(ystore)
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
    if not await _accept_websocket(websocket, channel="yws"):
        return
    _record_yws_open(webspace_id, dev_id)
    _track_yws_connection(webspace_id, websocket, device_id=dev_id)
    _transport_mark_open("yws")
    _publish_runtime_event(
        "browser.session.changed",
        {
            "device_id": dev_id,
            "webspace_id": webspace_id,
            "connection_state": "connected",
            "yjs_channel_state": "open",
            "source": "yws.gateway",
        },
    )
    await start_y_server()

    adapter: YWebsocket = FastAPIWebsocketAdapter(websocket, path=webspace_id)
    try:
        await y_server.serve(adapter)
    except RuntimeError:
        return
    finally:
        _untrack_yws_connection(webspace_id, websocket)
        _transport_mark_close("yws")
        _publish_runtime_event(
            "browser.session.changed",
            {
                "device_id": dev_id,
                "webspace_id": webspace_id,
                "connection_state": "closed",
                "yjs_channel_state": "closed",
                "source": "yws.gateway",
            },
        )
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
            _publish_bus(
                "device.registered",
                {
                    "device_id": captured_device,
                    "webspace_id": captured_ws,
                    "kind": "browser",
                },
            )
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

    if kind == "desktop.webspace.update":
        _publish_bus(
            "desktop.webspace.update",
            {
                "id": payload.get("id") or payload.get("webspace_id"),
                "title": payload.get("title"),
                "home_scenario": payload.get("home_scenario") or payload.get("scenario_id"),
            },
        )
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

    if kind == "desktop.webspace.ensure_dev":
        target = str((payload or {}).get("scenario_id") or "").strip()
        if not target:
            await _ack(False, error="scenario_id required")
            return None
        try:
            from adaos.services.scenario.webspace_runtime import ensure_dev_webspace_for_scenario

            result = await ensure_dev_webspace_for_scenario(
                target,
                requested_id=str((payload or {}).get("id") or (payload or {}).get("requested_id") or "").strip() or None,
                title=str((payload or {}).get("title") or "").strip() or None,
            )
            ensured_webspace_id = str(result.get("webspace_id") or "").strip() or None
            if ensured_webspace_id:
                await ensure_webspace_ready(
                    ensured_webspace_id,
                    scenario_id=str(result.get("home_scenario") or target).strip() or target,
                )
            await _ack(data=result)
        except ValueError as exc:
            await _ack(False, error=str(exc) or "scenario_id required")
        except Exception:
            _log.warning("desktop.webspace.ensure_dev failed scenario=%s", target, exc_info=True)
            await _ack(False, error="dev_webspace_unavailable")
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
    if not await _accept_websocket(websocket, channel="events"):
        return
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
    ws_loop = asyncio.get_running_loop()

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

            if msg.get("type") == "subscribe":
                added = _register_ws_event_subscriptions(
                    websocket,
                    ws_loop,
                    msg.get("topics"),
                )
                if added:
                    await _send_initial_ws_event_messages(websocket, added)
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
        _unregister_ws_event_subscriptions(websocket)
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
