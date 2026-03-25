from adaos.services.semver import bump_version


def test_bump_version_minor_resets_patch():
    assert bump_version("1.2.3", 1) == "1.3.0"


def test_bump_version_patch_increments_patch():
    assert bump_version("1.2.3", 2) == "1.2.4"


def test_bump_version_handles_prefix_and_missing_parts():
    assert bump_version("v1.2", 1) == "1.3.0"


def test_bump_version_defaults_from_none():
    assert bump_version(None, 1) == "0.1.0"


def test_bump_version_clamps_index():
    assert bump_version("1.2.3", -10) == "2.0.0"
    assert bump_version("1.2.3", 99) == "1.2.4"

