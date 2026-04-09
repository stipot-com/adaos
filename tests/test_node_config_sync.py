from __future__ import annotations

import copy
from pathlib import Path

import yaml

from adaos.services.agent_context import get_ctx
from adaos.services.node_config import NodeConfig, load_config, save_config


def _detached_config() -> NodeConfig:
    current = load_config()
    return NodeConfig(
        node_id=current.node_id,
        subnet_id=current.subnet_id,
        role=current.role,
        hub_url=current.hub_url,
        token=current.token,
        root_state=copy.deepcopy(current.root_state),
        root_settings=copy.deepcopy(current.root_settings),
        subnet_settings=copy.deepcopy(current.subnet_settings),
        node_settings=copy.deepcopy(current.node_settings),
        dev_settings=copy.deepcopy(current.dev_settings),
    )


def test_save_config_syncs_agent_context() -> None:
    ctx = get_ctx()
    detached = _detached_config()
    detached.subnet_id = "sn_saved_sync"
    detached.subnet_settings.id = detached.subnet_id

    save_config(detached)

    assert ctx.config.subnet_id == "sn_saved_sync"
    assert load_config().subnet_id == "sn_saved_sync"


def test_load_config_refreshes_agent_context() -> None:
    ctx = get_ctx()
    load_config()
    path = Path(ctx.paths.base_dir()) / "node.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["subnet_id"] = "sn_loaded_sync"
    data["role"] = "member"
    subnet = data.get("subnet") or {}
    subnet["id"] = "sn_loaded_sync"
    data["subnet"] = subnet
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    fresh = load_config()

    assert fresh.subnet_id == "sn_loaded_sync"
    assert ctx.config.subnet_id == "sn_loaded_sync"
    assert ctx.config.role == "member"


def test_load_config_reuses_cached_node_yaml_without_reparsing(monkeypatch) -> None:
    import adaos.services.node_config as mod

    ctx = get_ctx()
    baseline = mod.load_config()
    path = Path(ctx.paths.base_dir()) / "node.yaml"
    original = path.read_text(encoding="utf-8")
    mod._NODE_CONFIG_CACHE.clear()

    calls = {"count": 0}
    original_safe_load = mod.yaml.safe_load

    def _counting_safe_load(*args, **kwargs):
        calls["count"] += 1
        return original_safe_load(*args, **kwargs)

    monkeypatch.setattr(mod.yaml, "safe_load", _counting_safe_load)

    first = mod.load_config()
    second = mod.load_config()

    assert first.subnet_id == baseline.subnet_id
    assert first.subnet_id == second.subnet_id
    assert calls["count"] == 1
    path.write_text(original, encoding="utf-8")


def test_save_config_stores_managed_key_paths_relative_to_base() -> None:
    ctx = get_ctx()
    detached = _detached_config()
    base_dir = Path(ctx.paths.base_dir())
    keys_dir = base_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)

    detached.root_settings.ca_cert = str(keys_dir / "ca.cert")
    detached.subnet_settings.hub.key = str(keys_dir / "hub_private.pem")
    detached.subnet_settings.hub.cert = str(keys_dir / "hub_cert.pem")

    save_config(detached)

    data = yaml.safe_load((base_dir / "node.yaml").read_text(encoding="utf-8")) or {}
    root = data.get("root") or {}
    subnet = data.get("subnet") or {}
    hub = subnet.get("hub") or {}

    assert root.get("ca_cert") == "keys/ca.cert"
    assert hub.get("key") == "keys/hub_private.pem"
    assert hub.get("cert") == "keys/hub_cert.pem"


def test_load_config_migrates_legacy_key_paths_into_active_base_dir(tmp_path: Path) -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    legacy_root = tmp_path / "legacy-checkout" / "keys"
    legacy_root.mkdir(parents=True, exist_ok=True)
    legacy_key = legacy_root / "hub_private.pem"
    legacy_cert = legacy_root / "hub_cert.pem"
    legacy_ca = legacy_root / "ca.cert"
    legacy_key.write_text("legacy-key", encoding="utf-8")
    legacy_cert.write_text("legacy-cert", encoding="utf-8")
    legacy_ca.write_text("legacy-ca", encoding="utf-8")

    node_path = base_dir / "node.yaml"
    node_path.write_text(
        yaml.safe_dump(
            {
                "node_id": "node_legacy",
                "subnet_id": "sn_legacy",
                "role": "hub",
                "root": {"ca_cert": str(legacy_ca)},
                "subnet": {
                    "id": "sn_legacy",
                    "hub": {
                        "key": str(legacy_key),
                        "cert": str(legacy_cert),
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    fresh = load_config()
    keys_dir = base_dir / "keys"

    assert fresh.ca_cert_path() == (keys_dir / "ca.cert")
    assert fresh.hub_key_path() == (keys_dir / "hub_private.pem")
    assert fresh.hub_cert_path() == (keys_dir / "hub_cert.pem")
    assert (keys_dir / "ca.cert").read_text(encoding="utf-8") == "legacy-ca"
    assert (keys_dir / "hub_private.pem").read_text(encoding="utf-8") == "legacy-key"
    assert (keys_dir / "hub_cert.pem").read_text(encoding="utf-8") == "legacy-cert"

    migrated = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert ((migrated.get("root") or {}).get("ca_cert")) == "keys/ca.cert"
    assert (((migrated.get("subnet") or {}).get("hub") or {}).get("key")) == "keys/hub_private.pem"
    assert (((migrated.get("subnet") or {}).get("hub") or {}).get("cert")) == "keys/hub_cert.pem"
