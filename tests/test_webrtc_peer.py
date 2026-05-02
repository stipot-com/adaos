from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_peer_module(monkeypatch):
    fake_aiortc = ModuleType("aiortc")

    class DummyRTCPeerConnection:
        def __init__(self, configuration=None):
            self.configuration = configuration
            self.connectionState = "new"

        def on(self, _event):
            def decorator(fn):
                return fn

            return decorator

    class DummyRTCSessionDescription:
        def __init__(self, sdp: str, type: str):
            self.sdp = sdp
            self.type = type

    class DummyRTCIceServer:
        def __init__(self, urls):
            self.urls = urls

    class DummyRTCConfiguration:
        def __init__(self, iceServers):
            self.iceServers = iceServers

    fake_aiortc.RTCPeerConnection = DummyRTCPeerConnection
    fake_aiortc.RTCSessionDescription = DummyRTCSessionDescription
    fake_aiortc.RTCIceCandidate = object
    fake_aiortc.RTCConfiguration = DummyRTCConfiguration
    fake_aiortc.RTCIceServer = DummyRTCIceServer

    fake_aiortc_contrib = ModuleType("aiortc.contrib")
    fake_aiortc_contrib_media = ModuleType("aiortc.contrib.media")

    class DummyMediaRelay:
        def subscribe(self, track):
            return track

    fake_aiortc_contrib_media.MediaRelay = DummyMediaRelay

    fake_aiortc_sdp = ModuleType("aiortc.sdp")
    fake_aiortc_sdp.candidate_from_sdp = (
        lambda line: SimpleNamespace(candidate=line, sdpMid=None, sdpMLineIndex=None)
    )

    fake_yjs_adapter = ModuleType("adaos.services.webrtc.yjs_adapter")

    class DummyDataChannelYjsAdapter:
        def __init__(self, dc, webspace_id: str):
            self.dc = dc
            self.webspace_id = webspace_id

        def close(self) -> None:
            return None

        async def serve(self) -> None:
            return None

    fake_yjs_adapter.DataChannelYjsAdapter = DummyDataChannelYjsAdapter

    fake_media_library = ModuleType("adaos.services.media_library")
    fake_media_library.ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES = 10_000_000
    fake_media_library.guess_media_type = lambda name: "video/mp4"
    fake_media_library.media_file_path = lambda name: Path(name or "media.bin")

    fake_agent_context = ModuleType("adaos.services.agent_context")
    fake_agent_context.get_ctx = lambda: SimpleNamespace(bus=None)

    fake_eventbus = ModuleType("adaos.services.eventbus")
    fake_eventbus.emit = lambda *args, **kwargs: None

    monkeypatch.setitem(sys.modules, "aiortc", fake_aiortc)
    monkeypatch.setitem(sys.modules, "aiortc.contrib", fake_aiortc_contrib)
    monkeypatch.setitem(sys.modules, "aiortc.contrib.media", fake_aiortc_contrib_media)
    monkeypatch.setitem(sys.modules, "aiortc.sdp", fake_aiortc_sdp)
    monkeypatch.setitem(sys.modules, "adaos.services.webrtc.yjs_adapter", fake_yjs_adapter)
    monkeypatch.setitem(sys.modules, "adaos.services.media_library", fake_media_library)
    monkeypatch.setitem(sys.modules, "adaos.services.agent_context", fake_agent_context)
    monkeypatch.setitem(sys.modules, "adaos.services.eventbus", fake_eventbus)
    monkeypatch.delitem(sys.modules, "adaos.services.webrtc.peer", raising=False)

    module = importlib.import_module("adaos.services.webrtc.peer")
    return importlib.reload(module)


