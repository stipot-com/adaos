from __future__ import annotations

from pathlib import Path

import typer
from typer.testing import CliRunner

from adaos.apps.cli.commands import skill as skill_cmd


def test_skill_push_rejoins_split_message(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_cmd, "_resolve_skill_path", lambda target: skill_dir)

    class _Mgr:
        def push(self, skill_name: str, message: str, signoff: bool = False) -> str:
            assert skill_name == "demo_skill"
            assert message == "initial commit"
            assert signoff is False
            return "rev-1"

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    result = runner.invoke(skill_cmd.app, ["push", "demo_skill", "--message", "initial", "commit"])
    assert result.exit_code == 0, result.output
    assert "done" in result.output.lower() or "rev-1" in result.output
