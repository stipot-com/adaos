from adaos.services.setup.presets import get_preset


def test_default_preset_includes_infrastate_skill() -> None:
    preset = get_preset("default")
    assert "infrastate_skill" in preset.skills