def test_handle_rtc_offer_reuses_existing_clean_peer(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class ExistingPeer:
        def __init__(self) -> None:
            self.pc = SimpleNamespace(connectionState="connected")
            self.webspace_id = "desk-old"
            self._send_ice = None
            self.close_called = False
            self.handled_offers: list[tuple[str, str]] = []
            self.emitted_reasons: list[str] = []

        async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
            self.handled_offers.append((sdp, type))
            return {"sdp": "answer-sdp", "type": "answer"}

        async def close(self) -> None:
            self.close_called = True

        def is_reusable_for_offer(self) -> bool:
            return True

        def _emit_state_event(self, *, reason: str) -> None:
            self.emitted_reasons.append(reason)

    existing = ExistingPeer()
    peer_mod._peers.clear()
    peer_mod._peers["browser-1"] = existing

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    answer = asyncio.run(
        peer_mod.handle_rtc_offer(
            offer_sdp="offer-sdp",
            offer_type="offer",
            device_id="browser-1",
            webspace_id="desk-next",
            send_ice_cb=send_ice_cb,
        )
    )

    assert answer == {"sdp": "answer-sdp", "type": "answer"}
    assert peer_mod._peers["browser-1"] is existing
    assert existing.handled_offers == [("offer-sdp", "offer")]
    assert existing.close_called is False
    assert existing.webspace_id == "desk-next"
    assert existing._send_ice is send_ice_cb
    assert existing.emitted_reasons == ["offer.renegotiate"]


def test_hub_peer_is_not_reusable_with_live_channels(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    peer = peer_mod.HubPeer("browser-live", "default", send_ice_cb)
    peer._events_channel = SimpleNamespace(readyState="open")

    try:
        assert peer.is_reusable_for_offer() is False
    finally:
        asyncio.run(peer.close())


def test_setup_yjs_channel_replaces_previous_adapter_and_channel(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class TrackingAdapter:
        def __init__(self, dc, webspace_id: str):
            self.dc = dc
            self.webspace_id = webspace_id
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def serve(self) -> None:
            await asyncio.sleep(3600)

    class DummyChannel:
        def __init__(self, label: str = "yjs") -> None:
            self.label = label
            self.readyState = "open"
            self.close_called = 0

        def on(self, _event):
            def decorator(fn):
                return fn

            return decorator

        def close(self) -> None:
            self.close_called += 1

    peer_mod.DataChannelYjsAdapter = TrackingAdapter

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    async def _run() -> None:
        peer = peer_mod.HubPeer("browser-yjs", "default", send_ice_cb)
        first = DummyChannel()
        second = DummyChannel()
        peer._setup_yjs_channel(first)
        old_adapter = peer._yjs_adapter
        old_task = peer._yjs_task
        assert old_adapter is not None
        assert old_task is not None

        peer._setup_yjs_channel(second)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_adapter.closed is True
        assert first.close_called == 1
        assert old_task.cancelling() > 0 or old_task.cancelled() or old_task.done()
        assert peer._yjs_channel is second
        await peer.close()

    asyncio.run(_run())


def test_request_webio_stream_snapshots_extracts_node_qualified_receiver(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)
    published: list[tuple[str, dict[str, object], str]] = []

    peer_mod.get_ctx = lambda: SimpleNamespace(bus=object())
    peer_mod.bus_emit = lambda bus, topic, payload, source: published.append((topic, payload, source))

    peer_mod._request_webio_stream_snapshots(
        {"webio.stream.default.nodes.member-01.telemetry.feed"},
        transport="webrtc_data:events",
    )

    assert published == [
        (
            "webio.stream.snapshot.requested",
            {
                "topic": "webio.stream.default.nodes.member-01.telemetry.feed",
                "webspace_id": "default",
                "receiver": "telemetry.feed",
                "transport": "webrtc_data:events",
                "node_id": "member-01",
            },
            "webrtc.peer",
        )
    ]


def test_request_webio_stream_snapshots_extracts_global_node_receiver(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)
    published: list[tuple[str, dict[str, object], str]] = []

    peer_mod.get_ctx = lambda: SimpleNamespace(bus=object())
    peer_mod.bus_emit = lambda bus, topic, payload, source: published.append((topic, payload, source))

    peer_mod._request_webio_stream_snapshots(
        {"webio.stream.nodes.member-01.telemetry.feed"},
        transport="webrtc_data:events",
    )

    assert published == [
        (
            "webio.stream.snapshot.requested",
            {
                "topic": "webio.stream.nodes.member-01.telemetry.feed",
                "webspace_id": "default",
                "receiver": "telemetry.feed",
                "transport": "webrtc_data:events",
                "node_id": "member-01",
            },
            "webrtc.peer",
        )
    ]


def test_handle_rtc_offer_replaces_failed_peer(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class FailedPeer:
        def __init__(self) -> None:
            self.pc = SimpleNamespace(connectionState="failed")
            self.close_called = False

        async def close(self) -> None:
            self.close_called = True

    class NewPeer:
        def __init__(self, device_id: str, webspace_id: str, send_ice_cb) -> None:
            self.device_id = device_id
            self.webspace_id = webspace_id
            self._send_ice = send_ice_cb
            self.pc = SimpleNamespace(connectionState="new")
            self.handled_offers: list[tuple[str, str]] = []
            self.emitted_reasons: list[str] = []

        async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
            self.handled_offers.append((sdp, type))
            return {"sdp": "fresh-answer", "type": "answer"}

        def _emit_state_event(self, *, reason: str) -> None:
            self.emitted_reasons.append(reason)

    monkeypatch.setattr(peer_mod, "HubPeer", NewPeer)
    failed = FailedPeer()
    peer_mod._peers.clear()
    peer_mod._peers["browser-2"] = failed

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    answer = asyncio.run(
        peer_mod.handle_rtc_offer(
            offer_sdp="offer-sdp",
            offer_type="offer",
            device_id="browser-2",
            webspace_id="desk",
            send_ice_cb=send_ice_cb,
        )
    )

    new_peer = peer_mod._peers["browser-2"]
    assert failed.close_called is True
    assert isinstance(new_peer, NewPeer)
    assert answer == {"sdp": "fresh-answer", "type": "answer"}
    assert new_peer.handled_offers == [("offer-sdp", "offer")]
    assert new_peer.webspace_id == "desk"
    assert new_peer._send_ice is send_ice_cb
    assert new_peer.emitted_reasons == ["offer.accepted"]


def test_handle_rtc_offer_replaces_disconnected_peer(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class DisconnectedPeer:
        def __init__(self) -> None:
            self.pc = SimpleNamespace(connectionState="disconnected")
            self.close_called = False

        async def close(self) -> None:
            self.close_called = True

        def _connection_state(self) -> str:
            return "disconnected"

        def is_reusable_for_offer(self) -> bool:
            return False

    class NewPeer:
        def __init__(self, device_id: str, webspace_id: str, send_ice_cb) -> None:
            self.device_id = device_id
            self.webspace_id = webspace_id
            self._send_ice = send_ice_cb
            self.pc = SimpleNamespace(connectionState="new")
            self.handled_offers: list[tuple[str, str]] = []
            self.emitted_reasons: list[str] = []

        async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
            self.handled_offers.append((sdp, type))
            return {"sdp": "fresh-answer", "type": "answer"}

        def _emit_state_event(self, *, reason: str) -> None:
            self.emitted_reasons.append(reason)

    monkeypatch.setattr(peer_mod, "HubPeer", NewPeer)
    existing = DisconnectedPeer()
    peer_mod._peers.clear()
    peer_mod._peers["browser-3"] = existing

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    answer = asyncio.run(
        peer_mod.handle_rtc_offer(
            offer_sdp="offer-sdp",
            offer_type="offer",
            device_id="browser-3",
            webspace_id="desk",
            send_ice_cb=send_ice_cb,
        )
    )

    new_peer = peer_mod._peers["browser-3"]
    assert existing.close_called is True
    assert isinstance(new_peer, NewPeer)
    assert answer == {"sdp": "fresh-answer", "type": "answer"}
    assert new_peer.handled_offers == [("offer-sdp", "offer")]
    assert new_peer.emitted_reasons == ["offer.accepted"]


def test_handle_rtc_offer_continues_when_existing_close_hangs(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)
    monkeypatch.setattr(peer_mod, "_REPLACE_CLOSE_TIMEOUT_SECONDS", 0.01)

    class HangingPeer:
        def __init__(self) -> None:
            self.pc = SimpleNamespace(connectionState="failed")
            self.close_called = False

        async def close(self) -> None:
            self.close_called = True
            await asyncio.sleep(3600)

        def _connection_state(self) -> str:
            return "failed"

        def is_reusable_for_offer(self) -> bool:
            return False

    class NewPeer:
        def __init__(self, device_id: str, webspace_id: str, send_ice_cb) -> None:
            self.device_id = device_id
            self.webspace_id = webspace_id
            self._send_ice = send_ice_cb
            self.pc = SimpleNamespace(connectionState="new")
            self.handled_offers: list[tuple[str, str]] = []
            self.emitted_reasons: list[str] = []

        async def handle_offer(self, sdp: str, type: str = "offer") -> dict[str, str]:
            self.handled_offers.append((sdp, type))
            return {"sdp": "fresh-answer", "type": "answer"}

        def _emit_state_event(self, *, reason: str) -> None:
            self.emitted_reasons.append(reason)

    monkeypatch.setattr(peer_mod, "HubPeer", NewPeer)
    hanging = HangingPeer()
    peer_mod._peers.clear()
    peer_mod._peers["browser-timeout"] = hanging

    async def send_ice_cb(candidate: dict[str, object]) -> None:
        return None

    answer = asyncio.run(
        peer_mod.handle_rtc_offer(
            offer_sdp="offer-sdp",
            offer_type="offer",
            device_id="browser-timeout",
            webspace_id="desk",
            send_ice_cb=send_ice_cb,
        )
    )

    new_peer = peer_mod._peers["browser-timeout"]
    assert hanging.close_called is True
    assert isinstance(new_peer, NewPeer)
    assert answer == {"sdp": "fresh-answer", "type": "answer"}
    assert new_peer.handled_offers == [("offer-sdp", "offer")]
    assert new_peer.emitted_reasons == ["offer.accepted"]


def test_close_peers_for_webspace_closes_matching_peers(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class DummyPeer:
        def __init__(self, device_id: str, webspace_id: str) -> None:
            self.device_id = device_id
            self.webspace_id = webspace_id
            self.closed = False

        async def close(self) -> None:
            self.closed = True
            if peer_mod._peers.get(self.device_id) is self:
                del peer_mod._peers[self.device_id]

    peer_mod._peers.clear()
    keep = DummyPeer("browser-keep", "desktop")
    close_a = DummyPeer("browser-a", "default")
    close_b = DummyPeer("browser-b", "default")
    peer_mod._peers[keep.device_id] = keep
    peer_mod._peers[close_a.device_id] = close_a
    peer_mod._peers[close_b.device_id] = close_b

    closed = asyncio.run(peer_mod.close_peers_for_webspace("default", reason="room_reset"))

    assert closed == 2
    assert close_a.closed is True
    assert close_b.closed is True
    assert keep.closed is False
    assert list(peer_mod._peers.keys()) == ["browser-keep"]


def test_webrtc_peer_snapshot_prunes_stale_peers(monkeypatch) -> None:
    peer_mod = _load_peer_module(monkeypatch)

    class StalePeer:
        def __init__(self, device_id: str, webspace_id: str) -> None:
            self.device_id = device_id
            self.webspace_id = webspace_id
            self.pc = SimpleNamespace(connectionState="disconnected")
            self._events_channel = None
            self._yjs_channel = None
            self._incoming_tracks = {}
            self._loopback_tracks = {}
            self.scheduled_reasons: list[str] = []

        def is_stale(self, *, now_ts=None) -> bool:
            return True

        def _schedule_close(self, *, reason: str, delay: float = 0.0) -> bool:
            self.scheduled_reasons.append(f"{reason}:{delay}")
            return True

        def _connection_state(self) -> str:
            return "disconnected"

        def _events_state(self) -> str:
            return "missing"

        def _yjs_state(self) -> str:
            return "missing"

    peer_mod._peers.clear()
    stale = StalePeer("browser-stale", "default")
    peer_mod._peers[stale.device_id] = stale

    snapshot = peer_mod.webrtc_peer_snapshot()

    assert snapshot["peer_total"] == 0
    assert snapshot["pruned_stale_peers"] == 1
    assert stale.device_id not in peer_mod._peers
    assert stale.scheduled_reasons == ["stale_peer_prune:0.0"]
