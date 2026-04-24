from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from adaos.sdk.data.skill_memory import get as skill_memory_get, set as skill_memory_set
from adaos.sdk.skill_env import get_env, read_env, set_env, skill_env_path
from adaos.services.agent_context import get_ctx
from adaos.services.skill import manager as skill_manager_module
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


class _Caps:
    def require(self, *_args, **_kwargs) -> None:
        return None


def test_slot_skill_env_uses_shared_runtime_store() -> None:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="demo_skill")
    env.prepare_version("1.0.0")

    slot = env.build_slot_paths("1.0.0", "A")

    assert slot.skill_env_path == env.data_root() / "db" / "skill_env.json"
    assert slot.skill_memory_path == slot.skill_env_path
    assert slot.legacy_skill_env_path == slot.runtime_dir / ".skill_env.json"
    assert slot.legacy_skill_memory_path == slot.runtime_dir / ".skill_memory.json"
    assert slot.internal_data_dir == env.internal_slot_dir("A")


def test_internal_data_slots_have_active_and_previous_markers() -> None:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name="internal_data_skill")
    env.prepare_version("1.0.0")

    assert env.read_active_internal_slot() == "a"
    assert env.internal_slot_dir("A").exists()
    assert env.internal_slot_dir("B").exists()

    env.set_active_internal_slot("B")
    assert env.read_active_internal_slot() == "b"
    assert env.rollback_internal_slot() == "a"
    assert env.read_active_internal_slot() == "a"


def test_sync_skill_env_merges_template_legacy_and_store(tmp_path: Path, monkeypatch) -> None:
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.skills_dir())
    skills_root = Path(ctx.paths.skills_dir())
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
    skills_root = Path(ctx.paths.skills_dir())
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
    runtime_root = Path(ctx.paths.skills_dir())
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


def test_run_dev_tool_respects_timeout(monkeypatch, tmp_path: Path) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_dir = tmp_path / "skills" / "slow_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "resolved.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": str(skill_dir),
                "version": "1.0.0",
                "slot": "A",
                "tools": {
                    "slow_tool": {
                        "callable": "slow_tool",
                        "timeout_seconds": 0.01,
                    }
                },
                "runtime": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeEnv:
        def __init__(self, root: Path) -> None:
            self._root = root

        def data_root(self) -> Path:
            return self._root / "data"

        def build_slot_paths(self, _version: str | None, slot_name: str | None) -> SimpleNamespace:
            runtime_root = self._root / "runtime" / str(slot_name or "A")
            runtime_root.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(
                skill_env_path=runtime_root / "skill_env.json",
                skill_memory_path=runtime_root / "skill_memory.json",
            )

    monkeypatch.setattr(
        mgr,
        "dev_runtime_status",
        lambda _name: {
            "version": "1.0.0",
            "active_slot": "A",
            "resolved_manifest": str(manifest_path),
            "ready": True,
        },
    )
    monkeypatch.setattr(mgr, "_runtime_env_dev", lambda _name: _FakeEnv(tmp_path))
    monkeypatch.setattr(mgr, "_persist_skill_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(skill_manager_module, "execute_tool", lambda *_args, **_kwargs: (time.sleep(0.05), {"ok": True})[1])

    try:
        mgr.run_dev_tool("slow_skill", "slow_tool", {})
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover - regression guard
        raise AssertionError("expected run_dev_tool() to respect timeout_seconds")


def test_prepare_runtime_copies_internal_data_without_custom_tool(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "copy_data_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: copy_data_skill\nversion: '1.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)
    env.prepare_version("1.0.0")
    env.internal_slot_dir("A").mkdir(parents=True, exist_ok=True)
    (env.internal_slot_dir("A") / "state.json").write_text('{"value": 1}', encoding="utf-8")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": "B",
            "source": str(kwargs["skill_dir"]),
            "runtime": {"skill_env": str(kwargs["slot"].skill_env_path), "skill_memory": str(kwargs["slot"].skill_memory_path)},
            "tools": {},
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )

    result = mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="B")

    assert result.data_migration is not None
    assert result.data_migration["mode"] == "copy"
    assert result.data_migration["copied_entries"] == 1
    assert (env.internal_slot_dir("B") / "state.json").read_text(encoding="utf-8") == '{"value": 1}'


