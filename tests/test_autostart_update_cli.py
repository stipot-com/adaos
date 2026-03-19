from __future__ import annotations

import types

from typer.testing import CliRunner

from adaos.apps.cli.commands.setup import autostart_app
from adaos.apps.cli.commands import setup as setup_cmd


def test_autostart_update_status_uses_local_admin_api(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "idle", "message": "boot"},
            "slots": {"active_slot": "A", "previous_slot": "B"},
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "active slot: A" in result.output


def test_autostart_smoke_update_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+1.abc"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_admin_post", _post)

    result = runner.invoke(autostart_app, ["smoke-update", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/admin/update/start"
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+1.abc"
    assert captured["body"]["reason"] == "cli.smoke_update"
