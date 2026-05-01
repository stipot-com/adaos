from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from nats.errors import ProtocolError
from nats.protocol.parser import Parser

import adaos.services.nats_ws_transport as nats_ws_transport
from adaos.services.nats_ws_transport import (
    WebSocketTransportAiohttp,
    WebSocketTransportWebsockets,
    _extract_route_subjects,
    _ws_control_intercept_enabled_from_env,
    _ws_data_heartbeat_s_from_env,
    _ws_data_ping_s_from_env,
    _ws_impl_from_env,
    _ws_proxy_from_env,
    _without_adaos_legacy_ws_proxy_env,
)


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.001)


class _FakeNC:
    def __init__(self) -> None:
        self.msgs: list[tuple[int, bytes, bytes, bytes, bytes | None]] = []
        self.pings = 0
        self.pongs = 0
        self.infos: list[dict] = []
        self.errors: list[str] = []
        self._ps = None

    async def _process_msg(
        self, sid: int, subject: bytes, reply: bytes, payload: bytes, hdr: bytes | None
    ) -> None:
        self.msgs.append((sid, subject, reply, payload, hdr))

    async def _process_ping(self) -> None:
        self.pings += 1

    async def _process_pong(self) -> None:
        self.pongs += 1

    async def _process_info(self, info: dict) -> None:
        self.infos.append(info)

    async def _process_err(self, err: str) -> None:
        self.errors.append(err)


class _FakeWebsocketsWS:
    def __init__(self, frames: list[bytes | str], *, block_when_empty: bool = False) -> None:
        self._frames = list(frames)
        self._block_when_empty = block_when_empty
        self.sent: list[bytes] = []
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"

    async def recv(self) -> bytes | str:
        if not self._frames:
            if self._block_when_empty:
                await asyncio.Future()
            raise RuntimeError("no more frames")
        return self._frames.pop(0)

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True
        self.state = "CLOSED"


class _FakeWebsocketsPingWS:
    def __init__(self) -> None:
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"
        self.pings: list[bytes] = []
        self.subprotocol = "nats"
        self.remote_address = ("127.0.0.1", 443)
        self.local_address = ("127.0.0.1", 12345)
        self.transport = SimpleNamespace(get_extra_info=lambda name: None)

    async def recv(self) -> bytes | str:
        await asyncio.Future()

    async def ping(self, payload: bytes = b"") -> None:
        self.pings.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True
        self.state = "CLOSED"

    async def wait_closed(self) -> None:
        return None


class _BlockingWebsocketsWS:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"
        self.recv_calls = 0
        self.recv_started = asyncio.Event()
        self.recv_release = asyncio.Event()
        self.recv_cancelled = asyncio.Event()
        self.recv_payload: bytes | str = b"INFO {}\r\n"

    async def recv(self) -> bytes | str:
        self.recv_calls += 1
        self.recv_started.set()
        try:
            await self.recv_release.wait()
        except asyncio.CancelledError:
            self.recv_cancelled.set()
            raise
        self.recv_release.clear()
        return self.recv_payload

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True
        self.state = "CLOSED"


class _FairWebsocketsWS:
    def __init__(self, transport: WebSocketTransportWebsockets, *, replenish_limit: int = 20) -> None:
        self._transport = transport
        self._replenish_limit = replenish_limit
        self.sent: list[bytes] = []
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"
        self.recv_calls = 0

    async def recv(self) -> bytes | str:
        self.recv_calls += 1
        if self.recv_calls == 1:
            return b"INFO {}\r\n"
        await asyncio.Future()

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))
        if len(self.sent) <= self._replenish_limit:
            self._transport.write(b"PUB route.to_browser.sn_1--k 2\r\nok\r\n")
        await asyncio.sleep(0.01)

    async def close(self) -> None:
        self.closed = True
        self.state = "CLOSED"


