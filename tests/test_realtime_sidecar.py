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


class _FakeAuthRemoteWS(_FakeRemoteWS):
    def __init__(self) -> None:
        super().__init__()
        self.recv_queue.put_nowait(
            b'INFO {"server_id":"test","version":"2.10.29","proto":1,"auth_required":true,"max_payload":1048576}\r\n'
        )

    async def send(self, payload: bytes) -> None:
        await super().send(payload)
        if bytes(payload).startswith(b"CONNECT "):
            await self.recv_queue.put(b"-ERR 'Authorization Violation'\r\n")


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


def test_realtime_sidecar_enabled_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_ENABLE", raising=False)
    monkeypatch.delenv("HUB_REALTIME_ENABLE", raising=False)

    assert realtime_sidecar_enabled(role="hub", os_name="nt") is False
    assert realtime_sidecar_enabled(role="hub", os_name="posix") is False
    assert realtime_sidecar_enabled(role="root", os_name="nt") is False


def test_realtime_sidecar_enabled_respects_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")

    assert realtime_sidecar_enabled(role="hub", os_name="nt") is True


def test_realtime_sidecar_local_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_HOST", "127.0.0.7")
    monkeypatch.setenv("ADAOS_REALTIME_PORT", "9234")

    assert realtime_sidecar_local_url() == "nats://127.0.0.7:9234"


def test_realtime_sidecar_loop_defaults_to_proactor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_WIN_LOOP", raising=False)

    assert realtime_sidecar_mod._sidecar_loop_mode() == "proactor"


def test_realtime_sidecar_ws_heartbeat_defaults_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_WS_HEARTBEAT_S", raising=False)

    assert realtime_sidecar_mod._realtime_ws_heartbeat_s() is None


@pytest.mark.asyncio
async def test_probe_realtime_sidecar_ready_accepts_nats_info() -> None:
    async def _handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'INFO {"server_id":"test"}\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    try:
        sock = server.sockets[0].getsockname()
        assert await realtime_sidecar_mod.probe_realtime_sidecar_ready(host=sock[0], port=sock[1], timeout_s=1.0)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_realtime_sidecar_ready_rejects_empty_listener() -> None:
    async def _handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    try:
        sock = server.sockets[0].getsockname()
        assert not await realtime_sidecar_mod.probe_realtime_sidecar_ready(host=sock[0], port=sock[1], timeout_s=1.0)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_realtime_sidecar_probe_does_not_break_immediate_nats_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import nats
    import websockets  # type: ignore

    async def _fake_connect(*args, **kwargs):
        return _FakeAuthRemoteWS()

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    await server.start()
    try:
        assert await realtime_sidecar_mod.probe_realtime_sidecar_ready(
            host=server.listen_host,
            port=server.listen_port,
            timeout_s=1.0,
        )

        nc = nats.aio.client.Client()
        try:
            with pytest.raises(nats.errors.Error, match="Authorization Violation"):
                await asyncio.wait_for(
                    nc.connect(
                        servers=[f"nats://{server.listen_host}:{server.listen_port}"],
                        user="hub_test",
                        password="bad",
                        allow_reconnect=False,
                        connect_timeout=1.0,
                        ping_interval=3600,
                        max_outstanding_pings=10,
                    ),
                    timeout=2.0,
                )
        finally:
            await nc.close()
    finally:
        await server.close()


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


@pytest.mark.asyncio
async def test_realtime_sidecar_remote_connect_does_not_inherit_global_ws_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    recorded: dict[str, object] = {}
    fake_ws = _FakeRemoteWS()

    async def _fake_connect(*args, **kwargs):
        recorded["kwargs"] = dict(kwargs)
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")
    monkeypatch.setenv("HUB_NATS_WS_HEARTBEAT_S", "37")
    monkeypatch.delenv("ADAOS_REALTIME_WS_HEARTBEAT_S", raising=False)

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    ws, _target = await server._connect_remote(session_id="rt-test")
    try:
        kwargs = dict(recorded["kwargs"])
        assert kwargs["ping_interval"] is None
        assert kwargs["ping_timeout"] is None
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_realtime_sidecar_remote_connect_allows_disabling_sidecar_ws_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    recorded: dict[str, object] = {}
    fake_ws = _FakeRemoteWS()

    async def _fake_connect(*args, **kwargs):
        recorded["kwargs"] = dict(kwargs)
        return fake_ws

    import websockets  # type: ignore

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("ADAOS_REALTIME_DIAG_FILE", str(tmp_path / "diag.jsonl"))
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://example.invalid/nats")
    monkeypatch.setenv("ADAOS_REALTIME_WS_HEARTBEAT_S", "0")

    server = RealtimeSidecarServer(host="127.0.0.1", port=0)
    ws, _target = await server._connect_remote(session_id="rt-test")
    try:
        kwargs = dict(recorded["kwargs"])
        assert kwargs["ping_interval"] is None
        assert kwargs["ping_timeout"] is None
    finally:
        await ws.close()


