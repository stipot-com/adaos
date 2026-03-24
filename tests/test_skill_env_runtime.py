from __future__ import annotations

import json
from pathlib import Path

from adaos.sdk.data.skill_memory import get as skill_memory_get, set as skill_memory_set
from adaos.sdk.skill_env import get_env, read_env, set_env, skill_env_path
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


class _Caps:
    def require(self, *_args, **_kwargs) -> None:
        return None


def test_slot_skill_env_uses_shared_runtime_store() -> None:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_cache_dir())
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="demo_skill")
    env.prepare_version("1.0.0")

    slot = env.build_slot_paths("1.0.0", "A")

    assert slot.skill_env_path == env.data_root() / "db" / "skill_env.json"
    assert slot.skill_memory_path == slot.skill_env_path
    assert slot.legacy_skill_env_path == slot.runtime_dir / ".skill_env.json"
    assert slot.legacy_skill_memory_path == slot.runtime_dir / ".skill_memory.json"


def test_sync_skill_env_merges_template_legacy_and_store(tmp_path: Path, monkeypatch) -> None:
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.skills_dir())
    skills_root = Path(ctx.paths.skills_cache_dir())
    skill_name = "merge_skill"
    skill_dir = workspace_root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / ".skill_env.json").write_text(
        json.dumps({"defaults": {"city": "Moscow"}, "ui": {"theme": "light"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (skill_dir / ".skill_memory.json").write_text(
        json.dumps({"memory": {"recent": ["weather"]}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill_name)
    env.prepare_version("1.2.0")
    slot = env.build_slot_paths("1.2.0", "A")
    staged_skill_root = slot.src_dir / "skills" / skill_name
    staged_skill_root.mkdir(parents=True, exist_ok=True)
    (slot.legacy_skill_env_path).write_text(
        json.dumps({"ui": {"expanded": True}, "state": {"last": "legacy"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    env.skill_env_store_path().parent.mkdir(parents=True, exist_ok=True)
    env.skill_env_store_path().write_text(
        json.dumps({"ui": {"theme": "dark"}, "state": {"authoritative": True}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    mgr._sync_skill_env(env=env, skill_dir=skill_dir, slot=slot)

    merged = json.loads(env.skill_env_store_path().read_text(encoding="utf-8"))
    assert merged["defaults"]["city"] == "Moscow"
    assert merged["memory"]["recent"] == ["weather"]
    assert merged["ui"]["theme"] == "dark"
    assert merged["ui"]["expanded"] is True
    assert merged["state"]["last"] == "legacy"
    assert merged["state"]["authoritative"] is True


def test_skill_memory_and_skill_env_share_same_store(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / "db" / "skill_env.json"
    legacy_memory = tmp_path / ".skill_memory.json"
    legacy_memory.write_text(json.dumps({"seed": 7}, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(env_path))
    monkeypatch.delenv("ADAOS_SKILL_MEMORY_PATH", raising=False)

    assert read_env()["seed"] == 7
    assert skill_memory_get("seed") == 7

    set_env("alpha", {"enabled": True})
    assert skill_memory_get("alpha") == {"enabled": True}

    skill_memory_set("beta", 42)
    assert get_env("beta") == 42


def test_skill_env_prefers_ctx_runtime_path_over_env_var(monkeypatch) -> None:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_cache_dir())
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="ctx_pref_skill")
    env.prepare_version("3.0.0")
    slot = env.build_slot_paths("3.0.0", "A")
    staged_skill_root = slot.src_dir / "skills" / "ctx_pref_skill"
    staged_skill_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ADAOS_SKILL_ENV_PATH", str(Path(ctx.paths.skills_dir()) / "ctx_pref_skill" / ".skill_env.json"))
    monkeypatch.delenv("ADAOS_SKILL_MEMORY_PATH", raising=False)

    previous = ctx.skill_ctx.get()
    assert ctx.skill_ctx.set("ctx_pref_skill", staged_skill_root)
    try:
        assert skill_env_path() == env.skill_env_store_path()
    finally:
        if previous is None:
            ctx.skill_ctx.clear()
        else:
            ctx.skill_ctx.set(previous.name, previous.path)


def test_skill_env_workspace_context_uses_runtime_store_without_prepared_runtime(monkeypatch) -> None:
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.skills_dir())
    runtime_root = Path(ctx.paths.skills_cache_dir())
    skill_dir = workspace_root / "workspace_only_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.delenv("ADAOS_SKILL_ENV_PATH", raising=False)
    monkeypatch.delenv("ADAOS_SKILL_MEMORY_PATH", raising=False)

    previous = ctx.skill_ctx.get()
    assert ctx.skill_ctx.set("workspace_only_skill", skill_dir)
    try:
        assert skill_env_path() == runtime_root / ".runtime" / "workspace_only_skill" / "data" / "db" / "skill_env.json"
    finally:
        if previous is None:
            ctx.skill_ctx.clear()
        else:
            ctx.skill_ctx.set(previous.name, previous.path)
