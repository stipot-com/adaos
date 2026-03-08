from __future__ import annotations

import pytest
from nats.errors import ProtocolError
from nats.protocol.parser import Parser

from adaos.services.nats_ws_transport import (
    WebSocketTransportAiohttp,
    WebSocketTransportWebsockets,
    _extract_route_subjects,
)


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
    def __init__(self, frames: list[bytes | str]) -> None:
        self._frames = list(frames)
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.state = "OPEN"

    async def recv(self) -> bytes | str:
        if not self._frames:
            raise RuntimeError("no more frames")
        return self._frames.pop(0)

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
async def test_websockets_transport_consumes_standalone_ping_out_of_band() -> None:
    nc = _FakeNC()
    transport = WebSocketTransportWebsockets()
    transport._adaos_nc = nc
    transport._ws = _FakeWebsocketsWS([b"PING\r\n", b"INFO {}\r\n"])

    data = await transport.readline()

    assert nc.pings == 1
    assert nc.pongs == 0
    assert data == b"INFO {}\r\n"


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
