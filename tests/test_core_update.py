from __future__ import annotations

import subprocess

from adaos.services.core_update import (
    clear_plan,
    execute_pending_update,
    finalize_runtime_boot_status,
    configured_update_command,
    read_last_result,
    read_plan,
    read_status,
    rollback_installed_skill_runtimes,
    write_plan,
    write_status,
)
from adaos.services.core_slots import active_slot, activate_slot, read_slot_manifest, write_slot_manifest


def test_core_update_plan_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    clear_plan()
    payload = {"target_rev": "rev2026", "expires_at": 9999999999.0}
    write_plan(payload)
    assert read_plan()["target_rev"] == "rev2026"


def test_core_update_command_formats_placeholders(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_CORE_UPDATE_CMD", "echo {target_rev} {target_version} {base_dir}")
    cmd = configured_update_command({"target_rev": "rev2026", "target_version": "1.2.3"})
    assert cmd is not None
    assert "rev2026" in cmd
    assert "1.2.3" in cmd
    assert str(tmp_path) in cmd


def test_core_update_command_uses_builtin_runner_when_not_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("ADAOS_CORE_UPDATE_CMD", raising=False)
    cmd = configured_update_command({"target_rev": "rev2026", "target_slot": "B", "inactive_slot_dir": str(tmp_path / "slot-b")})
    assert cmd is not None
    assert "adaos.apps.core_update_apply" in cmd
    assert "rev2026" in cmd
    assert '--slot "B"' in cmd
    assert f'--slot-dir "{tmp_path / "slot-b"}"' in cmd


def test_core_update_status_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status({"state": "countdown", "message": "scheduled"})
    assert read_status()["state"] == "countdown"


def test_core_update_status_publishes_bus_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    published: list[object] = []

    class _Bus:
        def publish(self, evt) -> None:
            published.append(evt)

    class _Ctx:
        bus = _Bus()

    monkeypatch.setattr("adaos.services.core_update.get_ctx", lambda: _Ctx())
    write_status({"state": "countdown", "message": "scheduled"})
    assert published
    assert getattr(published[0], "type", "") == "core.update.status"


def test_execute_pending_update_activates_target_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))

    def _fake_run(command: str, shell: bool, capture_output: bool, text: bool):
        write_slot_manifest(
            "B",
            {
                "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
                "version": "2026.1",
            },
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("adaos.services.core_update.subprocess.run", _fake_run)
    result = execute_pending_update({"target_rev": "rev2026", "target_slot": "B"})
    assert result["state"] == "succeeded"
    assert active_slot() == "B"
    assert read_slot_manifest("B")["version"] == "2026.1"


def test_execute_pending_update_rolls_back(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_slot_manifest("A", {"argv": ["python", "-m", "adaos.apps.autostart_runner"]})
    write_slot_manifest("B", {"argv": ["python", "-m", "adaos.apps.autostart_runner"]})
    activate_slot("A")
    activate_slot("B")
    monkeypatch.setattr(
        "adaos.services.core_update.rollback_installed_skill_runtimes",
        lambda: {"ok": True, "total": 2, "failed_total": 0, "rollback_total": 2, "skills": []},
    )
    result = execute_pending_update({"action": "rollback"})
    assert result["state"] == "rolled_back"
    assert active_slot() == "A"
    assert result["skill_runtime_rollback"]["rollback_total"] == 2


def test_execute_pending_update_inherits_target_rev_from_active_slot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_slot_manifest("A", {"argv": ["python", "-m", "adaos.apps.autostart_runner"], "target_rev": "rev2026"})
    activate_slot("A")

    seen: dict[str, str] = {}

    def _fake_run(command: str, shell: bool, capture_output: bool, text: bool):
        seen["command"] = command
        write_slot_manifest(
            "B",
            {
                "argv": ["python", "-m", "adaos.apps.autostart_runner", "--host", "{host}", "--port", "{port}"],
                "target_rev": "rev2026",
            },
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("adaos.services.core_update.subprocess.run", _fake_run)
    result = execute_pending_update({"target_version": "0.1.0"})
    assert result["state"] == "succeeded"
    assert "rev2026" in seen["command"]


def test_rollback_installed_skill_runtimes_marks_expected_skips(monkeypatch) -> None:
    class _Row:
        def __init__(self, name: str, installed: bool = True) -> None:
            self.name = name
            self.installed = installed

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row("weather_skill"), _Row("voice_skill"), _Row("draft_skill", installed=False)]

    class _Manager:
        def rollback_runtime(self, name: str) -> str:
            if name == "weather_skill":
                return "A"
            raise RuntimeError("no previous slot recorded for rollback")

    class _Ctx:
        sql = object()
        skills_repo = object()
        git = object()
        paths = object()
        bus = None
        caps = object()

    monkeypatch.setattr("adaos.services.core_update.get_ctx", lambda: _Ctx())
    monkeypatch.setattr("adaos.adapters.db.SqliteSkillRegistry", _Registry)
    monkeypatch.setattr("adaos.services.skill.manager.SkillManager", lambda **kwargs: _Manager())

    payload = rollback_installed_skill_runtimes()

    assert payload["ok"] is True
    assert payload["rollback_total"] == 1
    assert payload["skipped_total"] == 1
    assert payload["failed_total"] == 0


def test_finalize_runtime_boot_status_marks_root_promotion_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_slot_manifest(
        "B",
        {
            "slot": "B",
            "argv": ["python", "-m", "adaos.apps.autostart_runner"],
            "bootstrap_update": {
                "required": True,
                "changed_paths": ["src/adaos/apps/supervisor.py"],
            },
        },
    )
    activate_slot("B")
    write_status({"state": "restarting", "phase": "launch", "target_slot": "B"})

    payload = finalize_runtime_boot_status()

    assert payload is not None
    assert payload["state"] == "validated"
    assert payload["phase"] == "root_promotion_pending"
    assert payload["root_promotion_required"] is True
    assert "src/adaos/apps/supervisor.py" in payload["bootstrap_update"]["changed_paths"]
    assert read_last_result()["phase"] == "root_promotion_pending"
