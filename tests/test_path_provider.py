"""Tests covering path resolution helpers exposed through the global context."""

from __future__ import annotations

from pathlib import Path

from adaos.adapters.fs.path_provider import PathProvider
from adaos.services.agent_context import get_ctx
from adaos.services.settings import Settings


def test_path_provider_locales_base_dir(tmp_path):
    settings = Settings.from_sources().with_overrides(base_dir=tmp_path / "adaos-test", profile="test")
    provider = PathProvider(settings)

    expected = (Path(settings.base_dir).expanduser().resolve() / "i18n")
    assert provider.locales_base_dir() == expected
    assert provider.skills_locales_dir() == expected
    assert provider.scenarios_locales_dir() == expected


def test_path_provider_workspace_layout(tmp_path):
    settings = Settings.from_sources().with_overrides(base_dir=tmp_path / "adaos-test", profile="test")
    provider = PathProvider(settings)

    base = Path(settings.base_dir).expanduser().resolve()
    assert provider.workspace_dir() == base / "workspace"
    assert provider.skills_workspace_dir() == base / "workspace" / "skills"
    assert provider.scenarios_workspace_dir() == base / "workspace" / "scenarios"
    assert provider.skills_cache_dir() == base / "skills"
    assert provider.scenarios_cache_dir() == base / "scenarios"
    # Compatibility aliases
    assert provider.skills_dir() == provider.skills_workspace_dir()
    assert provider.scenarios_dir() == provider.scenarios_workspace_dir()


def test_agent_context_exposes_locales_from_provider():
    ctx = get_ctx()

    base_locales = Path(ctx.paths.locales_base_dir())
    assert base_locales == Path(ctx.paths.skills_locales_dir())
    assert base_locales == Path(ctx.paths.scenarios_locales_dir())

    # ensure the directory lives under the base dir from the context
    assert base_locales.parent == Path(ctx.paths.base_dir())
