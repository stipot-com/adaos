from __future__ import annotations

import types

from typer.testing import CliRunner

from adaos.apps.cli.commands.setup import autostart_app
from adaos.apps.cli.commands import setup as setup_cmd
from requests import ConnectionError as RequestsConnectionError


def test_autostart_update_status_uses_local_admin_api(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        setup_cmd,
        "_autostart_admin_get",
        lambda path, token=None: {
            "ok": True,
            "status": {"state": "idle", "message": "boot", "target_rev": "rev2026"},
            "slots": {
                "active_slot": "A",
                "previous_slot": "B",
                "slots": {
                    "A": {
                        "manifest": {
                            "target_version": "0.1.0",
                            "git_short_commit": "8e2f6e75",
                            "git_commit": "8e2f6e7529b60f67094a7951e690558c67fdf333",
                            "git_branch": "rev2026",
                            "git_subject": "feat: add git webhook",
                        }
                    },
                    "B": {"manifest": {"target_version": "0.1.0", "git_short_commit": "4a525775", "git_branch": "rev2026"}},
                },
            },
        },
    )

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code == 0, result.output
    assert "state: idle" in result.output
    assert "target rev: rev2026" in result.output
    assert "active slot: A | 0.1.0 | 8e2f6e75 | rev2026" in result.output
    assert "active commit: 8e2f6e7529b60f67094a7951e690558c67fdf333" in result.output


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


def test_autostart_update_start_defaults_to_current_branch(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(setup_cmd, "BUILD_INFO", types.SimpleNamespace(version="0.1.0+2.def"))
    monkeypatch.setattr(setup_cmd, "_repo_git_text", lambda *args: "rev2026")
    captured: dict[str, object] = {}

    def _post(path, *, body=None, token=None):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(setup_cmd, "_autostart_admin_post", _post)

    result = runner.invoke(autostart_app, ["update-start", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["body"]["target_rev"] == "rev2026"
    assert captured["body"]["target_version"] == "0.1.0+2.def"


def test_autostart_update_status_reports_service_unavailable(monkeypatch) -> None:
    runner = CliRunner()

    def _boom(path, *, token=None):
        raise RuntimeError(
            "local AdaOS admin API is unavailable; the service may be restarting or failed to boot. "
            "Inspect 'journalctl --user -u adaos.service -n 120 --no-pager' and '.adaos/state/core_update/status.json'."
        )

    monkeypatch.setattr(setup_cmd, "_autostart_admin_get", _boom)

    result = runner.invoke(autostart_app, ["update-status"])

    assert result.exit_code != 0
    assert "local AdaOS admin API is unavailable" in result.output
