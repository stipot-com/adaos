from adaos.services.subnet_alias import display_subnet_alias


def test_display_subnet_alias_prefers_subnet_for_generic_hub_alias():
    assert display_subnet_alias("hub", "sn_123") == "sn_123"
    assert display_subnet_alias("hub-2", "sn_123") == "sn_123"


def test_display_subnet_alias_keeps_explicit_alias():
    assert display_subnet_alias("office", "sn_123") == "office"


def test_display_subnet_alias_falls_back_to_subnet():
    assert display_subnet_alias("", "sn_123") == "sn_123"