class _InboundBurstWebsocketsWS:
    def __init__(self, transport: WebSocketTransportWebsockets, payload: bytes) -> None:
        self._transport = transport
        self._payload = payload
        self.sent: list[bytes] = []
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"
        self.recv_calls = 0

    async def recv(self) -> bytes | str:
        self.recv_calls += 1
        if self.recv_calls == 1:
            self._transport.write(self._payload)
        await asyncio.sleep(0)
        return b"MSG route.to_browser 1 2\r\nok\r\n"

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True
        self.state = "CLOSED"


class _FakeAiohttpMsg:
    def __init__(self, kind: object, data: bytes | str) -> None:
        self.type = kind
        self.data = data


class _FakeAiohttpWS:
    def __init__(self, messages: list[_FakeAiohttpMsg]) -> None:
        self._messages = list(messages)
        self.closed = False
        self.close_code = None
        self.close_reason = None

    async def receive(self) -> _FakeAiohttpMsg:
        if not self._messages:
            raise RuntimeError("no more messages")
        return self._messages.pop(0)

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> None:
        return None


class _FakeAiohttpPingWS:
    def __init__(self) -> None:
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self._response = SimpleNamespace(headers={})
        self.pings: list[bytes] = []

    async def ping(self, payload: bytes = b"") -> None:
        self.pings.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> None:
        return None


@pytest.mark.asyncio
async def test_parser_ping_injected_mid_payload_corrupts_stream() -> None:
    nc = _FakeNC()
    parser = Parser(nc)
    nc._ps = parser

    await parser.parse(b"MSG route.to_browser 1 10\r\nhello")

    with pytest.raises(ProtocolError):
        await parser.parse(b"PING\r\nworld\r\n")

    assert nc.pings == 0
    assert [msg[3] for msg in nc.msgs] == [b"helloPING\r"]


@pytest.mark.asyncio
async def test_websockets_transport_replies_to_standalone_ping_immediately() -> None:
    nc = _FakeNC()
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = nc
    ws = _FakeWebsocketsWS([b"PING\r\n", b"INFO {}\r\n"])
    transport._ws = ws

    data = await transport.readline()

    assert nc.pings == 0
    assert ws.sent == [b"PONG\r\n"]
    assert data == b"INFO {}\r\n"


@pytest.mark.asyncio
async def test_websockets_transport_replies_to_coalesced_ping_before_msg() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    payload = b"MSG route.to_browser 1 2\r\nok\r\n"
    ws = _FakeWebsocketsWS([b"PING\r\n" + payload])
    transport._ws = ws

    data = await transport.readline()

    assert ws.sent == [b"PONG\r\n"]
    assert data == payload


@pytest.mark.asyncio
async def test_websockets_transport_replies_to_coalesced_ping_after_msg() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    payload = b"MSG route.to_browser 1 2\r\nok\r\n"
    ws = _FakeWebsocketsWS([payload + b"PING\r\n"])
    transport._ws = ws

    data = await transport.readline()

    assert ws.sent == [b"PONG\r\n"]
    assert data == payload


@pytest.mark.asyncio
async def test_websockets_transport_does_not_reply_to_ping_inside_msg_payload() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    payload = b"MSG route.to_browser 1 6\r\nPING\r\n\r\n"
    ws = _FakeWebsocketsWS([payload])
    transport._ws = ws

    data = await transport.readline()

    assert ws.sent == []
    assert data == payload


@pytest.mark.asyncio
async def test_aiohttp_transport_consumes_standalone_pong_out_of_band() -> None:
    pytest.importorskip("aiohttp")

    nc = _FakeNC()
    transport = WebSocketTransportAiohttp()
    try:
        binary = transport._aiohttp.WSMsgType.BINARY
        transport._adaos_nc = nc
        transport._ws = _FakeAiohttpWS(
            [
                _FakeAiohttpMsg(binary, b"PONG\r\n"),
                _FakeAiohttpMsg(binary, b"INFO {}\r\n"),
            ]
        )

        data = await transport.readline()

        assert nc.pings == 0
        assert nc.pongs == 1
        assert data == b"INFO {}\r\n"
    finally:
        await transport._client.close()


