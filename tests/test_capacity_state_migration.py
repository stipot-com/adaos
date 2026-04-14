from __future__ import annotations

from pathlib import Path

import yaml

from adaos.services import capacity as capacity_mod
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.registry import subnet_directory as subnet_directory_mod


def test_local_capacity_seeds_registry_and_prunes_legacy_node_yaml() -> None:
    ctx = get_ctx()
    cfg = load_config()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"
    payload = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    payload["capacity"] = {
        "io": [{"io_type": "git", "capabilities": ["git"], "priority": 45}],
        "skills": [{"name": "watchdog_skill", "version": "1.2.3", "active": True, "dev": False}],
        "scenarios": [{"name": "ops", "version": "0.3.0", "active": True, "dev": False}],
    }
    node_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    capacity_mod._CAPACITY_CACHE.clear()
    subnet_directory_mod._DIR = None

    snapshot = capacity_mod.get_local_capacity()
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    repo = subnet_directory_mod.get_directory().repo

    assert any(item.get("io_type") == "git" for item in snapshot["io"])
    assert any(item.get("name") == "watchdog_skill" for item in snapshot["skills"])
    assert any(item.get("name") == "ops" for item in snapshot["scenarios"])
    assert "capacity" not in saved
    assert any(item.get("io_type") == "git" for item in repo.io_for_node(cfg.node_id))
    assert any(item.get("name") == "watchdog_skill" for item in repo.skills_for_node(cfg.node_id))
    assert any(item.get("name") == "ops" for item in repo.scenarios_for_node(cfg.node_id))


def test_capacity_updates_registry_without_restoring_node_yaml_capacity() -> None:
    ctx = get_ctx()
    cfg = load_config()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"

    capacity_mod._CAPACITY_CACHE.clear()
    subnet_directory_mod._DIR = None

    capacity_mod.install_skill_in_capacity("new_skill", "2.0.0", active=True, dev=False)
    capacity_mod.install_scenario_in_capacity("desk", "1.0.0", active=True, dev=False)
    capacity_mod.install_io_in_capacity("voice", ["audio", "stt:vosk"], priority=30)

    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    repo = subnet_directory_mod.get_directory().repo

    assert "capacity" not in saved
    assert any(item.get("name") == "new_skill" for item in repo.skills_for_node(cfg.node_id))
    assert any(item.get("name") == "desk" for item in repo.scenarios_for_node(cfg.node_id))
    assert any(item.get("io_type") == "voice" for item in repo.io_for_node(cfg.node_id))
