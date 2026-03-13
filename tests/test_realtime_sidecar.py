from __future__ import annotations

import asyncio
import socket

import pytest

from adaos.apps.cli.commands import realtime as realtime_cmd
from adaos.services import realtime_sidecar as realtime_sidecar_mod
from adaos.services.realtime_sidecar import (
    RealtimeSidecarServer,
    realtime_sidecar_enabled,
    realtime_sidecar_local_url,
)


class _FakeRemoteWS:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.recv_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.transport = None

    async def recv(self) -> bytes:
        return await self.recv_queue.get()

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    async def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeSocket:
    def __init__(self) -> None:
        self.sockopts: list[tuple[int, int, int]] = []
        self.keepalive_vals = None

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        self.sockopts.append((level, optname, value))

    def ioctl(self, code, value) -> None:
        self.keepalive_vals = (code, value)


class _FakeTransport:
    def __init__(self, sock: _FakeSocket) -> None:
        self._sock = sock

    def get_extra_info(self, name: str):
        if name == "socket":
            return self._sock
        return None


def test_realtime_sidecar_enabled_defaults_to_windows_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_ENABLE", raising=False)
    monkeypatch.delenv("HUB_REALTIME_ENABLE", raising=False)

    assert realtime_sidecar_enabled(role="hub", os_name="nt") is True
    assert realtime_sidecar_enabled(role="hub", os_name="posix") is False
    assert realtime_sidecar_enabled(role="root", os_name="nt") is False


def test_realtime_sidecar_local_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_HOST", "127.0.0.7")
    monkeypatch.setenv("ADAOS_REALTIME_PORT", "9234")

    assert realtime_sidecar_local_url() == "nats://127.0.0.7:9234"


@pytest.mark.asyncio
async def test_realtime_sidecar_relays_bytes_between_local_nats_and_remote_ws(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_ws = _FakeRemoteWS()

    async def _fake_connect(*args, **kwargs):
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection(server.listen_host, server.listen_port)
        writer.write(b"PING\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)

        assert fake_ws.sent == [b"PING\r\n"]

        await fake_ws.recv_queue.put(b"INFO {}\r\n")
        data = await asyncio.wait_for(reader.readexactly(len(b"INFO {}\r\n")), timeout=1.0)

        assert data == b"INFO {}\r\n"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_realtime_sidecar_remote_connect_uses_ws_ping_and_tcp_keepalive(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    recorded: dict[str, object] = {}
    fake_ws = _FakeRemoteWS()
    fake_sock = _FakeSocket()
    fake_ws.transport = _FakeTransport(fake_sock)

    async def _fake_connect(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = dict(kwargs)
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")
    monkeypatch.setenv("ADAOS_REALTIME_WS_HEARTBEAT_S", "20")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    ws, target = await server._connect_remote(session_id="rt-test")
    try:
        assert ws is fake_ws
        assert target.startswith("wss://example.invalid/nats")
        kwargs = dict(recorded["kwargs"])
        assert kwargs["ping_interval"] == 20.0
        assert kwargs["ping_timeout"] is None
        assert kwargs["subprotocols"] == ["nats"]
        assert kwargs["compression"] is None
        assert any(opt[1] == socket.SO_KEEPALIVE for opt in fake_sock.sockopts)
    finally:
        await ws.close()


def test_realtime_cli_applies_loop_policy_before_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(realtime_cmd, "apply_realtime_loop_policy", lambda: calls.append("policy"))

    def _fake_run(coro):
        calls.append("run")
        try:
            coro.close()
        except Exception:
            pass
        return 0

    monkeypatch.setattr(realtime_cmd.asyncio, "run", _fake_run)

    with pytest.raises(SystemExit) as exc:
        realtime_cmd.serve(host="127.0.0.1", port=7422)

    assert exc.value.code == 0
    assert calls == ["policy", "run"]


@pytest.mark.asyncio
async def test_realtime_sidecar_sends_own_nats_keepalive_and_swallows_pong(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_ws = _FakeRemoteWS()

    async def _fake_connect(*args, **kwargs):
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setattr(realtime_sidecar_mod, "_realtime_nats_ping_interval_s", lambda: 0.05)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection(server.listen_host, server.listen_port)
        for _ in range(20):
            if fake_ws.sent:
                break
            await asyncio.sleep(0.01)

        assert fake_ws.sent
        assert fake_ws.sent[0] == b"PING\r\n"

        await fake_ws.recv_queue.put(b"PONG\r\n")
        await asyncio.sleep(0.01)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), timeout=0.05)

        assert server._stats.sidecar_nats_pings_tx >= 1
        assert server._stats.sidecar_nats_pongs_rx == 1
        assert server._stats.sidecar_nats_pings_outstanding in {0, 1}
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_realtime_sidecar_forwards_pong_to_local_client_when_client_ping_outstanding(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_ws = _FakeRemoteWS()

    async def _fake_connect(*args, **kwargs):
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setattr(realtime_sidecar_mod, "_realtime_nats_ping_interval_s", lambda: None)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection(server.listen_host, server.listen_port)
        server._stats.sidecar_nats_pings_outstanding = 1

        writer.write(b"PING\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)

        assert fake_ws.sent == [b"PING\r\n"]

        await fake_ws.recv_queue.put(b"PONG\r\n")
        data = await asyncio.wait_for(reader.readexactly(len(b"PONG\r\n")), timeout=1.0)

        assert data == b"PONG\r\n"
        assert server._stats.local_nats_pings_tx == 1
        assert server._stats.client_nats_pings_outstanding == 0
        assert server._stats.sidecar_nats_pings_outstanding == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