@pytest.mark.asyncio
async def test_aiohttp_transport_replies_to_standalone_ping_immediately() -> None:
    pytest.importorskip("aiohttp")

    transport = WebSocketTransportAiohttp()
    try:
        binary = transport._aiohttp.WSMsgType.BINARY
        transport._adaos_nc = _FakeNC()
        transport._ws = _FakeAiohttpWS(
            [
                _FakeAiohttpMsg(binary, b"PING\r\n"),
                _FakeAiohttpMsg(binary, b"INFO {}\r\n"),
            ]
        )

        sent: list[bytes] = []

        async def _send_bytes(payload: bytes) -> None:
            sent.append(bytes(payload))

        setattr(transport._ws, "send_bytes", _send_bytes)
        data = await transport.readline()

        assert sent == [b"PONG\r\n"]
        assert data == b"INFO {}\r\n"
    finally:
        await transport._client.close()


@pytest.mark.asyncio
async def test_aiohttp_transport_replies_to_coalesced_ping_before_msg() -> None:
    pytest.importorskip("aiohttp")

    transport = WebSocketTransportAiohttp()
    try:
        binary = transport._aiohttp.WSMsgType.BINARY
        transport._adaos_nc = _FakeNC()
        payload = b"MSG route.to_browser 1 2\r\nok\r\n"
        transport._ws = _FakeAiohttpWS([_FakeAiohttpMsg(binary, b"PING\r\n" + payload)])
        sent: list[bytes] = []

        async def _send_bytes(payload: bytes) -> None:
            sent.append(bytes(payload))

        setattr(transport._ws, "send_bytes", _send_bytes)
        data = await transport.readline()

        assert sent == [b"PONG\r\n"]
        assert data == payload
    finally:
        await transport._client.close()


@pytest.mark.asyncio
async def test_websockets_transport_falls_back_to_raw_pong_without_nats_client() -> None:
    transport = WebSocketTransportWebsockets()
    ws = _FakeWebsocketsWS([b"PING\r\n", b"INFO {}\r\n"])
    transport._ws = ws

    data = await transport.readline()

    assert ws.sent == [b"PONG\r\n"]
    assert data == b"INFO {}\r\n"


@pytest.mark.asyncio
async def test_websockets_transport_does_not_start_io_recv_while_direct_recv_pending() -> None:
    transport = WebSocketTransportWebsockets()
    ws = _BlockingWebsocketsWS()
    transport._ws = ws

    read_task = asyncio.create_task(transport._direct_readline())
    await asyncio.wait_for(ws.recv_started.wait(), timeout=1.0)

    transport._adaos_nc = _FakeNC()
    transport.write(b"SUB test 1\r\n")
    await transport.drain()

    assert ws.recv_calls == 1
    assert transport._io_task is None

    ws.recv_release.set()
    data = await asyncio.wait_for(read_task, timeout=1.0)
    assert data == b"INFO {}\r\n"


@pytest.mark.asyncio
async def test_websockets_transport_processes_completed_recv_before_send_backlog() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    transport._io_poll_s = 0.01
    ws = _FairWebsocketsWS(transport, replenish_limit=20)
    transport._ws = ws
    transport.write(b"PUB route.to_browser.sn_1--k 2\r\nok\r\n")

    data = await asyncio.wait_for(transport.readline(), timeout=1.0)

    assert data == b"INFO {}\r\n"
    assert ws.recv_calls == 2


