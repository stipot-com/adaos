from __future__ import annotations

from pathlib import Path
import sys
import types

from typer.testing import CliRunner

if "y_py" not in sys.modules:
    fake_y_py = types.ModuleType("y_py")
    fake_y_py.YDoc = object
    fake_y_py.YMap = object
    fake_y_py.YText = object
    fake_y_py.apply_update = lambda *args, **kwargs: None
    fake_y_py.encode_state_as_update = lambda *args, **kwargs: b""
    sys.modules["y_py"] = fake_y_py

if "ypy_websocket" not in sys.modules:
    fake_ypy_websocket = types.ModuleType("ypy_websocket")
    fake_ystore = types.ModuleType("ypy_websocket.ystore")

    class _BaseYStore:
        pass

    class _YDocNotFound(Exception):
        pass

    fake_ystore.BaseYStore = _BaseYStore
    fake_ystore.YDocNotFound = _YDocNotFound
    fake_ypy_websocket.ystore = fake_ystore
    sys.modules["ypy_websocket"] = fake_ypy_websocket
    sys.modules["ypy_websocket.ystore"] = fake_ystore

from adaos.apps.cli.commands import skill as skill_cmd
from adaos.services.skill.update import SkillUpdateResult


def test_skill_migrate_detects_changed_skills_when_name_omitted(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    (skills_dir / "alpha").mkdir(parents=True, exist_ok=True)
    (skills_dir / "beta").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_cmd, "_workspace_root", lambda: skills_dir)

    class _Proc:
        returncode = 0
        stdout = " M skills/alpha/skill.yaml\n?? skills/beta/webui.json\n"
        stderr = ""

    monkeypatch.setattr(skill_cmd.subprocess, "run", lambda *args, **kwargs: _Proc())

    calls: list[tuple[str, bool]] = []

    class _Service:
        def request_update(self, skill_id: str, *, dry_run: bool = False) -> SkillUpdateResult:
            calls.append((skill_id, dry_run))
            return SkillUpdateResult(updated=True, version="1.2.3")

    side_effects: list[tuple[str, dict[str, object]]] = []
    rebuilds: list[str | None] = []

    monkeypatch.setattr(skill_cmd, "SkillUpdateService", lambda _ctx: _Service())
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: object())
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: object())
    monkeypatch.setattr(skill_cmd, "default_webspace_id", lambda: "default")
    monkeypatch.setattr(
        skill_cmd,
        "refresh_skill_runtime",
        lambda mgr, skill_name, **kwargs: {"runtime_updated": True, "runtime_migrated": False},
    )
    monkeypatch.setattr(
        skill_cmd,
        "_refresh_runtime_side_effects",
        lambda name, **kwargs: side_effects.append((name, kwargs)),
    )
    monkeypatch.setattr(
        skill_cmd,
        "_rebuild_local_webspace",
        lambda *, webspace_id=None: rebuilds.append(webspace_id),
    )

    result = runner.invoke(skill_cmd.app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert calls == [("alpha", False), ("beta", False)]
    assert side_effects == [
        (
            "alpha",
            {
                "webspace_id": "default",
                "notify_activation": True,
                "emit_updated": True,
                "defer_hub_rebuild": True,
                "rebuild_local": False,
            },
        ),
        (
            "beta",
            {
                "webspace_id": "default",
                "notify_activation": True,
                "emit_updated": True,
                "defer_hub_rebuild": True,
                "rebuild_local": False,
            },
        ),
    ]
    assert rebuilds == ["default"]
    assert "alpha: updated (version 1.2.3)" in result.output
    assert "beta: updated (version 1.2.3)" in result.output


def test_skill_migrate_reports_no_changed_skills(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_cmd, "_workspace_root", lambda: skills_dir)

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(skill_cmd.subprocess, "run", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr(skill_cmd, "SkillUpdateService", lambda _ctx: object())
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: object())

    result = runner.invoke(skill_cmd.app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert "no changed skills detected" in result.output.lower()


def test_skill_migrate_uses_longer_hub_timeout_for_remote_updates(monkeypatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, dict | None, float]] = []

    monkeypatch.setattr(skill_cmd, "_hub_api_ready", lambda timeout_s=3.0: True)
    monkeypatch.setattr(skill_cmd, "default_webspace_id", lambda: "default")

    def _fake_hub_post(path: str, *, body: dict | None = None, timeout_s: float = 30) -> dict:
        calls.append((path, body, timeout_s))
        return {"updated": True, "version": "2.0.0"}

    monkeypatch.setattr(skill_cmd, "_hub_post", _fake_hub_post)

    result = runner.invoke(skill_cmd.app, ["migrate", "weather_skill"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "/api/skills/update",
            {
                "name": "weather_skill",
                "dry_run": False,
                "webspace_id": "default",
            },
            120,
        )
    ]
    assert "weather_skill: updated (version 2.0.0)" in result.output


def test_skill_migrate_passes_force_flag_when_requested(monkeypatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, dict | None, float]] = []

    monkeypatch.setattr(skill_cmd, "_hub_api_ready", lambda timeout_s=3.0: True)
    monkeypatch.setattr(skill_cmd, "default_webspace_id", lambda: "default")

    def _fake_hub_post(path: str, *, body: dict | None = None, timeout_s: float = 30) -> dict:
        calls.append((path, body, timeout_s))
        return {"updated": True, "version": "2.0.0"}

    monkeypatch.setattr(skill_cmd, "_hub_post", _fake_hub_post)

    result = runner.invoke(skill_cmd.app, ["migrate", "weather_skill", "--force"])

    assert result.exit_code == 0, result.output
    assert calls[0][1]["force"] is True