def test_prepare_runtime_runs_custom_internal_data_migration_tool(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "migrate_data_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (
        skill_dir / "skill.yaml"
    ).write_text(
        "name: migrate_data_skill\nversion: '1.0.0'\ndata_migration_tool: migrate_data\n",
        encoding="utf-8",
    )

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)
    env.prepare_version("1.0.0")
    (env.internal_slot_dir("A") / "old.txt").write_text("legacy", encoding="utf-8")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": "B",
            "source": str(kwargs["skill_dir"]),
            "runtime": {"skill_env": str(kwargs["slot"].skill_env_path), "skill_memory": str(kwargs["slot"].skill_memory_path)},
            "tools": {
                "migrate_data": {
                    "module": "skills.migrate_data_skill.handlers.main",
                    "callable": "migrate_data",
                }
            },
            "default_tool": "",
            "data_migration_tool": "migrate_data",
            "data_migration": {"tool": "migrate_data"},
        },
    )

    captured: dict[str, object] = {}

    def _fake_execute_tool(skill_dir_arg, *, module=None, attr=None, payload=None, extra_paths=None):
        captured["module"] = module
        captured["attr"] = attr
        captured["payload"] = dict(payload or {})
        target = Path(str(payload["target_internal_dir"]))
        target.mkdir(parents=True, exist_ok=True)
        (target / "migrated.txt").write_text("ok", encoding="utf-8")
        return {"ok": True, "migrated": True}

    monkeypatch.setattr(skill_manager_module, "execute_tool", _fake_execute_tool)

    result = mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="B")

    assert result.data_migration is not None
    assert result.data_migration["mode"] == "tool"
    assert result.data_migration["tool"] == "migrate_data"
    assert captured["attr"] == "migrate_data"
    assert (env.internal_slot_dir("B") / "migrated.txt").read_text(encoding="utf-8") == "ok"


def test_activate_and_rollback_runtime_switch_internal_data_slot(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "internal_switch_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: internal_switch_skill\nversion: '1.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)
    env.prepare_version("1.0.0")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": "B",
            "source": str(kwargs["skill_dir"]),
            "runtime": {"skill_env": str(kwargs["slot"].skill_env_path), "skill_memory": str(kwargs["slot"].skill_memory_path)},
            "tools": {},
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="B")
    assert env.read_active_internal_slot() == "a"

    mgr.activate_runtime(skill_name, version="1.0.0", slot="B")
    assert env.read_active_internal_slot() == "b"

    restored = mgr.rollback_runtime(skill_name)
    assert restored == "A"
    assert env.read_active_internal_slot() == "a"


def test_deactivate_runtime_blocks_execution_until_reactivated(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "deactivate_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: deactivate_skill\nversion: '1.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {"skill_env": str(kwargs["slot"].skill_env_path), "skill_memory": str(kwargs["slot"].skill_memory_path)},
            "tools": {"handle": {"module": "skills.deactivate_skill.handlers.main", "callable": "handle"}},
            "default_tool": "handle",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)
    monkeypatch.setattr(skill_manager_module, "execute_tool", lambda *args, **kwargs: {"ok": True})

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="A")
    mgr.activate_runtime(skill_name, version="1.0.0", slot="A")

    payload = mgr.deactivate_runtime(skill_name, reason="post_commit_check_failed")
    status = mgr.runtime_status(skill_name)

    assert payload["deactivated"] is True
    assert status["deactivated"] is True
    assert status["active"] is False
    assert status["deactivation"]["reason"] == "post_commit_check_failed"

    try:
        mgr.run_tool(skill_name, None, {})
    except RuntimeError as exc:
        assert "deactivated" in str(exc)
    else:  # pragma: no cover - regression guard
        raise AssertionError("expected deactivated skill execution to fail")

    mgr.activate_runtime(skill_name, version="1.0.0", slot="A")
    status_after = mgr.runtime_status(skill_name)
    assert status_after["deactivated"] is False
    assert status_after["active"] is True


def test_activate_runtime_does_not_switch_slot_before_smoke_import(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "atomic_activate_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: atomic_activate_skill\nversion: '1.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {"skill_env": str(kwargs["slot"].skill_env_path), "skill_memory": str(kwargs["slot"].skill_memory_path)},
            "tools": {"handle": {"module": "skills.atomic_activate_skill.handlers.main", "callable": "handle"}},
            "default_tool": "handle",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="A")
    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="B")
    assert env.read_active_slot("1.0.0") == "A"

    monkeypatch.setattr(
        mgr,
        "_smoke_import",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broken handler import")),
    )

    try:
        mgr.activate_runtime(skill_name, version="1.0.0", slot="B")
    except RuntimeError as exc:
        assert "broken handler import" in str(exc)
    else:  # pragma: no cover - regression guard
        raise AssertionError("expected activation to fail")

    assert env.read_active_slot("1.0.0") == "A"


