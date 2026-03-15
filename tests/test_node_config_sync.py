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
