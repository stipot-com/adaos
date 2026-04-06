from __future__ import annotations

from adaos.services.zone_hosts import (
    canonical_zone_id,
    resolve_zone_for_server,
    zone_public_base_url,
    zone_public_host,
)


def test_canonical_zone_id_supports_legacy_aliases() -> None:
    assert canonical_zone_id("api") == "us"
    assert canonical_zone_id("de") == "eu"
    assert canonical_zone_id("cn") == "ch"
    assert canonical_zone_id("russia") == "ru"


def test_zone_public_host_uses_ru_host_only_for_ru_zone() -> None:
    assert zone_public_host("ru") == "ru.inimatic.com"
    assert zone_public_host("api") == "api.inimatic.com"
    assert zone_public_host("eu") == "api.inimatic.com"


def test_zone_public_base_url_maps_logical_zones_to_shared_hosts() -> None:
    assert zone_public_base_url("ru") == "https://ru.inimatic.com"
    assert zone_public_base_url("us") == "https://api.inimatic.com"
    assert zone_public_base_url("in") == "https://api.inimatic.com"


def test_resolve_zone_for_server_keeps_supported_requested_zone() -> None:
    assert resolve_zone_for_server("eu", "api") == "eu"
    assert resolve_zone_for_server("api", "api") == "us"
    assert resolve_zone_for_server("cn", "api") == "ch"


def test_resolve_zone_for_server_falls_back_to_server_family() -> None:
    assert resolve_zone_for_server("ru", "api") == "us"
    assert resolve_zone_for_server("eu", "ru") == "ru"
