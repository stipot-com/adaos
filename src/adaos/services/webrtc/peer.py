"""
Hub-side WebRTC peer connection management.

Each browser device that negotiates WebRTC gets a ``HubPeer`` instance holding
an ``RTCPeerConnection`` with two DataChannels:

* **events** – JSON commands (same protocol as the ``/ws`` endpoint)
* **yjs** – binary Yjs CRDT sync (same protocol as ``/yws``)

Signaling (SDP offer/answer + ICE candidates) flows through the existing
Events WebSocket which is already tunnelled via NATS.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import time
from typing import Any, Awaitable, Callable
from pathlib import Path

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
    from aiortc.contrib.media import MediaRelay
    from aiortc.sdp import candidate_from_sdp
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "aiortc is required for WebRTC support. "
        "Install via `pip install aiortc` or add it to pyproject.toml."
    ) from exc

from adaos.services.webrtc.yjs_adapter import DataChannelYjsAdapter
from adaos.services.media_library import (
    ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
    guess_media_type,
    media_file_path,
)
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit

_log = logging.getLogger("adaos.webrtc.peer")
_media_relay = MediaRelay()

STUN_CONFIG = RTCConfiguration(
    iceServers=[
        RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
    ]
)

# Active peers keyed by device_id.
_peers: dict[str, HubPeer] = {}


class HubPeer:
    """Manages a single WebRTC peer connection from a browser device."""

    def __init__(
        self,
        device_id: str,
        webspace_id: str,
        send_ice_cb: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.device_id = device_id
        self.webspace_id = webspace_id
        self._send_ice = send_ice_cb

        self.pc = RTCPeerConnection(configuration=STUN_CONFIG)
        self._yjs_adapter: DataChannelYjsAdapter | None = None
        self._yjs_task: asyncio.Task[None] | None = None
        self._local_desc_task: asyncio.Task[None] | None = None
        self._events_channel: Any | None = None
        self._yjs_channel: Any | None = None
        self._media_channel: Any | None = None
        self._incoming_tracks: dict[str, dict[str, Any]] = {}
        self._loopback_tracks: dict[str, dict[str, Any]] = {}
        self._media_upload: dict[str, Any] | None = None
        self._offer_lock = asyncio.Lock()

        # Browser creates the DataChannels – hub receives them here.
        @self.pc.on("datachannel")
        def on_datachannel(channel) -> None:  # type: ignore[no-untyped-def]
            _log.info("datachannel opened: label=%s device=%s", channel.label, self.device_id)
            if channel.label == "events":
                self._setup_events_channel(channel)
            elif channel.label == "yjs":
                self._setup_yjs_channel(channel)
            elif channel.label == "media":
                self._setup_media_channel(channel)
            else:
                _log.warning("unknown datachannel label=%s", channel.label)

        @self.pc.on("icecandidate")
        def on_ice(candidate) -> None:  # type: ignore[no-untyped-def]
            if candidate is None:
                return
            asyncio.ensure_future(self._send_ice({
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            }))

        @self.pc.on("connectionstatechange")
        def on_state() -> None:  # type: ignore[no-untyped-def]
            _log.info("peer %s connectionState=%s", self.device_id, self.pc.connectionState)
            self._emit_state_event(reason=f"connection_state:{self.pc.connectionState}")
            if self.pc.connectionState in ("failed", "closed"):
                asyncio.ensure_future(self.close())

        @self.pc.on("track")
        def on_track(track) -> None:  # type: ignore[no-untyped-def]
            track_id = str(getattr(track, "id", "") or f"{track.kind}:{len(self._incoming_tracks) + 1}")
            now = time.time()
            self._incoming_tracks[track_id] = {
                "id": track_id,
                "kind": str(getattr(track, "kind", "") or "unknown"),
                "ready_state": str(getattr(track, "readyState", "") or "live"),
                "received_at": now,
                "ended_at": None,
                "loopback": False,
            }
            _log.info(
                "media track received: kind=%s id=%s device=%s",
                getattr(track, "kind", "unknown"),
                track_id,
                self.device_id,
            )
            try:
                loopback_track = _media_relay.subscribe(track)
                sender = self.pc.addTrack(loopback_track)
                self._loopback_tracks[track_id] = {
                    "id": track_id,
                    "kind": str(getattr(track, "kind", "") or "unknown"),
                    "added_at": now,
                    "sender": sender,
                    "sender_kind": str(getattr(getattr(sender, "track", None), "kind", "") or getattr(track, "kind", "unknown")),
                }
                self._incoming_tracks[track_id]["loopback"] = True
            except Exception:
                _log.warning(
                    "failed to attach loopback media track device=%s id=%s",
                    self.device_id,
                    track_id,
                    exc_info=True,
                )
            self._emit_state_event(reason=f"track:{getattr(track, 'kind', 'unknown')}:received")

            @track.on("ended")
            async def on_track_ended() -> None:  # type: ignore[no-untyped-def]
                rec = self._incoming_tracks.get(track_id)
                if rec is not None:
                    rec["ready_state"] = "ended"
                    rec["ended_at"] = time.time()
                loopback = self._loopback_tracks.pop(track_id, None)
                await self._detach_loopback_sender(loopback)
                _log.info(
                    "media track ended: kind=%s id=%s device=%s",
                    getattr(track, "kind", "unknown"),
                    track_id,
                    self.device_id,
                )
                self._emit_state_event(reason=f"track:{getattr(track, 'kind', 'unknown')}:ended")

    # -- DataChannel handlers -------------------------------------------------

    def _setup_events_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *events* DataChannel to the same command processing as ``/ws``."""
        from adaos.services.yjs.gateway_ws import process_events_command

        self._events_channel = channel
        state = {"webspace_id": self.webspace_id}
        self._emit_state_event(reason="events_channel:open")

        async def _send(msg: dict[str, Any]) -> None:
            try:
                channel.send(json.dumps(msg))
            except Exception:
                _log.warning("events dc send failed device=%s", self.device_id, exc_info=True)

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
            try:
                msg = json.loads(text)
            except Exception:
                return
            ch = msg.get("ch")
            t = msg.get("t")
            if ch != "events" or t != "cmd":
                return
            cmd_id = msg.get("id", "")
            kind = msg.get("kind", "")
            payload = msg.get("payload") or {}

            async def _handle() -> None:
                new_ws = await process_events_command(
                    kind=kind,
                    cmd_id=cmd_id,
                    payload=payload,
                    device_id=self.device_id,
                    webspace_id=state["webspace_id"],
                    send_response=_send,
                )
                if new_ws:
                    state["webspace_id"] = new_ws
                    if self._yjs_channel is None:
                        self.webspace_id = new_ws

            asyncio.ensure_future(_handle())

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._events_channel = None
            self._emit_state_event(reason="events_channel:closed")

    def _setup_yjs_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *yjs* DataChannel to ``ypy-websocket``."""
        self._yjs_channel = channel
        self._yjs_adapter = DataChannelYjsAdapter(channel, self.webspace_id)
        self._yjs_task = asyncio.ensure_future(
            self._yjs_adapter.serve(),
            # name kwarg is py3.11+ for asyncio.ensure_future but Task() accepts it.
        )
        self._yjs_task.add_done_callback(
            lambda _t: _log.debug("yjs dc task done device=%s", self.device_id)
        )
        self._emit_state_event(reason="yjs_channel:open")

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._yjs_channel = None
            self._emit_state_event(reason="yjs_channel:closed")

    def _setup_media_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Accept direct binary media upload chunks over a dedicated DataChannel."""
        self._media_channel = channel

        async def _send(msg: dict[str, Any]) -> None:
            try:
                channel.send(json.dumps(msg))
            except Exception:
                _log.warning("media dc send failed device=%s", self.device_id, exc_info=True)

        async def _fail(upload_id: str, detail: str, *, code: str = "media_upload_failed") -> None:
            await _send({
                "ch": "media",
                "t": "error",
                "uploadId": upload_id,
                "error": code,
                "detail": detail,
            })

        async def _handle_json(msg: dict[str, Any]) -> None:
            upload_id = str(msg.get("uploadId") or "").strip()
            if not upload_id:
                return
            kind = str(msg.get("t") or "").strip().lower()
            if kind == "start":
                if self._media_upload is not None:
                    await _fail(upload_id, "media_upload_busy", code="media_upload_busy")
                    return
                try:
                    target = media_file_path(str(msg.get("filename") or ""))
                except ValueError as exc:
                    await _fail(upload_id, str(exc), code="media_upload_bad_request")
                    return
                expected_size = max(0, int(msg.get("sizeBytes") or 0))
                if expected_size > int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES):
                    await _fail(upload_id, "media_upload_too_large", code="media_upload_too_large")
                    return
                tmp_path = target.with_name(
                    f"{target.name}.p2p-{int(time.time() * 1000)}.part"
                )
                try:
                    handle = tmp_path.open("wb")
                except Exception as exc:
                    await _fail(upload_id, str(exc), code="media_upload_open_failed")
                    return
                self._media_upload = {
                    "upload_id": upload_id,
                    "target": target,
                    "tmp_path": tmp_path,
                    "handle": handle,
                    "size_bytes": 0,
                    "expected_size": expected_size,
                    "replaced": target.exists(),
                    "mime_type": guess_media_type(target.name),
                }
                await _send({
                    "ch": "media",
                    "t": "progress",
                    "uploadId": upload_id,
                    "receivedBytes": 0,
                })
                return
            if kind == "end":
                upload = self._media_upload
                if not upload or str(upload.get("upload_id") or "") != upload_id:
                    await _fail(upload_id, "media_upload_missing_session", code="media_upload_missing_session")
                    return
                try:
                    handle = upload.get("handle")
                    if handle:
                        handle.close()
                    target = Path(upload["target"])
                    tmp_path = Path(upload["tmp_path"])
                    tmp_path.replace(target)
                    size_bytes = int(upload.get("size_bytes") or 0)
                    mime_type = str(upload.get("mime_type") or guess_media_type(target.name))
                    self._cleanup_media_upload(remove_temp=False)
                    await _send({
                        "ch": "media",
                        "t": "done",
                        "uploadId": upload_id,
                        "sizeBytes": size_bytes,
                        "mimeType": mime_type,
                    })
                except Exception as exc:
                    self._cleanup_media_upload(remove_temp=True)
                    await _fail(upload_id, str(exc), code="media_upload_finalize_failed")
                return
            if kind == "abort":
                upload = self._media_upload
                if upload and str(upload.get("upload_id") or "") == upload_id:
                    self._cleanup_media_upload(remove_temp=True)
                return

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            async def _handle() -> None:
                if isinstance(data, str):
                    try:
                        msg = json.loads(data)
                    except Exception:
                        return
                    await _handle_json(msg if isinstance(msg, dict) else {})
                    return
                upload = self._media_upload
                if not upload:
                    return
                try:
                    blob = bytes(data)
                    size_bytes = int(upload.get("size_bytes") or 0) + len(blob)
                    if size_bytes > int(ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES):
                        upload_id = str(upload.get("upload_id") or "")
                        self._cleanup_media_upload(remove_temp=True)
                        await _fail(upload_id, "media_upload_too_large", code="media_upload_too_large")
                        return
                    handle = upload.get("handle")
                    if not handle:
                        upload_id = str(upload.get("upload_id") or "")
                        self._cleanup_media_upload(remove_temp=True)
                        await _fail(upload_id, "media_upload_no_handle", code="media_upload_no_handle")
                        return
                    handle.write(blob)
                    upload["size_bytes"] = size_bytes
                    await _send({
                        "ch": "media",
                        "t": "progress",
                        "uploadId": str(upload.get("upload_id") or ""),
                        "receivedBytes": size_bytes,
                    })
                except Exception as exc:
                    upload_id = str(upload.get("upload_id") or "")
                    self._cleanup_media_upload(remove_temp=True)
                    await _fail(upload_id, str(exc), code="media_upload_write_failed")

            asyncio.ensure_future(_handle())

        @channel.on("close")
        def on_close() -> None:  # type: ignore[no-untyped-def]
            self._cleanup_media_upload(remove_temp=True)
            self._media_channel = None

    def _cleanup_media_upload(self, *, remove_temp: bool) -> None:
        upload = self._media_upload
        self._media_upload = None
        if not upload:
            return
        handle = upload.get("handle")
        try:
            if handle:
                handle.close()
        except Exception:
            pass
        if remove_temp:
            try:
                Path(upload["tmp_path"]).unlink(missing_ok=True)
            except Exception:
                pass

    async def _detach_loopback_sender(self, loopback: dict[str, Any] | None) -> None:
        if not isinstance(loopback, dict):
            return
        sender = loopback.get("sender")
        if sender is None:
            return
        try:
            remove_track = getattr(self.pc, "removeTrack", None)
            if callable(remove_track):
                result = remove_track(sender)
                if asyncio.iscoroutine(result):
                    await result
                return
        except Exception:
            _log.debug("removeTrack failed device=%s", self.device_id, exc_info=True)
        try:
            replace_track = getattr(sender, "replaceTrack", None)
            if callable(replace_track):
                result = replace_track(None)
                if asyncio.iscoroutine(result):
                    await result
                return
        except Exception:
            _log.debug("replaceTrack(None) failed device=%s", self.device_id, exc_info=True)
        try:
            stop_sender = getattr(sender, "stop", None)
            if callable(stop_sender):
                result = stop_sender()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            _log.debug("sender stop failed device=%s", self.device_id, exc_info=True)

    # -- SDP / ICE ------------------------------------------------------------

    async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
        async with self._offer_lock:
            await self._cancel_local_desc_task()
            offer = RTCSessionDescription(sdp=sdp, type=type)
            await self.pc.setRemoteDescription(offer)
            answer = await self.pc.createAnswer()
            self._local_desc_task = asyncio.ensure_future(
                self._set_local_description(answer)
            )
        # Run setLocalDescription in background — avoids blocking on STUN
        # resolution (2-5 s).  ICE candidates trickle via the on_ice callback.
        return {
            "sdp": answer.sdp,
            "type": answer.type,
        }

    async def _set_local_description(self, answer: RTCSessionDescription) -> None:
        try:
            await self.pc.setLocalDescription(answer)
        except Exception:
            _log.warning("setLocalDescription failed device=%s", self.device_id, exc_info=True)

    async def _cancel_local_desc_task(self) -> None:
        task = self._local_desc_task
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.debug("previous local description task failed device=%s", self.device_id, exc_info=True)
        finally:
            if self._local_desc_task is task:
                self._local_desc_task = None

    async def add_ice_candidate(self, candidate_dict: dict[str, Any]) -> None:
        if not candidate_dict:
            return
        # Parse ICE candidate from SDP string format
        sdp_line = candidate_dict.get("candidate", "")
        if not sdp_line:
            return
        candidate = candidate_from_sdp(sdp_line)
        candidate.sdpMid = candidate_dict.get("sdpMid")
        candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
        await self.pc.addIceCandidate(candidate)

    def snapshot_record(self) -> dict[str, Any]:
        try:
            connection_state = str(getattr(self.pc, "connectionState", "") or "unknown").strip().lower() or "unknown"
        except Exception:
            connection_state = "unknown"
        try:
            events_state = str(getattr(getattr(self, "_events_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        except Exception:
            events_state = "missing"
        try:
            yjs_state = str(getattr(getattr(self, "_yjs_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        except Exception:
            yjs_state = "missing"
        incoming = self._incoming_tracks if isinstance(getattr(self, "_incoming_tracks", None), dict) else {}
        loopback = self._loopback_tracks if isinstance(getattr(self, "_loopback_tracks", None), dict) else {}
        incoming_audio_tracks = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "audio"
            and str(item.get("ready_state") or "live") != "ended"
        )
        incoming_video_tracks = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "video"
            and str(item.get("ready_state") or "live") != "ended"
        )
        loopback_audio_tracks = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "audio"
        )
        loopback_video_tracks = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "video"
        )
        return {
            "device_id": self.device_id,
            "webspace_id": str(getattr(self, "webspace_id", "") or ""),
            "connection_state": connection_state,
            "events_channel_state": events_state,
            "yjs_channel_state": yjs_state,
            "incoming_audio_tracks": incoming_audio_tracks,
            "incoming_video_tracks": incoming_video_tracks,
            "loopback_audio_tracks": loopback_audio_tracks,
            "loopback_video_tracks": loopback_video_tracks,
            "media_track_total": incoming_audio_tracks + incoming_video_tracks,
        }

    def _emit_state_event(self, *, reason: str) -> None:
        try:
            ctx = get_ctx()
        except Exception:
            return
        try:
            payload = {**self.snapshot_record(), "reason": str(reason or "state.changed")}
            bus_emit(ctx.bus, "webrtc.peer.state.changed", payload, "webrtc.peer")
        except Exception:
            _log.debug("failed to emit webrtc peer state device=%s reason=%s", self.device_id, reason, exc_info=True)

    # -- lifecycle ------------------------------------------------------------

    async def close(self) -> None:
        adapter = self._yjs_adapter
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            if self._yjs_task and not self._yjs_task.done():
                self._yjs_task.cancel()
        except Exception:
            pass
        try:
            if self._local_desc_task and not self._local_desc_task.done():
                self._local_desc_task.cancel()
        except Exception:
            pass
        self._incoming_tracks.clear()
        self._loopback_tracks.clear()
        self._cleanup_media_upload(remove_temp=True)
        yjs_task = self._yjs_task
        local_desc_task = self._local_desc_task
        self._yjs_task = None
        self._local_desc_task = None
        self._yjs_adapter = None
        self._events_channel = None
        self._yjs_channel = None
        self._media_channel = None
        if yjs_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await yjs_task
        if local_desc_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await local_desc_task
        try:
            await self.pc.close()
        except Exception:
            pass
        self._emit_state_event(reason="peer.closed")
        # Only remove ourselves — a replacement peer may already be registered.
        if _peers.get(self.device_id) is self:
            del _peers[self.device_id]
        _log.info("peer closed device=%s", self.device_id)


# -- Public API ---------------------------------------------------------------


async def handle_rtc_offer(
    offer_sdp: str,
    offer_type: str,
    device_id: str,
    webspace_id: str,
    send_ice_cb: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, str]:
    """
    Called from ``gateway_ws.py`` when browser sends ``rtc.offer``.

    Returns the SDP answer payload ``{"sdp": ..., "type": "answer"}``.
    """
    existing = _peers.get(device_id)
    if existing:
        state = str(getattr(existing.pc, "connectionState", "") or "").strip().lower()
        if state not in {"failed", "closed"}:
            existing.webspace_id = webspace_id
            existing._send_ice = send_ice_cb
            existing._emit_state_event(reason="offer.renegotiate")
            return await existing.handle_offer(offer_sdp, offer_type)
        _log.info(
            "replacing closed/failed peer for device=%s on new offer state=%s",
            device_id,
            state or "unknown",
        )
        await existing.close()

    peer = HubPeer(device_id, webspace_id, send_ice_cb)
    _peers[device_id] = peer
    peer._emit_state_event(reason="offer.accepted")
    return await peer.handle_offer(offer_sdp, offer_type)


async def handle_remote_ice(device_id: str, candidate: dict[str, Any] | None) -> None:
    """Called from ``gateway_ws.py`` when browser sends ``rtc.ice``."""
    peer = _peers.get(device_id)
    if not peer:
        _log.debug("rtc.ice for unknown device=%s (ignored)", device_id)
        return
    await peer.add_ice_candidate(candidate or {})


async def close_peers_for_webspace(
    webspace_id: str,
    *,
    reason: str = "webspace_reload",
) -> int:
    key = str(webspace_id or "").strip() or "default"
    peers = [
        peer
        for peer in list(_peers.values())
        if str(getattr(peer, "webspace_id", "") or "").strip() == key
    ]
    if not peers:
        return 0
    _log.info("closing webrtc peers for webspace=%s count=%s reason=%s", key, len(peers), reason)
    closed = 0
    for peer in peers:
        try:
            await peer.close()
            closed += 1
        except Exception:
            _log.debug(
                "failed to close webrtc peer device=%s webspace=%s reason=%s",
                getattr(peer, "device_id", "unknown"),
                key,
                reason,
                exc_info=True,
            )
    return closed


def webrtc_peer_snapshot(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    peers: list[dict[str, Any]] = []
    connection_states: dict[str, int] = {}
    open_events_channels = 0
    open_yjs_channels = 0
    incoming_audio_tracks = 0
    incoming_video_tracks = 0
    loopback_audio_tracks = 0
    loopback_video_tracks = 0
    for device_id, peer in list(_peers.items()):
        try:
            state = str(getattr(peer.pc, "connectionState", "") or "unknown").strip().lower() or "unknown"
        except Exception:
            state = "unknown"
        connection_states[state] = int(connection_states.get(state) or 0) + 1

        try:
            events_state = str(getattr(getattr(peer, "_events_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        except Exception:
            events_state = "missing"
        try:
            yjs_state = str(getattr(getattr(peer, "_yjs_channel", None), "readyState", "") or "missing").strip().lower() or "missing"
        except Exception:
            yjs_state = "missing"
        if events_state == "open":
            open_events_channels += 1
        if yjs_state == "open":
            open_yjs_channels += 1
        incoming = peer._incoming_tracks if isinstance(getattr(peer, "_incoming_tracks", None), dict) else {}
        loopback = peer._loopback_tracks if isinstance(getattr(peer, "_loopback_tracks", None), dict) else {}
        peer_incoming_audio = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "audio"
            and str(item.get("ready_state") or "live") != "ended"
        )
        peer_incoming_video = sum(
            1
            for item in incoming.values()
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "video"
            and str(item.get("ready_state") or "live") != "ended"
        )
        peer_loopback_audio = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "audio"
        )
        peer_loopback_video = sum(
            1
            for item in loopback.values()
            if isinstance(item, dict) and str(item.get("kind") or "") == "video"
        )
        incoming_audio_tracks += peer_incoming_audio
        incoming_video_tracks += peer_incoming_video
        loopback_audio_tracks += peer_loopback_audio
        loopback_video_tracks += peer_loopback_video
        peers.append(
            {
                "device_id": device_id,
                "webspace_id": str(getattr(peer, "webspace_id", "") or ""),
                "connection_state": state,
                "events_channel_state": events_state,
                "yjs_channel_state": yjs_state,
                "incoming_audio_tracks": peer_incoming_audio,
                "incoming_video_tracks": peer_incoming_video,
                "loopback_audio_tracks": peer_loopback_audio,
                "loopback_video_tracks": peer_loopback_video,
                "media_track_total": peer_incoming_audio + peer_incoming_video,
            }
        )
    return {
        "peer_total": len(peers),
        "connected_peers": int(connection_states.get("connected") or 0),
        "connecting_peers": int(connection_states.get("connecting") or 0),
        "open_events_channels": open_events_channels,
        "open_yjs_channels": open_yjs_channels,
        "incoming_audio_tracks": incoming_audio_tracks,
        "incoming_video_tracks": incoming_video_tracks,
        "loopback_audio_tracks": loopback_audio_tracks,
        "loopback_video_tracks": loopback_video_tracks,
        "connection_states": connection_states,
        "peers": peers,
        "updated_at": now,
    }
