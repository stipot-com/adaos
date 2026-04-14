from pathlib import Path

import yaml

from adaos.services.agent_context import get_ctx
from adaos.services.subnet_alias import display_subnet_alias, load_subnet_alias, save_subnet_alias


def test_display_subnet_alias_prefers_subnet_for_generic_hub_alias():
    assert display_subnet_alias("hub", "sn_123") == "sn_123"
    assert display_subnet_alias("hub-2", "sn_123") == "sn_123"


def test_display_subnet_alias_keeps_explicit_alias():
    assert display_subnet_alias("office", "sn_123") == "office"


def test_display_subnet_alias_falls_back_to_subnet():
    assert display_subnet_alias("", "sn_123") == "sn_123"


def test_subnet_alias_roundtrip_is_scoped_by_subnet():
    save_subnet_alias("office", subnet_id="sn_123")

    assert load_subnet_alias(subnet_id="sn_123") == "office"
    assert load_subnet_alias(subnet_id="sn_456") is None


def test_load_subnet_alias_migrates_legacy_node_yaml_alias() -> None:
    ctx = get_ctx()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"
    node_path.write_text(
        yaml.safe_dump(
            {
                "subnet_id": "sn_legacy",
                "subnet": {"id": "sn_legacy"},
                "nats": {"alias": "kitchen"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert load_subnet_alias(subnet_id="sn_legacy") == "kitchen"

    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert "alias" not in (saved.get("nats") or {})