def test_realtime_sidecar_prefers_api_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_PREFER_DEDICATED", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_API_FALLBACK", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"]


def test_realtime_sidecar_does_not_inherit_hub_prefer_dedicated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_PREFER_DEDICATED", "0")
    monkeypatch.delenv("ADAOS_REALTIME_PREFER_DEDICATED", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_API_FALLBACK", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"]


def test_realtime_sidecar_can_disable_api_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_NATS_PREFER_DEDICATED", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_PREFER_DEDICATED", raising=False)
    monkeypatch.setenv("ADAOS_REALTIME_ALLOW_API_FALLBACK", "0")
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://nats.inimatic.com/nats"]


def test_realtime_sidecar_can_explicitly_prefer_dedicated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_NATS_PREFER_DEDICATED", "0")
    monkeypatch.setenv("ADAOS_REALTIME_PREFER_DEDICATED", "1")
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_API_FALLBACK", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://nats.inimatic.com/nats", "wss://api.inimatic.com/nats"]


def test_realtime_sidecar_uses_ws_fallback_for_direct_tcp_node_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK", raising=False)
    monkeypatch.setattr(
        realtime_sidecar_mod,
        "_load_node_yaml",
        lambda: {"nats": {"ws_url": "nats://nats.inimatic.com:4222"}},
    )

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://api.inimatic.com/nats", "wss://nats.inimatic.com/nats"]


def test_realtime_sidecar_can_append_tcp_fallback_for_direct_tcp_node_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_URL", raising=False)
    monkeypatch.setenv("ADAOS_REALTIME_ALLOW_TCP_FALLBACK", "1")
    monkeypatch.setattr(
        realtime_sidecar_mod,
        "_load_node_yaml",
        lambda: {"nats": {"ws_url": "nats://nats.inimatic.com:4222"}},
    )

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == [
        "wss://api.inimatic.com/nats",
        "wss://nats.inimatic.com/nats",
        "nats://nats.inimatic.com:4222",
    ]


