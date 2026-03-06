from adaos.apps.cli.commands.api import _advertise_base, _is_local_url, _resolve_bind
from adaos.services.node_config import NodeConfig


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
