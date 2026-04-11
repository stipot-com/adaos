from __future__ import annotations

from types import SimpleNamespace

import pytest

from adaos.apps.cli.commands.hub import _resolve_root_base_url


def test_resolve_root_base_url_prefers_ru_public_root_for_default_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_ZONE_ID", "ru")
    conf = SimpleNamespace(
        zone_id=None,
        root_settings=SimpleNamespace(base_url="https://api.inimatic.com"),
    )

    assert _resolve_root_base_url(conf) == "https://ru.api.inimatic.com"


def test_resolve_root_base_url_keeps_explicit_non_default_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_ZONE_ID", "ru")
    conf = SimpleNamespace(
        zone_id=None,
        root_settings=SimpleNamespace(base_url="https://custom-root.example"),
    )

    assert _resolve_root_base_url(conf) == "https://custom-root.example"


def test_resolve_root_base_url_does_not_normalize_non_two_letter_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAOS_ZONE_ID", "russia")
    conf = SimpleNamespace(
        zone_id=None,
        root_settings=SimpleNamespace(base_url="https://api.inimatic.com"),
    )

    assert _resolve_root_base_url(conf) == "https://api.inimatic.com"
