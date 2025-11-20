"""Tests for the reusable skill runtime helpers."""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from adaos.services.agent_context import get_ctx
from adaos.services.skill.runtime import (
    SkillDirectoryNotFoundError,
    SkillPrepScriptNotFoundError,
    find_skill_dir,
    find_skill_slot,
    resolve_active_version,
    run_skill_handler_sync,
    run_skill_prep,
)
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


@pytest.fixture
def skill_factory() -> Callable[[str], tuple[SkillRuntimeEnvironment, str]]:
    """Prepare a namespaced runtime layout for skills used in tests."""

    default_handler = textwrap.dedent(
        """
        def handle(topic, payload):
            return {"topic": topic, "payload": payload}
        """
    )

    default_prep = textwrap.dedent(
        """
        from pathlib import Path

        def run_prep(skill_path: Path):
            artifact = skill_path / "prep" / "artifact.txt"
            artifact.write_text("done", encoding="utf-8")
            return {"status": "ok", "artifact": str(artifact)}
        """
    )

    def _create_skill(
        name: str,
        *,
        version: str = "1.0.0",
        slots: tuple[str, ...] = ("A",),
        handler_source: str | dict[str, str] | None = None,
        prep_source: str | None = default_prep,
        active_slot: str | None = None,
    ) -> tuple[SkillRuntimeEnvironment, str]:
        ctx = get_ctx()
        skills_root = Path(ctx.paths.skills_dir())
        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        selected = active_slot or slots[0]
        env.prepare_version(version, activate_slot=selected)

        if isinstance(handler_source, dict):
            slot_sources: dict[str, str] = {}
            for slot in slots:
                code = handler_source.get(slot)
                if code is None:
                    code = handler_source.get("default")
                slot_sources[slot] = textwrap.dedent(code or default_handler)
        else:
            code = textwrap.dedent(handler_source or default_handler)
            slot_sources = {slot: code for slot in slots}

        for slot in slots:
            slot_paths = env.build_slot_paths(version, slot)
            namespace_root = slot_paths.src_dir / "skills"
            target = namespace_root / name
            if slot_paths.src_dir.exists():
                shutil.rmtree(slot_paths.src_dir)
            namespace_root.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            (target / "__init__.py").write_text("", encoding="utf-8")
            handlers_dir = target / "handlers"
            handlers_dir.mkdir(parents=True, exist_ok=True)
            handlers_dir.joinpath("__init__.py").write_text(
                "from .main import handle  # noqa: F401\n", encoding="utf-8"
            )
            handlers_dir.joinpath("main.py").write_text(slot_sources[slot], encoding="utf-8")
            if prep_source is not None:
                prep_dir = target / "prep"
                prep_dir.mkdir(parents=True, exist_ok=True)
                prep_dir.joinpath("prepare.py").write_text(
                    textwrap.dedent(prep_source),
                    encoding="utf-8",
                )
        env.active_version_marker().write_text(version, encoding="utf-8")
        env.set_active_slot(version, selected)
        return env, version

    return _create_skill


def test_find_skill_dir_returns_package_path(skill_factory):
    env, version = skill_factory("demo_skill")
    active_slot = env.read_active_slot(version)
    expected = env.build_slot_paths(version, active_slot).src_dir / "skills" / "demo_skill"
    assert find_skill_dir("demo_skill").resolve() == expected.resolve()


def test_find_skill_slot_points_to_current_symlink(skill_factory):
    env, version = skill_factory("symlinked_skill", slots=("A", "B"), active_slot="B")
    slot_path = find_skill_slot("symlinked_skill")
    assert slot_path.name == "current"
    assert slot_path.resolve() == env.build_slot_paths(version, "B").root.resolve()


def test_find_skill_dir_missing_raises():
    with pytest.raises(SkillDirectoryNotFoundError):
        find_skill_dir("does_not_exist")


def test_resolve_active_version_returns_marker_value(skill_factory):
    _, version = skill_factory("versioned_skill", version="2.5.1")
    assert resolve_active_version("versioned_skill") == "2.5.1"


def test_run_skill_handler_sync_handles_coroutines(skill_factory):
    handler_source = textwrap.dedent(
        """
        import asyncio

        async def handle(topic, payload):
            await asyncio.sleep(0)
            return {"topic": topic, "payload": payload, "status": "ok"}
        """
    )
    skill_factory("async_skill", handler_source=handler_source, prep_source=None)

    result = run_skill_handler_sync("async_skill", "demo.topic", {"foo": "bar"})
    assert result == {"topic": "demo.topic", "payload": {"foo": "bar"}, "status": "ok"}


def test_run_skill_handler_reflects_slot_switch(skill_factory):
    env, version = skill_factory(
        "switch_skill",
        slots=("A", "B"),
        handler_source={
            "A": textwrap.dedent(
                """
                def handle(topic, payload):
                    return {"slot": "A"}
                """
            ),
            "B": textwrap.dedent(
                """
                def handle(topic, payload):
                    return {"slot": "B"}
                """
            ),
        },
        prep_source=None,
    )

    first = run_skill_handler_sync("switch_skill", "demo.topic", {})
    assert first == {"slot": "A"}

    env.set_active_slot(version, "B")
    second = run_skill_handler_sync("switch_skill", "demo.topic", {})
    assert second == {"slot": "B"}


def test_run_skill_prep_executes_script(skill_factory):
    env, version = skill_factory("prep_skill")

    result = run_skill_prep("prep_skill")

    assert result["status"] == "ok"
    artifact_path = Path(result["artifact"])
    assert artifact_path.read_text(encoding="utf-8") == "done"
    active_slot = env.read_active_slot(version)
    expected_artifact = env.build_slot_paths(version, active_slot).src_dir / "skills" / "prep_skill" / "prep" / "artifact.txt"
    assert artifact_path.resolve() == expected_artifact.resolve()


def test_run_skill_prep_missing_script_raises(skill_factory):
    skill_factory("no_prep", prep_source=None)

    with pytest.raises(SkillPrepScriptNotFoundError):
        run_skill_prep("no_prep")