def test_activate_runtime_runs_lifecycle_hooks_and_publishes_status(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "lifecycle_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: lifecycle_skill\nversion: '1.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)
    env.prepare_version("1.0.0")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {
                "skill_env": str(kwargs["slot"].skill_env_path),
                "skill_memory": str(kwargs["slot"].skill_memory_path),
                "python_paths": [],
            },
            "tools": {
                "persist_state": {"module": "skills.lifecycle_skill.handlers.main", "callable": "persist_state"},
                "after_activate_tool": {"module": "skills.lifecycle_skill.handlers.main", "callable": "after_activate_tool"},
                "rehydrate_tool": {"module": "skills.lifecycle_skill.handlers.main", "callable": "rehydrate_tool"},
            },
            "lifecycle": {
                "persist_before_switch": "persist_state",
                "after_activate": "after_activate_tool",
                "rehydrate": "rehydrate_tool",
            },
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)

    calls: list[tuple[str | None, dict[str, object]]] = []

    def _fake_execute_tool(skill_dir_arg, *, module=None, attr=None, payload=None, extra_paths=None):
        calls.append((attr, dict(payload or {})))
        return {"ok": True, "attr": attr}

    monkeypatch.setattr(skill_manager_module, "execute_tool", _fake_execute_tool)

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="A")
    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="B")
    mgr.activate_runtime(skill_name, version="1.0.0", slot="B")

    status = mgr.runtime_status(skill_name)

    assert [name for name, _payload in calls] == ["persist_state", "after_activate_tool", "rehydrate_tool"]
    assert status["lifecycle"]["persist"]["ok"] is True
    assert status["lifecycle"]["persist"]["skipped"] is False
    assert status["lifecycle"]["after_activate"]["tool"] == "after_activate_tool"
    assert status["lifecycle"]["rehydrate"]["tool"] == "rehydrate_tool"
    assert status["lifecycle"]["healthcheck"]["ok"] is True


def test_deactivate_runtime_runs_before_deactivate_hook(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "before_deactivate_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: before_deactivate_skill\nversion: '1.0.0'\n", encoding="utf-8")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {
                "skill_env": str(kwargs["slot"].skill_env_path),
                "skill_memory": str(kwargs["slot"].skill_memory_path),
                "python_paths": [],
            },
            "tools": {
                "before_deactivate_tool": {
                    "module": "skills.before_deactivate_skill.handlers.main",
                    "callable": "before_deactivate_tool",
                }
            },
            "lifecycle": {"before_deactivate": "before_deactivate_tool"},
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)

    calls: list[tuple[str | None, dict[str, object]]] = []

    def _fake_execute_tool(skill_dir_arg, *, module=None, attr=None, payload=None, extra_paths=None):
        calls.append((attr, dict(payload or {})))
        return {"ok": True, "attr": attr}

    monkeypatch.setattr(skill_manager_module, "execute_tool", _fake_execute_tool)

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="A")
    mgr.activate_runtime(skill_name, version="1.0.0", slot="A")
    payload = mgr.deactivate_runtime(skill_name, reason="manual_check")
    status = mgr.runtime_status(skill_name)

    assert payload["deactivated"] is True
    assert [name for name, _payload in calls] == ["before_deactivate_tool"]
    assert status["lifecycle"]["before_deactivate"]["tool"] == "before_deactivate_tool"


def test_deactivate_runtime_runs_shutdown_hooks_in_order(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "shutdown_hooks_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: shutdown_hooks_skill\nversion: '1.0.0'\n", encoding="utf-8")

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(
        mgr,
        "_enrich_manifest",
        lambda **kwargs: {
            "name": skill_name,
            "version": "1.0.0",
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {
                "skill_env": str(kwargs["slot"].skill_env_path),
                "skill_memory": str(kwargs["slot"].skill_memory_path),
                "python_paths": [],
            },
            "tools": {
                "drain_tool": {"module": "skills.shutdown_hooks_skill.handlers.main", "callable": "drain_tool"},
                "dispose_tool": {"module": "skills.shutdown_hooks_skill.handlers.main", "callable": "dispose_tool"},
                "before_deactivate_tool": {
                    "module": "skills.shutdown_hooks_skill.handlers.main",
                    "callable": "before_deactivate_tool",
                },
            },
            "lifecycle": {
                "drain": "drain_tool",
                "dispose": "dispose_tool",
                "before_deactivate": "before_deactivate_tool",
            },
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        },
    )
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)

    calls: list[str | None] = []

    def _fake_execute_tool(skill_dir_arg, *, module=None, attr=None, payload=None, extra_paths=None):
        calls.append(attr)
        return {"ok": True, "attr": attr}

    monkeypatch.setattr(skill_manager_module, "execute_tool", _fake_execute_tool)

    mgr.prepare_runtime(skill_name, run_tests=False, preferred_slot="A")
    mgr.activate_runtime(skill_name, version="1.0.0", slot="A")
    status = mgr.runtime_status(skill_name)
    assert status["lifecycle"]["drain"]["skipped"] is True

    mgr.deactivate_runtime(skill_name, reason="manual_check")
    status = mgr.runtime_status(skill_name)

    assert calls == ["drain_tool", "dispose_tool", "before_deactivate_tool"]
    assert status["lifecycle"]["drain"]["tool"] == "drain_tool"
    assert status["lifecycle"]["dispose"]["tool"] == "dispose_tool"
    assert status["lifecycle"]["before_deactivate"]["tool"] == "before_deactivate_tool"