def test_skill_migrate_batches_remote_rebuild_until_the_end(monkeypatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, dict | None, float]] = []

    monkeypatch.setattr(skill_cmd, "_hub_api_ready", lambda timeout_s=3.0: True)
    monkeypatch.setattr(skill_cmd, "default_webspace_id", lambda: "default")
    monkeypatch.setattr(
        skill_cmd,
        "_hub_get",
        lambda path, **kwargs: {"items": [{"name": "alpha"}, {"name": "beta"}]},
    )

    def _fake_hub_post(path: str, *, body: dict | None = None, timeout_s: float = 30) -> dict:
        calls.append((path, body, timeout_s))
        return {"updated": True, "version": "2.0.0"}

    monkeypatch.setattr(skill_cmd, "_hub_post", _fake_hub_post)

    result = runner.invoke(skill_cmd.app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ("/api/skills/sync", None, 30),
        (
            "/api/skills/update",
            {
                "name": "alpha",
                "dry_run": False,
                "webspace_id": "default",
                "defer_webspace_rebuild": True,
            },
            120,
        ),
        (
            "/api/skills/update",
            {
                "name": "beta",
                "dry_run": False,
                "webspace_id": "default",
                "defer_webspace_rebuild": True,
            },
            120,
        ),
        (
            "/api/skills/runtime/rebuild-webspace",
            {"webspace_id": "default"},
            120,
        ),
    ]


def test_skill_uninstall_suggests_force_when_workspace_is_dirty(monkeypatch) -> None:
    runner = CliRunner()

    class _Mgr:
        def uninstall(self, name: str, *, safe: bool = False, force: bool = False) -> None:  # noqa: ARG002
            raise RuntimeError("git sparse-checkout init failed: error: cannot initialize sparse-checkout: You have unstaged changes.")

    monkeypatch.setattr(skill_cmd, "_hub_api_ready", lambda timeout_s=3.0: False)
    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(skill_cmd, "_", lambda key, **kwargs: f"rerun adaos skill uninstall {kwargs.get('name', '')} --force")

    result = runner.invoke(skill_cmd.app, ["uninstall", "infrascope_skill"])

    assert result.exit_code == 1
    assert "uninstall failed" in result.output
    assert "--force" in result.output