@pytest.mark.asyncio
async def test_websockets_transport_sends_between_inbound_burst_frames() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    transport._io_poll_s = 0.01
    payload = b"PUB route.to_browser.sn_1--k 2\r\nok\r\n"
    ws = _InboundBurstWebsocketsWS(transport, payload)
    transport._ws = ws

    data = await asyncio.wait_for(transport.readline(), timeout=1.0)

    assert data == b"MSG route.to_browser 1 2\r\nok\r\n"
    await asyncio.wait_for(_wait_until(lambda: ws.sent == [payload]), timeout=1.0)
    assert ws.recv_calls >= 2

    if transport._io_task is not None:
        transport._io_task.cancel()
        await asyncio.gather(transport._io_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_websockets_transport_cancels_pending_recv_before_shared_io_send() -> None:
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = _FakeNC()
    transport._io_poll_s = 0.01
    ws = _BlockingWebsocketsWS()
    transport._ws = ws

    read_task = asyncio.create_task(transport.readline())
    await asyncio.wait_for(ws.recv_started.wait(), timeout=1.0)

    payload = b"SUB test 1\r\n"
    transport.write(payload)
    await asyncio.wait_for(transport.drain(), timeout=1.0)

    assert ws.recv_cancelled.is_set()
    assert ws.sent == [payload]

    ws.recv_release.set()
    data = await asyncio.wait_for(read_task, timeout=1.0)

    assert data == b"INFO {}\r\n"
    assert ws.recv_calls >= 2


@pytest.mark.asyncio
async def test_websockets_transport_dispatches_ping_to_nats_client_when_attached() -> None:
    nc = _FakeNC()
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = nc
    ws = _FakeWebsocketsWS([b"PING\r\n", b"INFO {}\r\n"])
    transport._ws = ws

    data = await transport.readline()

    assert nc.pings == 0
    assert ws.sent == [b"PONG\r\n"]
    assert data == b"INFO {}\r\n"


@pytest.mark.asyncio
async def test_websockets_transport_inline_pong_does_not_drain_normal_backlog() -> None:
    transport = WebSocketTransportWebsockets()
    ws = _FakeWebsocketsWS([])
    transport._ws = ws
    transport._io_task = asyncio.current_task()
    normal = b"PUB route.to_browser.sn_1--k 2\r\nok\r\n"
    transport.write(normal)

    await transport._send_nats_pong(reason="ping")

    assert ws.sent == [b"PONG\r\n"]
    assert transport._dequeue_pending_nowait() == ("bytes", normal)


@pytest.mark.asyncio
async def test_aiohttp_transport_connect_uses_manual_ws_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("aiohttp")

    monkeypatch.setenv("HUB_NATS_WS_HEARTBEAT_S", "20")
    transport = WebSocketTransportAiohttp()
    recorded: dict[str, object] = {}
    fake_ws = _FakeAiohttpPingWS()

    async def _fake_ws_connect(url: str, **kwargs):
        recorded["url"] = url
        recorded["kwargs"] = dict(kwargs)
        return fake_ws

    try:
        monkeypatch.setattr(transport._client, "ws_connect", _fake_ws_connect)
        await transport.connect_tls(
            "wss://example.invalid/nats",
            ssl_context=None,  # type: ignore[arg-type]
            buffer_size=0,
            connect_timeout=5,
        )

        kwargs = recorded["kwargs"]
        assert isinstance(kwargs, dict)
        assert "heartbeat" not in kwargs
        assert transport._adaos_ws_heartbeat_mode == "manual_no_timeout"
        assert transport._ws_heartbeat_task is not None
    finally:
        transport.close()
        await transport.wait_closed()


@pytest.mark.asyncio
async def test_aiohttp_transport_manual_ws_heartbeat_sends_ping() -> None:
    pytest.importorskip("aiohttp")

    transport = WebSocketTransportAiohttp()
    fake_ws = _FakeAiohttpPingWS()
    try:
        transport._ws = fake_ws
        transport._adaos_ws_heartbeat = 0.01
        transport._adaos_ws_heartbeat_mode = "manual_no_timeout"
        transport._after_connect()

        await asyncio.sleep(0.035)

        assert fake_ws.pings
        assert transport._adaos_ws_pings_tx >= 1
    finally:
        transport.close()
        await transport.wait_closed()


@pytest.mark.asyncio
async def test_websockets_transport_connect_uses_manual_ws_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("websockets")

    monkeypatch.setenv("HUB_NATS_WS_HEARTBEAT_S", "20")
    monkeypatch.setenv("HUB_NATS_WS_HEARTBEAT_FORCE", "1")
    transport = WebSocketTransportWebsockets()
    recorded: dict[str, object] = {}
    fake_ws = _FakeWebsocketsPingWS()

    async def _fake_connect(url: str, **kwargs):
        recorded["url"] = url
        recorded["kwargs"] = dict(kwargs)
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    try:
        await transport.connect_tls(
            "wss://example.invalid/nats",
            ssl_context=None,  # type: ignore[arg-type]
            buffer_size=0,
            connect_timeout=5,
        )

        kwargs = recorded["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["ping_interval"] is None
        assert kwargs["ping_timeout"] is None
        assert transport._adaos_ws_heartbeat_mode == "manual_no_timeout"
        assert transport._ws_heartbeat_task is not None
    finally:
        transport.close()
        await transport.wait_closed()


@pytest.mark.asyncio
async def test_websockets_transport_manual_ws_heartbeat_sends_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = WebSocketTransportWebsockets()
    fake_ws = _FakeWebsocketsPingWS()
    try:
        transport._ws = fake_ws
        transport._adaos_nc = _FakeNC()
        transport._adaos_ws_heartbeat = 0.01
        transport._adaos_ws_heartbeat_mode = "manual_no_timeout"
        transport._start_ws_heartbeat_task()

        await asyncio.sleep(0.05)

        assert fake_ws.pings
        assert transport._adaos_ws_pings_tx >= 1
    finally:
        transport.close()
        await transport.wait_closed()


def test_ws_data_heartbeat_defaults_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_DATA_HEARTBEAT_S", raising=False)

    assert _ws_data_heartbeat_s_from_env(ws_impl="other") == 15.0


def test_ws_data_heartbeat_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_DATA_HEARTBEAT_S", "0")

    assert _ws_data_heartbeat_s_from_env() is None


def test_ws_impl_auto_uses_websockets_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_IMPL", raising=False)
    monkeypatch.setattr(nats_ws_transport.os, "name", "nt")

    assert _ws_impl_from_env() == "websockets"


def test_ws_impl_auto_keeps_websockets_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_IMPL", "auto")
    monkeypatch.setattr(nats_ws_transport.os, "name", "posix")

    assert _ws_impl_from_env() == "websockets"


def test_ws_impl_explicit_websockets_overrides_windows_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_IMPL", "websockets")
    monkeypatch.setattr(nats_ws_transport.os, "name", "nt")

    assert _ws_impl_from_env() == "websockets"


def test_ws_data_ping_defaults_for_windows_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_DATA_PING_S", raising=False)
    monkeypatch.setattr(nats_ws_transport.os, "name", "nt")

    assert _ws_data_ping_s_from_env(ws_impl="websockets") == 5.0


def test_ws_data_ping_defaults_disabled_for_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_DATA_PING_S", raising=False)
    monkeypatch.setattr(nats_ws_transport.os, "name", "posix")

    assert _ws_data_ping_s_from_env(ws_impl="websockets") is None


def test_ws_data_ping_auto_uses_windows_websockets_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_DATA_PING_S", "auto")
    monkeypatch.setattr(nats_ws_transport.os, "name", "nt")

    assert _ws_data_ping_s_from_env(ws_impl="websockets") == 5.0


def test_ws_data_ping_can_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_DATA_PING_S", "7")

    assert _ws_data_ping_s_from_env(ws_impl="websockets") == 7.0


def test_ws_data_ping_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_DATA_PING_S", "0")

    assert _ws_data_ping_s_from_env(ws_impl="websockets") is None


def test_ws_control_intercept_defaults_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_CONTROL_INTERCEPT", raising=False)

    assert _ws_control_intercept_enabled_from_env() is True


def test_ws_control_intercept_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_CONTROL_INTERCEPT", "0")

    assert _ws_control_intercept_enabled_from_env() is False


def test_ws_proxy_defaults_to_system_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_PROXY", raising=False)
    monkeypatch.delenv("HUB_NATS_WS_PROXY_MODE", raising=False)

    assert _ws_proxy_from_env() is True


def test_ws_proxy_mode_preferred_over_legacy_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_PROXY", "none")
    monkeypatch.setenv("HUB_NATS_WS_PROXY_MODE", "auto")

    assert _ws_proxy_from_env() is True


def test_ws_proxy_can_force_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_WS_PROXY", raising=False)
    monkeypatch.setenv("HUB_NATS_WS_PROXY_MODE", "none")

    assert _ws_proxy_from_env() is None


def test_ws_proxy_legacy_env_is_hidden_during_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_WS_PROXY", "auto")

    with _without_adaos_legacy_ws_proxy_env():
        assert "HUB_NATS_WS_PROXY" not in os.environ

    assert os.environ["HUB_NATS_WS_PROXY"] == "auto"


@pytest.mark.asyncio
async def test_websockets_transport_data_heartbeat_sends_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("websockets")

    monkeypatch.delenv("HUB_NATS_WS_HEARTBEAT_S", raising=False)
    monkeypatch.setenv("HUB_NATS_WS_DATA_HEARTBEAT_S", "5")
    transport = WebSocketTransportWebsockets()
    fake_ws = _FakeWebsocketsWS([], block_when_empty=True)

    async def _fake_connect(url: str, **kwargs):
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    try:
        await transport.connect_tls(
            "wss://example.invalid/nats",
            ssl_context=None,  # type: ignore[arg-type]
            buffer_size=0,
            connect_timeout=5,
        )

        assert transport._data_heartbeat_task is not None
        await transport._send_nats_pong(reason="data_hb")
        assert b"PONG\r\n" in fake_ws.sent
    finally:
        transport.close()
        await transport.wait_closed()


@pytest.mark.asyncio
async def test_websockets_transport_data_ping_sends_ping() -> None:
    transport = WebSocketTransportWebsockets()
    fake_ws = _FakeWebsocketsWS([], block_when_empty=True)
    try:
        transport._ws = fake_ws
        transport._adaos_ws_data_ping = 0.01
        transport._adaos_last_rx_at = time.monotonic()
        transport._adaos_last_tx_at = time.monotonic()
        transport._start_data_ping_task()

        await asyncio.sleep(0.05)

        assert b"PING\r\n" in fake_ws.sent
        assert transport._adaos_data_pings_tx >= 1
    finally:
        transport.close()
        await transport.wait_closed()


@pytest.mark.asyncio
async def test_websockets_transport_data_ping_queues_without_full_drain() -> None:
    transport = WebSocketTransportWebsockets()
    io_task = asyncio.create_task(asyncio.sleep(60))
    try:
        transport._ws = _FakeWebsocketsWS([], block_when_empty=True)
        transport._io_task = io_task
        transport.write(b"PUB route.to_browser.sn_1--k 2\r\nok\r\n")

        await asyncio.wait_for(transport._send_nats_ping(reason="data_ping"), timeout=0.05)

        assert transport._pending_hi.get_nowait() == b"PING\r\n"
        assert transport._pending.qsize() == 1
        assert transport._adaos_data_pings_tx == 1
    finally:
        io_task.cancel()
        transport.close()
        await transport.wait_closed()


def test_extract_route_subjects_finds_multiple_msg_subjects() -> None:
    raw = (
        b"MSG route.to_hub.sn_1--http--aaa 1 3\r\n{}\r\n"
        b"MSG foo.bar 2 2\r\n{}\r\n"
        b"MSG route.to_hub.sn_1--bbb 3 2\r\n{}\r\n"
    )

    subjects = _extract_route_subjects(raw, prefix=b"route.to_hub.")

    assert subjects == [
        "route.to_hub.sn_1--http--aaa",
        "route.to_hub.sn_1--bbb",
    ]
