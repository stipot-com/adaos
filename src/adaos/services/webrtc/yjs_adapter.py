"""
Adapter bridging an aiortc RTCDataChannel to the ypy-websocket protocol.

Mirrors ``FastAPIWebsocketAdapter`` from ``gateway_ws.py`` but operates on a
WebRTC DataChannel instead of a FastAPI WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiortc import RTCDataChannel

from adaos.services.yjs.gateway_ws import y_server, start_y_server

_log = logging.getLogger("adaos.webrtc.yjs")


class DataChannelYjsAdapter:
    """ypy-websocket ``Websocket`` interface backed by a WebRTC DataChannel."""

    def __init__(self, dc: RTCDataChannel, webspace_id: str) -> None:
        self._dc = dc
        self._path = webspace_id
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

        @dc.on("message")
        def on_message(data: bytes | str) -> None:
            if isinstance(data, str):
                self._recv_queue.put_nowait(data.encode("utf-8"))
            elif isinstance(data, (bytes, bytearray)):
                self._recv_queue.put_nowait(bytes(data))

        @dc.on("close")
        def on_close() -> None:
            self._closed = True
            self._recv_queue.put_nowait(b"")

    # -- ypy-websocket Websocket interface ------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):  # noqa: ANN204
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration()

    async def send(self, message: bytes) -> None:
        try:
            self._dc.send(message)
        except Exception:
            return

    async def recv(self) -> bytes:
        if self._closed:
            raise RuntimeError("datachannel closed")
        data = await self._recv_queue.get()
        if not data and self._closed:
            raise RuntimeError("datachannel closed")
        return data

    # -- lifecycle ------------------------------------------------------------

    async def serve(self) -> None:
        """Start serving Yjs sync on this DataChannel."""
        await start_y_server()
        try:
            await y_server.serve(self)  # type: ignore[arg-type]
        except RuntimeError:
            pass
        finally:
            _log.info("yjs datachannel closed webspace=%s", self._path)
