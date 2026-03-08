import types

from adaos.apps.cli.commands.api import (
    _advertise_base,
    _find_matching_server_pids,
    _is_local_url,
    _process_matches_bind,
    _resolve_stop_bind,
    _resolve_bind,
    app,
)
from adaos.services.node_config import NodeConfig
from typer.testing import CliRunner


def test_advertise_base_uses_loopback_for_wildcard_bind():
    assert _advertise_base("0.0.0.0", 8779) == "http://127.0.0.1:8779"
    assert _advertise_base("::", 8779) == "http://127.0.0.1:8779"


def test_resolve_bind_prefers_saved_local_hub_port():
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="hub",
        hub_url="http://127.0.0.1:8779",
        token="t1",
    )
    assert _resolve_bind(conf, "127.0.0.1", 8777) == ("127.0.0.1", 8779)


def test_resolve_bind_ignores_remote_hub_url_for_local_bind():
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="hub",
        hub_url="https://api.inimatic.com/hubs/sn_1",
        token="t1",
    )
    assert _resolve_bind(conf, "127.0.0.1", 8777) == ("127.0.0.1", 8777)
    assert not _is_local_url(conf.hub_url)


def test_resolve_stop_bind_uses_local_hub_url():
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="hub",
        hub_url="http://127.0.0.1:8779",
        token="t1",
    )
    assert _resolve_stop_bind(conf) == ("127.0.0.1", 8779)


def test_resolve_stop_bind_rejects_remote_hub_url():
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="member",
        hub_url="https://api.inimatic.com/hubs/sn_1",
        token="t1",
    )
    assert _resolve_stop_bind(conf) is None


def test_process_matches_bind_with_split_flags():
    proc = types.SimpleNamespace(cmdline=lambda: ["python", "-m", "adaos", "api", "serve", "--host", "127.0.0.1", "--port", "8778"])
    assert _process_matches_bind(proc, "127.0.0.1", 8778)
    assert not _process_matches_bind(proc, "127.0.0.1", 8777)


def test_process_matches_bind_with_equals_flags():
    proc = types.SimpleNamespace(cmdline=lambda: ["python", "-m", "adaos", "api", "serve", "--host=localhost", "--port=8778"])
    assert _process_matches_bind(proc, "127.0.0.1", 8778)


def test_process_matches_bind_defaults_to_loopback_and_8777():
    proc = types.SimpleNamespace(cmdline=lambda: ["python", "-m", "adaos", "api", "serve"])
    assert _process_matches_bind(proc, "127.0.0.1", 8777)
    assert not _process_matches_bind(proc, "127.0.0.1", 8778)


def test_find_matching_server_pids_skips_protected_wrappers(monkeypatch):
    class FakeProc:
        def __init__(self, pid: int, cmdline: list[str]):
            self.info = {"pid": pid, "cmdline": cmdline}
            self._cmdline = cmdline

        def cmdline(self):
            return list(self._cmdline)

    procs = [
        FakeProc(100, ["D:\\git\\adaos\\.venv\\Scripts\\python.exe", "-m", "adaos", "api", "serve", "--host", "127.0.0.1", "--port", "8778"]),
        FakeProc(200, ["C:\\Python311\\python.exe", "-m", "adaos", "api", "serve", "--host", "127.0.0.1", "--port", "8778"]),
        FakeProc(300, ["python", "-m", "adaos", "api", "serve", "--host", "127.0.0.1", "--port", "8779"]),
    ]

    monkeypatch.setattr("adaos.apps.cli.commands.api.psutil.process_iter", lambda *_args, **_kwargs: procs)
    monkeypatch.setattr("adaos.apps.cli.commands.api.os.getpid", lambda: 999)

    assert _find_matching_server_pids("127.0.0.1", 8778, protected_pids={100}) == [200]


def test_api_stop_uses_hub_url_from_node_config(monkeypatch):
    runner = CliRunner()
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="hub",
        hub_url="http://127.0.0.1:8779",
        token="t1",
    )
    called: list[tuple[str, int]] = []

    monkeypatch.setattr("adaos.apps.cli.commands.api.load_config", lambda: conf)
    monkeypatch.setattr("adaos.apps.cli.commands.api._stop_previous_server", lambda host, port: called.append((host, port)))
    monkeypatch.setattr("adaos.apps.cli.commands.api._pidfile_path", lambda host, port: types.SimpleNamespace(exists=lambda: False))
    monkeypatch.setattr("adaos.apps.cli.commands.api._find_listening_server_pid", lambda host, port: None)
    monkeypatch.setattr("adaos.apps.cli.commands.api._find_matching_server_pids", lambda host, port, protected_pids=None: [])
    monkeypatch.setattr("adaos.apps.cli.commands.api._current_process_family_pids", lambda: set())

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0
    assert called == [("127.0.0.1", 8779)]
    assert "No AdaOS API server running at http://127.0.0.1:8779" in result.stdout


def test_api_stop_prefers_graceful_shutdown(monkeypatch):
    runner = CliRunner()
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="hub",
        hub_url="http://127.0.0.1:8779",
        token="t1",
    )
    forced: list[tuple[str, int]] = []

    monkeypatch.setattr("adaos.apps.cli.commands.api.load_config", lambda: conf)
    monkeypatch.setattr("adaos.apps.cli.commands.api._pidfile_path", lambda host, port: types.SimpleNamespace(exists=lambda: True))
    owner_state = {"calls": 0}

    def _owner_pid(host, port):
        owner_state["calls"] += 1
        return 1234 if owner_state["calls"] == 1 else None

    monkeypatch.setattr("adaos.apps.cli.commands.api._find_listening_server_pid", _owner_pid)
    monkeypatch.setattr("adaos.apps.cli.commands.api._find_matching_server_pids", lambda host, port, protected_pids=None: [])
    monkeypatch.setattr("adaos.apps.cli.commands.api._current_process_family_pids", lambda: set())
    monkeypatch.setattr("adaos.apps.cli.commands.api._request_graceful_shutdown", lambda host, port, token, reason='cli.stop': True)
    monkeypatch.setattr("adaos.apps.cli.commands.api._stop_previous_server", lambda host, port: forced.append((host, port)))

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0
    assert forced == []
    assert "Stopped AdaOS API gracefully at http://127.0.0.1:8779" in result.stdout


def test_api_stop_fails_for_non_local_hub_url(monkeypatch):
    runner = CliRunner()
    conf = NodeConfig(
        node_id="n1",
        subnet_id="sn_1",
        role="member",
        hub_url="https://api.inimatic.com/hubs/sn_1",
        token="t1",
    )

    monkeypatch.setattr("adaos.apps.cli.commands.api.load_config", lambda: conf)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 1
    assert "does not contain a local hub_url" in result.stdout
