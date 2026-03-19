from __future__ import annotations

import subprocess

from adaos.services.core_update import (
    clear_plan,
    execute_pending_update,
    configured_update_command,
    read_plan,
    read_status,
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


def test_core_update_status_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status({"state": "countdown", "message": "scheduled"})
    assert read_status()["state"] == "countdown"


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
    result = execute_pending_update({"action": "rollback"})
    assert result["state"] == "rolled_back"
    assert active_slot() == "A"