def test_realtime_sidecar_respects_explicit_public_ws_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_REALTIME_REMOTE_WS_URL", "wss://api.inimatic.com/nats")
    monkeypatch.delenv("ADAOS_REALTIME_REMOTE_WS_ALT", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_ALLOW_API_FALLBACK", raising=False)

    ordered = realtime_sidecar_mod.resolve_realtime_remote_candidates()

    assert ordered == ["wss://api.inimatic.com/nats"]


@pytest.mark.asyncio
async def test_realtime_sidecar_subprocess_forces_dedicated_direct_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    popen_env: dict[str, str] = {}

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

    async def _fake_is_port_open(_host: str, _port: int) -> bool:
        return False

    async def _fake_wait_ready(*, host: str, port: int, timeout_s: float = 10.0) -> bool:
        return True

    def _fake_popen(*args, **kwargs):
        nonlocal popen_env
        popen_env = dict(kwargs["env"])
        return _FakeProc()

    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setattr(realtime_sidecar_mod, "_is_port_open", _fake_is_port_open)
    monkeypatch.setattr(realtime_sidecar_mod, "wait_realtime_sidecar_ready", _fake_wait_ready)
    monkeypatch.setattr(realtime_sidecar_mod.subprocess, "Popen", _fake_popen)

    proc = await realtime_sidecar_mod.start_realtime_sidecar_subprocess(role="hub")

    assert proc is not None
    assert popen_env["ADAOS_REALTIME_PREFER_DEDICATED"] == "0"
    assert popen_env["ADAOS_REALTIME_ALLOW_API_FALLBACK"] == "1"
    assert popen_env["ADAOS_REALTIME_WIN_LOOP"] == "proactor"


@pytest.mark.asyncio
async def test_realtime_sidecar_subprocess_starts_for_direct_tcp_node_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    popen_env: dict[str, str] = {}

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

    async def _fake_is_port_open(_host: str, _port: int) -> bool:
        return False

    async def _fake_wait_ready(*, host: str, port: int, timeout_s: float = 10.0) -> bool:
        return True

    def _fake_popen(*args, **kwargs):
        nonlocal popen_env
        popen_env = dict(kwargs["env"])
        return _FakeProc()

    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setattr(
        realtime_sidecar_mod,
        "_load_node_yaml",
        lambda: {"nats": {"ws_url": "nats://nats.inimatic.com:4222"}},
    )
    monkeypatch.setattr(realtime_sidecar_mod, "_is_port_open", _fake_is_port_open)
    monkeypatch.setattr(realtime_sidecar_mod, "wait_realtime_sidecar_ready", _fake_wait_ready)
    monkeypatch.setattr(realtime_sidecar_mod.subprocess, "Popen", _fake_popen)

    proc = await realtime_sidecar_mod.start_realtime_sidecar_subprocess(role="hub")

    assert proc is not None
    assert popen_env["ADAOS_REALTIME_PREFER_DEDICATED"] == "0"
    assert popen_env["ADAOS_REALTIME_ALLOW_API_FALLBACK"] == "1"
    assert popen_env["ADAOS_REALTIME_WIN_LOOP"] == "proactor"


@pytest.mark.asyncio
async def test_realtime_sidecar_subprocess_replaces_stale_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    popen_env: dict[str, str] = {}
    replace_calls: list[tuple[str, int]] = []

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

    async def _fake_is_port_open(_host: str, _port: int) -> bool:
        return not replace_calls

    async def _fake_wait_ready(*, host: str, port: int, timeout_s: float = 10.0) -> bool:
        return True

    def _fake_popen(*args, **kwargs):
        nonlocal popen_env
        popen_env = dict(kwargs["env"])
        return _FakeProc()

    def _fake_replace_existing_realtime_listener(host: str, port: int) -> bool:
        replace_calls.append((host, port))
        return True

    monkeypatch.setenv("ADAOS_REALTIME_ENABLE", "1")
    monkeypatch.setenv("ADAOS_REALTIME_LOG", str(tmp_path / "sidecar.log"))
    monkeypatch.setattr(realtime_sidecar_mod, "_is_port_open", _fake_is_port_open)
    monkeypatch.setattr(
        realtime_sidecar_mod,
        "_replace_existing_realtime_listener",
        _fake_replace_existing_realtime_listener,
    )
    monkeypatch.setattr(realtime_sidecar_mod, "wait_realtime_sidecar_ready", _fake_wait_ready)
    monkeypatch.setattr(realtime_sidecar_mod.subprocess, "Popen", _fake_popen)

    proc = await realtime_sidecar_mod.start_realtime_sidecar_subprocess(role="hub")

    assert proc is not None
    assert replace_calls == [("127.0.0.1", 7422)]
    assert popen_env["ADAOS_REALTIME_WIN_LOOP"] == "proactor"


def test_realtime_sidecar_nats_keepalive_defaults_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAOS_REALTIME_NATS_PING_S", raising=False)
    monkeypatch.delenv("ADAOS_REALTIME_UPSTREAM_NATS_PING_S", raising=False)

    assert realtime_sidecar_mod._realtime_nats_ping_interval_s() == 15.0


def test_realtime_sidecar_filters_quarantined_remote_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    dedicated = "wss://nats.inimatic.com/nats"
    api = "wss://api.inimatic.com/nats"
    quarantine = {
        realtime_sidecar_mod._realtime_remote_quarantine_key(dedicated): realtime_sidecar_mod.time.monotonic() + 60.0
    }
    monkeypatch.setattr(realtime_sidecar_mod, "_realtime_remote_quarantine_until", quarantine)
    monkeypatch.setattr(realtime_sidecar_mod, "resolve_realtime_remote_candidates", lambda: [dedicated, api])

    assert realtime_sidecar_mod._available_realtime_remote_candidates() == [api]


def test_realtime_sidecar_orders_all_quarantined_candidates_by_oldest_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dedicated = "wss://nats.inimatic.com/nats"
    api = "wss://api.inimatic.com/nats"
    now_m = realtime_sidecar_mod.time.monotonic()
    quarantine = {
        realtime_sidecar_mod._realtime_remote_quarantine_key(dedicated): now_m + 30.0,
        realtime_sidecar_mod._realtime_remote_quarantine_key(api): now_m + 60.0,
    }
    monkeypatch.setattr(realtime_sidecar_mod, "_realtime_remote_quarantine_until", quarantine)
    monkeypatch.setattr(realtime_sidecar_mod, "resolve_realtime_remote_candidates", lambda: [api, dedicated])

    assert realtime_sidecar_mod._available_realtime_remote_candidates() == [dedicated, api]


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
async def test_realtime_sidecar_matches_pongs_to_sidecar_and_client_pings_in_order(
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
        await asyncio.sleep(0.05)
        server._stats.sidecar_nats_pings_outstanding = 1
        server._pending_ping_sources.append("sidecar")

        writer.write(b"PING\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)

        assert fake_ws.sent == [b"PING\r\n"]

        await fake_ws.recv_queue.put(b"PONG\r\n")
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), timeout=0.05)

        assert server._stats.sidecar_nats_pings_outstanding == 0
        assert server._stats.client_nats_pings_outstanding == 1

        await fake_ws.recv_queue.put(b"PONG\r\n")
        data = await asyncio.wait_for(reader.readexactly(len(b"PONG\r\n")), timeout=1.0)

        assert data == b"PONG\r\n"
        assert server._stats.local_nats_pings_tx == 1
        assert server._stats.client_nats_pings_outstanding == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