def test_failed_rehydrate_restores_previous_active_version(monkeypatch) -> None:
    ctx = get_ctx()
    mgr = SkillManager(git=ctx.git, paths=ctx.paths, caps=_Caps())
    skill_name = "rehydrate_restore_skill"
    skill_dir = Path(ctx.paths.skills_dir()) / skill_name
    (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
    (skill_dir / "handlers" / "main.py").write_text("def handle(payload=None):\n    return payload or {}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: rehydrate_restore_skill\nversion: '2.0.0'\n", encoding="utf-8")

    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=skill_name)
    env.prepare_version("1.0.0")
    env.prepare_version("2.0.0")
    env.active_version_marker().write_text("1.0.0", encoding="utf-8")
    env.set_active_slot("1.0.0", "A")
    env.set_active_internal_slot("A")

    def _fake_enrich_manifest(**kwargs):
        version = kwargs["manifest"].get("version") or "0.0.0"
        return {
            "name": skill_name,
            "version": version,
            "slot": kwargs["slot"].slot,
            "source": str(kwargs["skill_dir"]),
            "runtime": {
                "skill_env": str(kwargs["slot"].skill_env_path),
                "skill_memory": str(kwargs["slot"].skill_memory_path),
                "python_paths": [],
            },
            "tools": {
                "rehydrate_tool": {"module": "skills.rehydrate_restore_skill.handlers.main", "callable": "rehydrate_tool"},
                "drain_tool": {"module": "skills.rehydrate_restore_skill.handlers.main", "callable": "drain_tool"},
                "dispose_tool": {"module": "skills.rehydrate_restore_skill.handlers.main", "callable": "dispose_tool"},
                "before_deactivate_tool": {
                    "module": "skills.rehydrate_restore_skill.handlers.main",
                    "callable": "before_deactivate_tool",
                },
            },
            "lifecycle": {
                "rehydrate": "rehydrate_tool",
                "drain": "drain_tool",
                "dispose": "dispose_tool",
                "before_deactivate": "before_deactivate_tool",
            },
            "default_tool": "",
            "data_migration_tool": "",
            "data_migration": {},
        }

    monkeypatch.setattr(mgr, "_prepare_runtime_environment", lambda **kwargs: (Path("python"), []))
    monkeypatch.setattr(mgr, "_enrich_manifest", _fake_enrich_manifest)
    monkeypatch.setattr(skill_manager_module, "install_skill_in_capacity", lambda *args, **kwargs: None)
    monkeypatch.setattr(mgr, "_smoke_import", lambda **kwargs: None)

    calls: list[str | None] = []

    def _fake_execute_tool(skill_dir_arg, *, module=None, attr=None, payload=None, extra_paths=None):
        calls.append(attr)
        if attr == "rehydrate_tool":
            raise RuntimeError("rehydrate exploded")
        return {"ok": True, "attr": attr}

    monkeypatch.setattr(skill_manager_module, "execute_tool", _fake_execute_tool)

    mgr.prepare_runtime(skill_name, version_override="1.0.0", run_tests=False, preferred_slot="A")
    mgr.prepare_runtime(skill_name, version_override="2.0.0", run_tests=False, preferred_slot="B")

    try:
        mgr.activate_runtime(skill_name, version="2.0.0", slot="B")
    except RuntimeError as exc:
        assert "activation rehydrate failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected activation failure")

    assert env.resolve_active_version() == "1.0.0"
    assert env.read_active_slot("1.0.0") == "A"
    status = mgr.runtime_status(skill_name)
    assert status["version"] == "1.0.0"

    failed_meta = env.read_version_metadata("2.0.0")
    failed_lifecycle = failed_meta["slots"]["B"]["lifecycle"]
    assert failed_lifecycle["healthcheck"]["ok"] is False
    assert failed_lifecycle["rollback"]["ok"] is True
    assert failed_lifecycle["rollback"]["restored_active_version"] == "1.0.0"
    assert calls == ["rehydrate_tool", "drain_tool", "dispose_tool", "before_deactivate_tool"]
