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
import json
import logging
from typing import Any, Awaitable, Callable

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
    from aiortc.sdp import candidate_from_sdp
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "aiortc is required for WebRTC support. "
        "Install via `pip install aiortc` or add it to pyproject.toml."
    ) from exc

from adaos.services.webrtc.yjs_adapter import DataChannelYjsAdapter

_log = logging.getLogger("adaos.webrtc.peer")

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

        # Browser creates the DataChannels – hub receives them here.
        @self.pc.on("datachannel")
        def on_datachannel(channel) -> None:  # type: ignore[no-untyped-def]
            _log.info("datachannel opened: label=%s device=%s", channel.label, self.device_id)
            if channel.label == "events":
                self._setup_events_channel(channel)
            elif channel.label == "yjs":
                self._setup_yjs_channel(channel)
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
            if self.pc.connectionState in ("failed", "closed"):
                asyncio.ensure_future(self.close())

    # -- DataChannel handlers -------------------------------------------------

    def _setup_events_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *events* DataChannel to the same command processing as ``/ws``."""
        from adaos.services.yjs.gateway_ws import process_events_command

        state = {"webspace_id": self.webspace_id}

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

            asyncio.ensure_future(_handle())

    def _setup_yjs_channel(self, channel) -> None:  # type: ignore[no-untyped-def]
        """Bridge *yjs* DataChannel to ``ypy-websocket``."""
        self._yjs_adapter = DataChannelYjsAdapter(channel, self.webspace_id)
        self._yjs_task = asyncio.ensure_future(
            self._yjs_adapter.serve(),
            # name kwarg is py3.11+ for asyncio.ensure_future but Task() accepts it.
        )
        self._yjs_task.add_done_callback(
            lambda _t: _log.debug("yjs dc task done device=%s", self.device_id)
        )

    # -- SDP / ICE ------------------------------------------------------------

    async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
        offer = RTCSessionDescription(sdp=sdp, type=type)
        await self.pc.setRemoteDescription(offer)
        answer = await self.pc.createAnswer()
        # Run setLocalDescription in background — avoids blocking on STUN
        # resolution (2-5 s).  ICE candidates trickle via the on_ice callback.
        if self._local_desc_task and not self._local_desc_task.done():
            self._local_desc_task.cancel()
        self._local_desc_task = asyncio.ensure_future(
            self._set_local_description(answer)
        )
        return {
            "sdp": answer.sdp,
            "type": answer.type,
        }

    async def _set_local_description(self, answer: RTCSessionDescription) -> None:
        try:
            await self.pc.setLocalDescription(answer)
        except Exception:
            _log.warning("setLocalDescription failed device=%s", self.device_id, exc_info=True)

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

    # -- lifecycle ------------------------------------------------------------

    async def close(self) -> None:
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
        try:
            await self.pc.close()
        except Exception:
            pass
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
        # Try re-offer on existing peer (fast path for ICE restart).
        existing._send_ice = send_ice_cb
        existing.webspace_id = webspace_id
        try:
            return await existing.handle_offer(offer_sdp, offer_type)
        except Exception:
            _log.info("re-offer failed for device=%s, creating new peer", device_id)
            await existing.close()

    peer = HubPeer(device_id, webspace_id, send_ice_cb)
    _peers[device_id] = peer
    return await peer.handle_offer(offer_sdp, offer_type)


async def handle_remote_ice(device_id: str, candidate: dict[str, Any] | None) -> None:
    """Called from ``gateway_ws.py`` when browser sends ``rtc.ice``."""
    peer = _peers.get(device_id)
    if not peer:
        _log.debug("rtc.ice for unknown device=%s (ignored)", device_id)
        return
    await peer.add_ice_candidate(candidate or {})
