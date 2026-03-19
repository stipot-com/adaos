from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from adaos.apps.cli.commands import scenario as scenario_cmd


def test_scenario_push_rejoins_split_message(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    scenario_dir = tmp_path / "web_desktop"
    scenario_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(scenario_cmd, "_resolve_scenario_path", lambda target: scenario_dir)

    class _Mgr:
        def push(self, scenario_name: str, message: str, signoff: bool = False) -> str:
            assert scenario_name == "web_desktop"
            assert message == "feat: add yjs reload btn"
            assert signoff is False
            return "rev-2"

    monkeypatch.setattr(scenario_cmd, "_mgr", lambda: _Mgr())
    result = runner.invoke(
        scenario_cmd.app,
        ["push", "web_desktop", "-m", "feat:", "add", "yjs", "reload", "btn"],
    )
    assert result.exit_code == 0, result.output
    assert "done" in result.output.lower() or "rev-2" in result.output
