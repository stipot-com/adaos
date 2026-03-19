from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

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

    monkeypatch.setattr(skill_cmd, "SkillUpdateService", lambda _ctx: _Service())
    monkeypatch.setattr(skill_cmd, "get_ctx", lambda: object())

    result = runner.invoke(skill_cmd.app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert calls == [("alpha", False), ("beta", False)]
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
